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
  - ``export_pendientes``  → dead-drop local de los pendientes del acta a un .md.

El transcript crudo NUNCA se incluye en el payload (v1, no negociable). El anti-SSRF valida
la IP resuelta ANTES del POST; ``requests`` re-resuelve en el request (rebinding básico
mitigado — limitación documentada: no se pinnea la IP validada al socket).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from urllib.parse import urlparse

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
# Dead-drop local: pendientes → OPS (markdown)
# ---------------------------------------------------------------------------

def _fmt_pendiente_md(p) -> str:
    """Un pendiente como checkbox markdown. Acepta string o dict {texto, responsable, ...}."""
    if isinstance(p, str):
        return f"- [ ] {p}"
    if not isinstance(p, dict):
        return f"- [ ] {p}"
    meta = []
    if p.get("responsable"):
        meta.append(f"@{p['responsable']}")
    fh = " ".join(x for x in (p.get("fecha"), p.get("hora")) if x)
    if fh:
        meta.append(f"📅 {fh}")
    suffix = f" ({' · '.join(meta)})" if meta else ""
    return f"- [ ] {p.get('texto', '')}{suffix}"


def export_pendientes(meeting: dict, export_dir: str) -> "str | None":
    """Escribe los pendientes del acta a ``<export_dir>/vflow-pendientes-<id>-<fecha>.md``.

    Dead-drop LOCAL: no requiere anti-SSRF. Si *export_dir* está vacío → no hace nada
    (feature apagada). Devuelve la ruta escrita, o None si no aplica / falla.
    """
    export_dir = (export_dir or "").strip()
    if not export_dir:
        return None
    try:
        minutes = _load_json(meeting.get("minutes_json"), {})
        insights = _load_json(meeting.get("insights_json"), {})
        pendientes = minutes.get("pendientes") or insights.get("pendientes") or []

        mid = meeting.get("id", "x")
        started = meeting.get("started_at") or meeting.get("created_at") or ""
        date = (started or "")[:10] or "sin-fecha"
        title = meeting.get("title") or f"Reunión {date}".strip()

        os.makedirs(export_dir, exist_ok=True)
        fname = f"vflow-pendientes-{mid}-{date}.md"
        path = os.path.join(export_dir, fname)

        lines = [f"# Pendientes — {title}", "", f"Reunión #{mid} · {date}", ""]
        if pendientes:
            lines += [_fmt_pendiente_md(p) for p in pendientes]
        else:
            lines.append("_(sin pendientes registrados)_")
        lines.append("")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
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
