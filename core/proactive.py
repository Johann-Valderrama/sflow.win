"""Proactivo v2 (unidad 5.3) — modo, gate de push, cola de lull y coaching de monólogo.

Módulo PURO (sin Qt, sin I/O de red, sin hilos propios): solo estado en memoria
y funciones/objetos testeables de forma sintética (el caller — main.py, en el tick
del ``_meeting_sync_timer`` — decide CUÁNDO llamar ``update``/``pop_deliverable``).
``ProactiveGate`` SÍ es mutado concurrentemente por 3 hilos reales de la app (tick
Qt, daemon de insight, daemon de consolidación — ver unidad 1.1), así que protege
su estado con un ``threading.Lock`` interno; eso no lo vuelve "impuro": no lanza
hilos ni hace I/O, solo serializa el acceso a memoria compartida.

Producto (no se re-litiga aquí, ver unidad): un "push" es cualquier tarjeta que
aparece sin que el usuario la pida; el presupuesto es ~1 push/5min salvo que sea
un pendiente (esos SIEMPRE se muestran, es su contrato desde la unidad 2.2). Toda
tarjeta expira si nadie la atiende. El HUD (5.3, ui/hud_widget.py) es quien renderiza;
este módulo solo decide QUÉ y CUÁNDO entregar.

Modos (PROACTIVE_MODE): "silent" (solo pendientes, cero proactividad extra),
"copilot" (default: pendientes + detecciones + memoria cruzada), "trainer"
(+ coaching, p.ej. monólogo prolongado).

Concurrencia (unidad 1.1, 2026-07): TODOS los métodos públicos que leen o mutan
``_queue``/presupuesto/dedup toman ``self._lock`` UNA sola vez y delegan en
helpers privados ``_*_locked`` (nunca en otro método público, que también toma
el lock — ``threading.Lock`` no es reentrante: hacerlo produciría deadlock).
El lock solo protege mutaciones de estado en memoria; nunca se retiene durante
trabajo largo (no hay LLM ni I/O en esta clase). ``try_push`` es el método
atómico que reemplaza el patrón previo should_push→enqueue→mark_pushed en 3
llamadas separadas (TOCTOU real entre hilos): decide, encola y consume
presupuesto bajo un único acquire.
"""
import os
import threading
import time

from config import MEETING_SILENCE_RMS

_VALID_MODES = ("silent", "copilot", "trainer")
_DEFAULT_MODE = "copilot"

# Presupuesto: como máximo un push NO-pendiente cada N segundos.
_PUSH_BUDGET_SECONDS = 300.0

# Cola de lull: cuánto puede esperar una tarjeta a una pausa natural antes de
# entregarse igualmente (evita que se quede esperando para siempre en una
# reunión sin silencios), y cuándo expira (se descarta sin mostrarse).
_LULL_FORCE_AFTER_SECONDS = 60.0
_QUEUE_EXPIRE_SECONDS = 180.0

# Umbral de silencio: MISMO valor que MEETING_SILENCE_RMS (config.py), importado
# directamente (unidad 1.4) — antes era un literal duplicado aquí, que quedaba
# desincronizado si alguien cambiaba MEETING_SILENCE_RMS por .env. Alias local
# conservado (mismo nombre) porque is_lull/LullDetector/MonologueWatch ya lo usan
# como valor por defecto de sus parámetros.
_SILENCE_RMS = MEETING_SILENCE_RMS
_LULL_MIN_TICKS = 2  # ticks sostenidos (~1s cada uno) por debajo del umbral

# Monólogo (modo trainer): ~90 ticks de 1s = ~90s hablando yo, sin que ellos hablen.
_MONOLOGUE_TICKS = 90
_YIELD_TICKS = 5  # ceder la palabra >=5 ticks sostenidos reinicia el contador


def get_mode() -> str:
    """Modo proactivo activo (env PROACTIVE_MODE). Cae a 'copilot' si el valor
    no es reconocido (fail-safe: nunca deja el sistema silenciado por typo)."""
    mode = (os.getenv("PROACTIVE_MODE", "") or "").strip().lower()
    return mode if mode in _VALID_MODES else _DEFAULT_MODE


class ProactiveGate:
    """Decide SI se puede empujar una tarjeta ahora, y gestiona la cola de espera
    a una pausa natural (lull) antes de entregarla al HUD.

    No decide EL CONTENIDO de las tarjetas (eso lo hacen las clases 5.1/5.2); solo
    el timing y el presupuesto. Instancia única de proceso: ``PROACTIVE`` al final
    del módulo (mismo patrón que ``MEETING`` en core/meeting.py).
    """

    def __init__(self):
        self._lock = threading.Lock()  # protege TODO lo de abajo (unidad 1.1)
        self._last_push_at: dict = {}  # kind -> monotonic del último push de ese tipo
        self._last_nonpending_push_at: float = 0.0
        self._queue: list = []          # [{key,tipo,texto,detail?,t,queued_at}]
        self._delivered_keys: set = set()
        self._discarded_keys: set = set()

    def reset(self):
        """Reinicia todo el estado (usar al terminar/empezar una reunión nueva)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self):
        self._last_push_at.clear()
        self._last_nonpending_push_at = 0.0
        self._queue.clear()
        self._delivered_keys.clear()
        self._discarded_keys.clear()

    # ------------------------------------------------------------------
    # Presupuesto / gate
    # ------------------------------------------------------------------

    def should_push(self, kind: str, now: float = None) -> bool:
        """¿Se puede empujar una tarjeta de tipo ``kind`` ahora mismo?

        - "pendiente" siempre True (decisión de producto — unidad 2.2, no se toca).
        - Cualquier otro tipo: False si el modo activo es "silent", o si hubo un
          push NO-pendiente hace menos de _PUSH_BUDGET_SECONDS (presupuesto global
          de ruido, no por-kind: un push de detección y uno de coaching comparten
          el mismo presupuesto de "algo apareció sin que lo pidiera").

        NOTA (unidad 1.1): expuesto para tests sintéticos y para el coaching de
        main.py, que no necesita atomicidad con un enqueue. Los callers de
        producción que SÍ encolan a partir de esta decisión (detecciones 5.1,
        memoria cruzada 5.2) deben usar ``try_push`` para evitar el TOCTOU entre
        esta llamada y ``mark_pushed``.
        """
        with self._lock:
            return self._should_push_locked(kind, now)

    def _should_push_locked(self, kind: str, now: float = None) -> bool:
        if kind == "pendiente":
            return True
        if get_mode() == "silent":
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_nonpending_push_at) >= _PUSH_BUDGET_SECONDS

    def mark_pushed(self, kind: str, now: float = None):
        """Registra que se empujó una tarjeta de tipo ``kind`` (consume presupuesto
        si no es un pendiente)."""
        with self._lock:
            self._mark_pushed_locked(kind, now)

    def _mark_pushed_locked(self, kind: str, now: float = None):
        now = now if now is not None else time.monotonic()
        self._last_push_at[kind] = now
        if kind != "pendiente":
            self._last_nonpending_push_at = now

    # ------------------------------------------------------------------
    # Cola de espera a lull
    # ------------------------------------------------------------------

    def enqueue(self, card: dict, now: float = None) -> bool:
        """Encola una tarjeta candidata a entregarse en la próxima pausa natural.

        Dedup por ``key``: si ya se entregó o se descartó esa key, se ignora (nunca
        reaparece). Si ya está en cola, no duplica. Devuelve True si la tarjeta
        quedó efectivamente encolada, False si se descartó por dedup/falta de key
        (el valor de retorno es nuevo en la unidad 1.1; los callers que lo ignoraban
        antes siguen funcionando igual).
        """
        with self._lock:
            return self._enqueue_locked(card, now)

    def _enqueue_locked(self, card: dict, now: float = None) -> bool:
        key = card.get("key")
        if not key:
            return False
        if key in self._delivered_keys or key in self._discarded_keys:
            return False
        if any(c["key"] == key for c in self._queue):
            return False
        now = now if now is not None else time.monotonic()
        entry = dict(card)
        entry["queued_at"] = now
        self._queue.append(entry)
        return True

    def pop_deliverable(self, lull: bool, now: float = None) -> "dict | None":
        """Extrae de la cola LA SIGUIENTE tarjeta lista para entregarse (FIFO), o
        None si ninguna lo está todavía.

        Primero purga expiradas (>= _QUEUE_EXPIRE_SECONDS en cola, se descartan sin
        entregarse). Luego, si hay lull o la más vieja lleva >= _LULL_FORCE_AFTER_SECONDS
        esperando, la entrega (queda marcada como entregada — dedup futuro).
        """
        with self._lock:
            return self._pop_deliverable_locked(lull, now)

    def _pop_deliverable_locked(self, lull: bool, now: float = None) -> "dict | None":
        now = now if now is not None else time.monotonic()
        self._purge_expired_locked(now)
        if not self._queue:
            return None
        oldest = self._queue[0]
        waited = now - oldest["queued_at"]
        if lull or waited >= _LULL_FORCE_AFTER_SECONDS:
            self._queue.pop(0)
            self._delivered_keys.add(oldest["key"])
            oldest.pop("queued_at", None)
            return oldest
        return None

    def _purge_expired_locked(self, now: float):
        keep = []
        for c in self._queue:
            if now - c["queued_at"] >= _QUEUE_EXPIRE_SECONDS:
                self._discarded_keys.add(c["key"])
            else:
                keep.append(c)
        self._queue = keep

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def delivered(self, key: str) -> bool:
        """¿Esta key ya se entregó al HUD? Lectura pura (unidad 5.1): la usa
        MEETING.status() para exponer al panel web solo las tarjetas que el
        usuario ya vio y aún no tienen feedback ✓/✗."""
        with self._lock:
            return key in self._delivered_keys

    # ------------------------------------------------------------------
    # Decisión + encolado + presupuesto ATÓMICOS (unidad 1.1)
    # ------------------------------------------------------------------

    def try_push(self, kind: str, card: dict, now: float = None) -> bool:
        """should_push → enqueue → mark_pushed bajo UN solo ``acquire`` del lock.

        Reemplaza el patrón previo de 3 llamadas separadas (should_push,
        enqueue, mark_pushed) que dos hilos podían intercalar: ambos veían
        presupuesto disponible antes de que ninguno marcara el push, y los dos
        terminaban empujando tarjeta (doble push en la misma ventana). Aquí la
        decisión completa ocurre bajo un único lock.

        Regla dura (no se re-litiga): si el dedup de ``enqueue`` rechaza la
        tarjeta (ya entregada/descartada/ya en cola), el presupuesto NO se
        consume — ``mark_pushed`` solo se llama tras un ``enqueue`` exitoso.
        Devuelve True si la tarjeta quedó efectivamente encolada.
        """
        with self._lock:
            if not self._should_push_locked(kind, now):
                return False
            if not self._enqueue_locked(card, now):
                return False
            self._mark_pushed_locked(kind, now)
            return True


def is_lull(level_yo: float, level_ellos: float, threshold: float = _SILENCE_RMS) -> bool:
    """Verificación puntual (un solo tick): ¿ambos canales están en silencio ahora?

    Para el criterio sostenido (>= 2 ticks) usar ``LullDetector``.
    """
    return level_yo < threshold and level_ellos < threshold


class LullDetector:
    """Detecta una pausa natural SOSTENIDA (>= _LULL_MIN_TICKS ticks consecutivos
    con ambos canales por debajo del umbral de silencio). Un tick = una llamada a
    ``update`` (se asume cadencia de ~1s, igual que ``_meeting_sync_timer``)."""

    def __init__(self, threshold: float = _SILENCE_RMS, min_ticks: int = _LULL_MIN_TICKS):
        self._threshold = threshold
        self._min_ticks = min_ticks
        self._quiet_ticks = 0

    def update(self, level_yo: float, level_ellos: float) -> bool:
        """Alimenta un tick de niveles; devuelve True si hay lull sostenido AHORA."""
        if is_lull(level_yo, level_ellos, self._threshold):
            self._quiet_ticks += 1
        else:
            self._quiet_ticks = 0
        return self._quiet_ticks >= self._min_ticks

    def reset(self):
        self._quiet_ticks = 0


class MonologueWatch:
    """Coaching (modo trainer): detecta que el usuario lleva mucho rato hablando
    sin que "Ellos" intervenga, SOBRE LOS NIVELES en vivo (level_yo/level_ellos),
    nunca sobre los segmentos transcritos (esos llegan con latencia de varios
    segundos — corrección O6 del debate adversarial: usar señales sin lag).

    Dispara una única tarjeta por episodio (no re-dispara en cada tick mientras
    el umbral siga superado); se rearma cuando el otro canal recibe la palabra
    de forma sostenida (>= _YIELD_TICKS ticks), no con un simple parpadeo.
    """

    CARD_TEXT = "Llevas +90s hablando sin ceder la palabra"

    def __init__(self, threshold: float = _SILENCE_RMS,
                 monologue_ticks: int = _MONOLOGUE_TICKS,
                 yield_ticks: int = _YIELD_TICKS):
        self._threshold = threshold
        self._monologue_ticks = monologue_ticks
        self._yield_ticks = yield_ticks
        self._talk_ticks = 0     # ticks consecutivos: yo hablo, ellos callan
        self._yield_ticks_seen = 0
        self._fired = False      # ya se disparó en este episodio (no repetir)

    def update(self, level_yo: float, level_ellos: float) -> "dict | None":
        """Alimenta un tick; devuelve la tarjeta de coaching si dispara este tick
        (solo una vez por episodio), o None."""
        yo_habla = level_yo >= self._threshold
        ellos_habla = level_ellos >= self._threshold

        if ellos_habla:
            self._yield_ticks_seen += 1
            if self._yield_ticks_seen >= self._yield_ticks:
                # Cedió la palabra de forma sostenida: reset completo del episodio.
                self._talk_ticks = 0
                self._fired = False
                self._yield_ticks_seen = 0
            return None

        self._yield_ticks_seen = 0
        if yo_habla:
            self._talk_ticks += 1
        # Silencio de ambos: no acumula, pero tampoco resetea (una pausa breve
        # mientras yo sigo con la palabra no debe reiniciar el contador).

        if not self._fired and self._talk_ticks >= self._monologue_ticks:
            self._fired = True
            return {"tipo": "coaching", "texto": self.CARD_TEXT}
        return None

    def reset(self):
        self._talk_ticks = 0
        self._yield_ticks_seen = 0
        self._fired = False


# Instancia única de proceso (patrón MEETING/PROACTIVE compartida entre el
# controlador Qt y el servidor Flask, si este último llegara a necesitarla).
PROACTIVE = ProactiveGate()
