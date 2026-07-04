"""Proactivo v2 (unidad 5.3) — modo, gate de push, cola de lull y coaching de monólogo.

Módulo PURO (sin Qt, sin threading propio, sin I/O de red): solo estado en memoria
y funciones/objetos testeables de forma sintética (el caller — main.py, en el tick
del ``_meeting_sync_timer`` — decide CUÁNDO llamar ``update``/``pop_deliverable``).

Producto (no se re-litiga aquí, ver unidad): un "push" es cualquier tarjeta que
aparece sin que el usuario la pida; el presupuesto es ~1 push/5min salvo que sea
un pendiente (esos SIEMPRE se muestran, es su contrato desde la unidad 2.2). Toda
tarjeta expira si nadie la atiende. El HUD (5.3, ui/hud_widget.py) es quien renderiza;
este módulo solo decide QUÉ y CUÁNDO entregar.

Modos (PROACTIVE_MODE): "silent" (solo pendientes, cero proactividad extra),
"copilot" (default: pendientes + detecciones + memoria cruzada), "trainer"
(+ coaching, p.ej. monólogo prolongado).
"""
import os
import time

_VALID_MODES = ("silent", "copilot", "trainer")
_DEFAULT_MODE = "copilot"

# Presupuesto: como máximo un push NO-pendiente cada N segundos.
_PUSH_BUDGET_SECONDS = 300.0

# Cola de lull: cuánto puede esperar una tarjeta a una pausa natural antes de
# entregarse igualmente (evita que se quede esperando para siempre en una
# reunión sin silencios), y cuándo expira (se descarta sin mostrarse).
_LULL_FORCE_AFTER_SECONDS = 60.0
_QUEUE_EXPIRE_SECONDS = 180.0

# Umbral de silencio compartido con MEETING_SILENCE_RMS (config.py); se repite
# aquí como literal para que este módulo siga siendo puro (sin import de Qt/app
# config) — mismo valor, ver config.py MEETING_SILENCE_RMS.
_SILENCE_RMS = 0.012
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
        self._last_push_at: dict = {}  # kind -> monotonic del último push de ese tipo
        self._last_nonpending_push_at: float = 0.0
        self._queue: list = []          # [{key,tipo,texto,detail?,t,queued_at}]
        self._delivered_keys: set = set()
        self._discarded_keys: set = set()

    def reset(self):
        """Reinicia todo el estado (usar al terminar/empezar una reunión nueva)."""
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
        """
        if kind == "pendiente":
            return True
        if get_mode() == "silent":
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_nonpending_push_at) >= _PUSH_BUDGET_SECONDS

    def mark_pushed(self, kind: str, now: float = None):
        """Registra que se empujó una tarjeta de tipo ``kind`` (consume presupuesto
        si no es un pendiente)."""
        now = now if now is not None else time.monotonic()
        self._last_push_at[kind] = now
        if kind != "pendiente":
            self._last_nonpending_push_at = now

    # ------------------------------------------------------------------
    # Cola de espera a lull
    # ------------------------------------------------------------------

    def enqueue(self, card: dict, now: float = None):
        """Encola una tarjeta candidata a entregarse en la próxima pausa natural.

        Dedup por ``key``: si ya se entregó o se descartó esa key, se ignora (nunca
        reaparece). Si ya está en cola, no duplica.
        """
        key = card.get("key")
        if not key:
            return
        if key in self._delivered_keys or key in self._discarded_keys:
            return
        if any(c["key"] == key for c in self._queue):
            return
        now = now if now is not None else time.monotonic()
        entry = dict(card)
        entry["queued_at"] = now
        self._queue.append(entry)

    def pop_deliverable(self, lull: bool, now: float = None) -> "dict | None":
        """Extrae de la cola LA SIGUIENTE tarjeta lista para entregarse (FIFO), o
        None si ninguna lo está todavía.

        Primero purga expiradas (>= _QUEUE_EXPIRE_SECONDS en cola, se descartan sin
        entregarse). Luego, si hay lull o la más vieja lleva >= _LULL_FORCE_AFTER_SECONDS
        esperando, la entrega (queda marcada como entregada — dedup futuro).
        """
        now = now if now is not None else time.monotonic()
        self._purge_expired(now)
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

    def _purge_expired(self, now: float):
        keep = []
        for c in self._queue:
            if now - c["queued_at"] >= _QUEUE_EXPIRE_SECONDS:
                self._discarded_keys.add(c["key"])
            else:
                keep.append(c)
        self._queue = keep

    def queue_size(self) -> int:
        return len(self._queue)

    def delivered(self, key: str) -> bool:
        """¿Esta key ya se entregó al HUD? Lectura pura (unidad 5.1): la usa
        MEETING.status() para exponer al panel web solo las tarjetas que el
        usuario ya vio y aún no tienen feedback ✓/✗."""
        return key in self._delivered_keys


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
