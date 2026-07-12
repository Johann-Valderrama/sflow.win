"""Webhook saliente al generar el acta de una reunión (unidad 6.1, patrón Fireflies).

Al cerrar una reunión y quedar PERSISTIDA con acta, esta capa hace un POST JSON a una
URL configurada por el usuario, firmado con HMAC-SHA256 del body. Es **opt-in** y está
APAGADO por defecto: la regla del proyecto es que nada sale a internet sin una decisión
explícita del usuario (misma filosofía que GROQ_FALLBACK / AUDIO_SOURCE).

Piezas:
  - ``build_payload``      → shape explícito y minimizado según ``WEBHOOK_SCOPE``.
  - ``sign``               → HMAC-SHA256 hexdigest del body (con el secreto DPAPI).
  - ``validate_url``       → anti-SSRF: resuelve el host por DNS y rechaza IPs privadas /
                             loopback / link-local salvo opt-in ``WEBHOOK_ALLOW_LOCAL``.
  - ``send_webhook``       → POST con timeout corto + reintentos con backoff; jamás propaga.
  - ``dispatch_async``     → dispara el POST en un hilo daemon fire-and-forget.
  - ``export_pendientes``  → dead-drop local de los pendientes del acta: contrato de
                             tarea v1 (YAML, ver docs/CONTRATO-MACROSISTEMA.md Parte A).

El transcript crudo NUNCA se incluye en el payload (v1, no negociable). El anti-SSRF valida
la IP resuelta ANTES del POST; ``requests`` re-resuelve en el request (rebinding básico
mitigado — limitación documentada: no se pinnea la IP validada al socket).

## Contrato de tarea v1 (dead-drop → OPS, unidad 5.4)

``export_pendientes`` escribe un archivo por reunión SOLO si hay ≥1 pendiente (O7: una
reunión sin pendientes no genera archivo). El contenido es YAML completo
(``schema_version: 1``, ver ``docs/CONTRATO-MACROSISTEMA.md``) seguido de una vista humana
en markdown. Piezas del contrato:

  - ``get_machine_id``     → id corto (8 hex) estable por instalación, persistido en el
                             data dir de la app (O3: meeting_id es local, no global).
  - ``build_tarea``        → normaliza los pendientes del acta al shape del contrato
                             (id=hash del texto normalizado, due, transcript_offset mm:ss,
                             contexto); ``None`` si no queda ningún pendiente con texto.
  - ``render_tarea_yaml``  → serialización YAML determinista y manual (el schema es fijo
                             y conocido, así que no hace falta una dependencia de YAML
                             genérica; los strings usan comillas dobles vía ``json.dumps``,
                             un subconjunto válido de escalares YAML 1.2).
  - Naming e idempotencia (O3): ``vflow-pendientes-<instalacion>-<meeting_id>-<hash8>.md``,
    escritura ``open(path, "x")`` (create-only). Acta cambiada → hash distinto → archivo
    NUEVO; el viejo queda intacto. Vflow nunca edita ni borra archivos del drop.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

from config import APP_DATA_DIR

logger = logging.getLogger(__name__)

# Headers del POST (patrón Fireflies-like).
_EVENT_NAME = "meeting.minutes"
_SIG_HEADER = "X-Vflow-Signature"
_EVENT_HEADER = "X-Vflow-Event"

# Parámetros de red del envío fire-and-forget.
_TIMEOUT_SECONDS = 10          # timeout corto: un receptor lento no cuelga el hilo
_MAX_ATTEMPTS = 3              # intentos totales (1 + 2 reintentos)
_BACKOFF_BASE_SECONDS = 1.0    # backoff exponencial: 1s, 2s, 4s...


class WebhookError(Exception):
    """Error de validación/envío del webhook. Nunca se propaga fuera del hilo daemon."""


# ---------------------------------------------------------------------------
# Lectura de configuración (perezosa: se lee en cada uso, se apaga sin reiniciar)
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return os.getenv("WEBHOOK_ENABLED", "false").strip().lower() == "true"


def _allow_local() -> bool:
    return os.getenv("WEBHOOK_ALLOW_LOCAL", "false").strip().lower() == "true"


def _scope() -> str:
    val = (os.getenv("WEBHOOK_SCOPE", "pendientes") or "pendientes").strip().lower()
    return val if val in ("pendientes", "acta") else "pendientes"


def _secret() -> str:
    """Secreto de firma en runtime. config.py descifra WEBHOOK_SECRET_ENC (DPAPI) al
    arrancar; aquí solo se lee la variable ya en claro en el entorno del proceso."""
    return os.getenv("WEBHOOK_SECRET", "") or ""


# ---------------------------------------------------------------------------
# Firma HMAC-SHA256
# ---------------------------------------------------------------------------

def sign(body: bytes, secret: str) -> str:
    """Devuelve ``sha256=<hexdigest>`` del HMAC-SHA256 de *body* con *secret*.

    El formato del header replica el de Fireflies/GitHub: prefijo ``sha256=`` seguido
    del hexdigest. Con secreto vacío igual firma (útil si el receptor no valida), pero
    la UI recomienda configurar uno.
    """
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


# ---------------------------------------------------------------------------
# Anti-SSRF: validación de URL / IP resuelta
# ---------------------------------------------------------------------------

def _ip_is_blocked(ip: str) -> bool:
    """True si la IP es loopback, privada, link-local, reservada o no global."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # no parseable → bloquear por seguridad
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    )


def validate_url(url: str, *, allow_local: bool = False) -> str:
    """Valida el esquema y resuelve el host por DNS, rechazando destinos internos.

    Reglas (anti-SSRF, O6):
      - Esquema: solo ``https://`` por defecto; ``http://`` solo con *allow_local*.
      - Resuelve el hostname a IP(s) y rechaza loopback / privadas (10/8, 172.16/12,
        192.168/16) / link-local (169.254/16, fe80::/10) / ::1 / reservadas, SALVO
        que *allow_local* sea True (opt-in explícito para integrar en LAN, con riesgo
        documentado).

    Devuelve la primera IP validada (para conectar a ella y mitigar rebinding básico).
    Lanza WebhookError si el destino no es aceptable.
    """
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()

    if scheme not in ("http", "https"):
        raise WebhookError(f"esquema no soportado: {scheme!r} (usa https)")
    if scheme == "http" and not allow_local:
        raise WebhookError("http:// solo se permite con WEBHOOK_ALLOW_LOCAL=true")

    host = parsed.hostname
    if not host:
        raise WebhookError("URL sin hostname")

    # Resolver TODAS las IPs del host; si alguna es interna y no hay opt-in → bloquear.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebhookError(f"no se pudo resolver el host {host!r}: {exc}") from exc

    resolved = []
    for info in infos:
        ip = info[4][0]
        resolved.append(ip)
        if not allow_local and _ip_is_blocked(ip):
            raise WebhookError(
                f"destino interno bloqueado: {host} → {ip} "
                "(activa WEBHOOK_ALLOW_LOCAL=true para permitirlo, bajo tu riesgo)"
            )

    if not resolved:
        raise WebhookError(f"host {host!r} no resolvió a ninguna IP")
    return resolved[0]


# ---------------------------------------------------------------------------
# Construcción del payload (minimizado por scope)
# ---------------------------------------------------------------------------

def _load_json(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else default
    except Exception:  # noqa: BLE001
        return default


def build_payload(meeting: dict, scope: str) -> dict:
    """Construye el payload JSON según *scope*.

    ``scope="pendientes"`` (default): solo metadatos + lista de pendientes.
    ``scope="acta"``: además el minutes_json completo + capítulos. NUNCA el transcript
    crudo (v1, no negociable) — ni siquiera en scope="acta".
    """
    minutes = _load_json(meeting.get("minutes_json"), {})
    insights = _load_json(meeting.get("insights_json"), {})
    pendientes = minutes.get("pendientes") or insights.get("pendientes") or []

    payload = {
        "event": _EVENT_NAME,
        "scope": scope,
        "meeting": {
            "id": meeting.get("id"),
            "title": meeting.get("title") or f"Reunión {(meeting.get('started_at') or '')[:10]}".strip(),
            "started_at": meeting.get("started_at") or meeting.get("created_at"),
            "duration_seconds": meeting.get("duration_seconds") or 0,
        },
        "pendientes": pendientes,
    }

    if scope == "acta":
        # Acta completa (minutes) + capítulos. Sin transcript.
        payload["minutes"] = minutes
        payload["chapters"] = _load_json(meeting.get("chapters_json"), [])

    return payload


# ---------------------------------------------------------------------------
# Envío HTTP con reintentos
# ---------------------------------------------------------------------------

def send_webhook(url: str, payload: dict, secret: str, *, allow_local: bool = False,
                 max_attempts: int = _MAX_ATTEMPTS, timeout: float = _TIMEOUT_SECONDS) -> bool:
    """POST del *payload* firmado a *url*. Reintenta con backoff exponencial.

    Devuelve True si algún intento recibió 2xx. NUNCA lanza: cualquier fallo (red,
    validación, timeout) se loguea y devuelve False. Pensado para correr en un hilo
    daemon: un fallo jamás debe propagar a stop().
    """
    import requests  # import perezoso: solo cuando de verdad se envía

    try:
        validated_ip = validate_url(url, allow_local=allow_local)
    except WebhookError as exc:
        logger.warning("Webhook no enviado (URL inválida): %s", exc)
        return False

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        _EVENT_HEADER: _EVENT_NAME,
        _SIG_HEADER: sign(body, secret),
    }

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Re-validar en cada intento mitiga rebinding entre reintentos (limitación:
            # requests re-resuelve el host; no pinneamos validated_ip al socket).
            if attempt > 1:
                validate_url(url, allow_local=allow_local)
            # allow_redirects=False (F3): un receptor comprometido podría responder
            # 307/308 hacia una IP privada/loopback y burlar el anti-SSRF de
            # validate_url(), que solo valida la URL original — nunca la de un
            # redirect. No seguimos redirects, punto.
            resp = requests.post(url, data=body, headers=headers, timeout=timeout,
                                 allow_redirects=False)
            if 200 <= resp.status_code < 300:
                logger.info("Webhook entregado (%s) intento %d.", resp.status_code, attempt)
                return True
            if 300 <= resp.status_code < 400:
                # Fallo TERMINAL, no transitorio: no se sigue el redirect por
                # seguridad y no se gastan los reintentos restantes.
                logger.error(
                    "Webhook: el receptor respondió una redirección (HTTP %s); "
                    "no se sigue por seguridad y no se reintenta.",
                    resp.status_code,
                )
                return False
            last_err = f"HTTP {resp.status_code}"
            logger.warning("Webhook intento %d/%d devolvió %s.", attempt, max_attempts, last_err)
        except Exception as exc:  # noqa: BLE001  (red, timeout, validación, etc.)
            last_err = str(exc)
            logger.warning("Webhook intento %d/%d falló: %s", attempt, max_attempts, last_err)

        if attempt < max_attempts:
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    logger.error("Webhook no entregado tras %d intentos (último error: %s).", max_attempts, last_err)
    return False


# ---------------------------------------------------------------------------
# Dead-drop local: pendientes → OPS (contrato de tarea v1, unidad 5.4)
# ---------------------------------------------------------------------------

_MACHINE_ID_FILENAME = "machine_id.txt"
_ID_HASH_LEN = 8       # id de cada pendiente (hash del texto normalizado)
_CONTENT_HASH_LEN = 8  # hash del contenido completo, usado en el nombre de archivo


def get_machine_id() -> str:
    """Id corto (8 hex) estable por instalación (O3: ``meeting_id`` es local a la DB
    de esta máquina, no un id global — el consumidor OPS necesita distinguir de qué
    instalación viene cada tarea).

    Se genera una sola vez (``os.urandom``) y se persiste en un archivo plano dentro
    del data dir de la app (no es un secreto: no usa DPAPI). Lecturas posteriores lo
    releen del archivo, así que es estable entre reinicios y entre llamadas.

    Best-effort: si no se puede leer/escribir el archivo (permisos, disco, etc.), cae
    a un id determinista derivado del propio data dir (mismo valor en la misma
    máquina, aunque no persista) y loguea un warning — nunca lanza.
    """
    path = os.path.join(APP_DATA_DIR, _MACHINE_ID_FILENAME)
    try:
        if os.path.exists(path):
            existing = open(path, "r", encoding="utf-8").read().strip()
            if existing:
                return existing
    except Exception as exc:  # noqa: BLE001
        logger.warning("machine_id: no se pudo leer %s (%s).", path, exc)

    new_id = os.urandom(4).hex()
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_id)
        return new_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "machine_id: no se pudo persistir en %s (%s); usando fallback determinista "
            "(cambiará solo si el data dir cambia).", path, exc,
        )
        return hashlib.sha256(APP_DATA_DIR.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]


def _normalize_texto_para_hash(texto: str) -> str:
    """Minúsculas + espacios colapsados. Solo absorbe diferencias triviales de
    espaciado/mayúsculas entre re-exports del MISMO pendiente (a diferencia del
    dedup de detecciones de core/meeting.py, aquí no se quita puntuación: el id
    debe ser estable, no fusionar textos distintos)."""
    return re.sub(r"\s+", " ", str(texto or "").strip().lower())


def _pendiente_id(texto: str) -> str:
    norm = _normalize_texto_para_hash(texto)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]


def _mmss(seconds) -> "str | None":
    """Convierte segundos (float/int, como los que guarda ``core/insights.py`` en la
    clave ``t``) a ``mm:ss``. ``None`` si no es numérico o es negativo."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _normalize_pendiente(raw) -> "dict | None":
    """Normaliza un pendiente crudo del acta (string plano o dict de
    ``core/insights.py``: ``{texto, responsable, fecha, hora, t?}``) al shape del
    contrato v1. Devuelve ``None`` si el texto queda vacío tras limpiar (se descarta:
    nunca se cuenta como pendiente real)."""
    if isinstance(raw, str):
        texto = raw.strip()
        responsable = fecha = hora = offset = contexto = None
    elif isinstance(raw, dict):
        texto = str(raw.get("texto") or raw.get("text") or "").strip()
        responsable = raw.get("responsable") or None
        fecha = raw.get("fecha") or None
        hora = raw.get("hora") or None
        offset = _mmss(raw.get("t"))
        contexto = raw.get("contexto") or None
    else:
        return None
    if not texto:
        return None
    return {
        "id": _pendiente_id(texto),
        "texto": texto,
        "responsable": responsable,
        "due": {"fecha": fecha, "hora": hora},
        "transcript_offset": offset,
        "contexto": contexto,
    }


def _collect_pendientes(meeting: dict) -> list:
    minutes = _load_json(meeting.get("minutes_json"), {})
    insights = _load_json(meeting.get("insights_json"), {})
    raw = minutes.get("pendientes") or insights.get("pendientes") or []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        norm = _normalize_pendiente(item)
        if norm is not None:
            out.append(norm)
    return out


def build_tarea(meeting: dict, machine_id: str) -> "dict | None":
    """Construye el payload de la tarea v1 (contrato Parte A / O2).

    Devuelve ``None`` si la reunión no tiene ningún pendiente con texto (gate de
    emisión O7: sin pendientes, no se escribe archivo).
    """
    pendientes = _collect_pendientes(meeting)
    if not pendientes:
        return None

    started = meeting.get("started_at") or meeting.get("created_at") or ""
    fecha_reunion = (started or "")[:10] or None
    title = meeting.get("title") or f"Reunión {fecha_reunion or 'sin fecha'}".strip()

    return {
        "tipo": "tarea-vflow",
        "schema_version": 1,
        "origen": "vflow-meeting",
        "instalacion": machine_id,
        "meeting_id": meeting.get("id"),
        "fecha_reunion": fecha_reunion,
        "titulo_reunion": title,
        "generado_en": time.strftime("%Y-%m-%d %H:%M"),  # hora local
        "pendientes": pendientes,
    }


def _yaml_scalar(value) -> str:
    """Un escalar YAML para *value*. Los strings usan comillas dobles vía
    ``json.dumps``: el subconjunto de escalares string de JSON es YAML 1.2 válido,
    así que cualquier texto con comillas/dos puntos/saltos de línea/unicode queda
    correctamente escapado sin necesitar un serializador YAML genérico."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_tarea_yaml(tarea: dict) -> str:
    """Serialización YAML determinista y manual del contrato v1.

    El schema es fijo y conocido (ver docstring del módulo y
    ``docs/CONTRATO-MACROSISTEMA.md`` Parte A), así que no hace falta una
    dependencia de YAML genérica (PyYAML es solo transitiva en este repo, no un
    requirement directo — no se agrega como dependencia nueva).
    """
    due_scalar = lambda due: (  # noqa: E731
        f"{{fecha: {_yaml_scalar((due or {}).get('fecha'))}, "
        f"hora: {_yaml_scalar((due or {}).get('hora'))}}}"
    )
    lines = [
        "tipo: tarea-vflow",
        "schema_version: 1",
        "origen: vflow-meeting",
        f"instalacion: {_yaml_scalar(tarea['instalacion'])}",
        f"meeting_id: {_yaml_scalar(tarea['meeting_id'])}",
        f"fecha_reunion: {_yaml_scalar(tarea['fecha_reunion'])}",
        f"titulo_reunion: {_yaml_scalar(tarea['titulo_reunion'])}",
        f"generado_en: {_yaml_scalar(tarea['generado_en'])}",
        "pendientes:",
    ]
    for p in tarea["pendientes"]:
        lines.append(f"  - id: {_yaml_scalar(p['id'])}")
        lines.append(f"    texto: {_yaml_scalar(p['texto'])}")
        lines.append(f"    responsable: {_yaml_scalar(p['responsable'])}")
        lines.append(f"    due: {due_scalar(p['due'])}")
        lines.append(f"    transcript_offset: {_yaml_scalar(p['transcript_offset'])}")
        lines.append(f"    contexto: {_yaml_scalar(p['contexto'])}")
    return "\n".join(lines) + "\n"


def render_tarea_markdown(tarea: dict) -> str:
    """Vista humana render-friendly, DEBAJO del YAML en el mismo archivo (el contrato
    es el YAML; esto es cortesía para quien abra el .md a simple vista)."""
    lines = [
        f"# Pendientes — {tarea['titulo_reunion']}",
        "",
        f"Reunión #{tarea['meeting_id']} · {tarea['fecha_reunion'] or 'sin fecha'}",
        "",
    ]
    for p in tarea["pendientes"]:
        meta = []
        if p.get("responsable"):
            meta.append(f"@{p['responsable']}")
        due = p.get("due") or {}
        fh = " ".join(x for x in (due.get("fecha"), due.get("hora")) if x)
        if fh:
            meta.append(f"📅 {fh}")
        if p.get("transcript_offset"):
            meta.append(f"⏱ {p['transcript_offset']}")
        suffix = f" ({' · '.join(meta)})" if meta else ""
        lines.append(f"- [ ] {p['texto']}{suffix}")
    lines.append("")
    return "\n".join(lines)


def export_pendientes(meeting: dict, export_dir: str) -> "str | None":
    """Escribe la tarea v1 (YAML + vista humana) a
    ``<export_dir>/vflow-pendientes-<instalacion>-<meeting_id>-<hash8>.md``.

    Dead-drop LOCAL: no requiere anti-SSRF. Contrato (docs/CONTRATO-MACROSISTEMA.md
    Parte A):
      - *export_dir* vacío → no hace nada (feature apagada).
      - ``meeting.get("id")`` None (reunión no persistida, p. ej. SAVE_HISTORY=false)
        → no hace nada. Vflow NUNCA escribe pendientes de una reunión sin persistir.
      - Sin pendientes (O7) → no se escribe archivo.
      - Create-only (O3): si el archivo con ese nombre exacto ya existe (mismo
        contenido, por definición del hash) → no-op silencioso. Vflow nunca edita
        ni sobrescribe un archivo del drop. Acta cambiada → hash distinto → archivo
        nuevo; el viejo queda intacto.

    Devuelve la ruta del archivo (nuevo o preexistente), o None si no aplica / falla.
    Fail-open: cualquier excepción se loguea y jamás propaga (llamado desde un hilo
    daemon fire-and-forget).
    """
    export_dir = (export_dir or "").strip()
    if not export_dir:
        return None
    if meeting.get("id") is None:
        return None
    try:
        # Gate O7 ANTES de resolver machine_id: get_machine_id() persiste un archivo
        # en el data dir la primera vez, y ese side effect no debe ocurrir cuando la
        # reunión no va a generar export alguno.
        if not _collect_pendientes(meeting):
            logger.debug(
                "Export de pendientes: reunión #%s sin pendientes, no se escribe archivo (O7).",
                meeting.get("id"),
            )
            return None
        machine_id = get_machine_id()
        tarea = build_tarea(meeting, machine_id)
        if tarea is None:
            return None

        yaml_text = render_tarea_yaml(tarea)
        content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:_CONTENT_HASH_LEN]
        fname = f"vflow-pendientes-{machine_id}-{tarea['meeting_id']}-{content_hash}.md"

        os.makedirs(export_dir, exist_ok=True)
        path = os.path.join(export_dir, fname)

        if os.path.exists(path):
            logger.debug("Export de pendientes: %s ya existe (mismo contenido), no-op.", path)
            return path

        body = yaml_text + "\n---\n\n" + render_tarea_markdown(tarea)
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(body)
        except FileExistsError:
            # Carrera entre el exists() de arriba y este open("x"): por definición
            # del hash del nombre, el contenido ya escrito es idéntico. No-op.
            logger.debug("Export de pendientes: %s creado por una escritura concurrente.", path)
            return path

        logger.info("Pendientes exportados a %s.", path)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Export de pendientes (dead-drop) falló: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Orquestación: disparo fire-and-forget desde meeting.stop()
# ---------------------------------------------------------------------------

def _dispatch_worker(meeting: dict):
    """Trabajo del hilo daemon: dead-drop local + POST del webhook. Aísla todo error."""
    try:
        export_pendientes(meeting, os.getenv("PENDING_EXPORT_DIR", ""))
    except Exception as exc:  # noqa: BLE001  (defensa extra; export_pendientes ya no lanza)
        logger.warning("Dead-drop de pendientes falló en el hilo: %s", exc)

    try:
        if not _enabled():
            return
        url = (os.getenv("WEBHOOK_URL", "") or "").strip()
        if not url:
            logger.warning("Webhook activado pero WEBHOOK_URL vacío: no se dispara.")
            return
        payload = build_payload(meeting, _scope())
        send_webhook(url, payload, _secret(), allow_local=_allow_local())
    except Exception as exc:  # noqa: BLE001  (el hilo muere solo, jamás propaga)
        logger.error("Webhook: error inesperado en el hilo (ignorado): %s", exc)


def dispatch_async(meeting: dict, meeting_id) -> bool:
    """Dispara el webhook + dead-drop en un hilo daemon fire-and-forget.

    Contrato (O5, O8):
      - Si *meeting_id* es None (SAVE_HISTORY=false o insert fallido): NO envía nada,
        loguea un warning claro y devuelve False. La UI ya advierte que con historial
        desactivado el webhook nunca dispara.
      - Un fallo de red JAMÁS propaga a stop() ni bloquea: todo corre en el hilo daemon.

    Devuelve True si se lanzó el hilo, False si se saltó (sin persistir).
    """
    if meeting_id is None:
        logger.warning("Webhook no disparado: reunión no persistida (meeting_id=None).")
        return False

    # Snapshot inmutable de los datos que el hilo necesita (no compartir estado vivo).
    snapshot = dict(meeting)
    snapshot.setdefault("id", meeting_id)

    t = threading.Thread(target=_dispatch_worker, args=(snapshot,),
                         name="vflow-webhook", daemon=True)
    t.start()
    return True
