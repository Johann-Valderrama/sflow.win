"""Blueprint de ajustes: API keys, configuración general, micrófonos y modelo local."""

import os
import threading

from flask import Blueprint, jsonify, request

from core import dictation_modes as _dictation_modes
from core import insights as _insights
from core import ops_briefing as _ops_briefing
from core import proactive as _proactive
from web.state import (
    _SECRET_KEYS,
    _download_lock,
    _download_state,
    _safe_int_env,
    _save_secret_key,
    _set_env_key,
    _validate_briefing_path,
    _validate_export_dir,
    _validate_meeting_retention_days,
)

bp = Blueprint("settings", __name__)

_SETTINGS_VALIDATORS = {
    "pending_export_dir": _validate_export_dir,
    "ops_briefing_path": _validate_briefing_path,
    "meeting_retention_days": _validate_meeting_retention_days,
}


@bp.route("/api/keys")
def get_api_keys():
    """Estado de cada API key (configurada o no), sin exponer nunca el valor."""
    return jsonify({p: bool(os.getenv(env, "").strip()) for p, env in _SECRET_KEYS.items()})


@bp.route("/api/keys", methods=["POST"])
def set_api_keys():
    """Guarda una o varias API keys cifradas. Body JSON: {groq?: "...", openrouter?: "..."}."""
    data = request.get_json(silent=True) or {}
    saved = []
    for provider in _SECRET_KEYS:
        val = data.get(provider)
        if isinstance(val, str) and val.strip() and _save_secret_key(provider, val):
            saved.append(provider)
    if not saved:
        return jsonify({"error": "No se recibió ninguna API key válida."}), 400
    status = {p: bool(os.getenv(env, "").strip()) for p, env in _SECRET_KEYS.items()}
    return jsonify({"ok": True, "saved": saved, "status": status})


@bp.route("/api/settings")
def get_settings():
    """Devuelve la configuración actual (idioma, micrófono, sonidos, backend)."""
    return jsonify({
        "language": os.getenv("WHISPER_LANGUAGE", "es"),
        "translate_target": os.getenv("TRANSLATE_TARGET_LANG", "en"),
        "device_name": os.getenv("AUDIO_DEVICE_NAME", ""),
        "sounds_enabled": os.getenv("SOUNDS_ENABLED", "true") == "true",
        "beep_volume": int(os.getenv("BEEP_VOLUME_STEPS", "2")),
        "save_history": os.getenv("SAVE_HISTORY", "true").lower() == "true",
        "retention_days": int(os.getenv("HISTORY_RETENTION_DAYS", "0") or 0),
        "transcription_backend": os.getenv("TRANSCRIPTION_BACKEND", "groq"),
        "local_whisper_model": os.getenv("LOCAL_WHISPER_MODEL", "small"),
        "groq_fallback": os.getenv("GROQ_FALLBACK", "false").lower() == "true",
        "audio_source": os.getenv("AUDIO_SOURCE", "mic"),
        "proactive_mode": _proactive.get_mode(),
        "insights_backend": os.getenv("INSIGHTS_BACKEND", "groq"),
        "insights_backend_live": (os.getenv("INSIGHTS_BACKEND_LIVE", "").strip().lower()
                                   or os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
                                   or "groq"),
        "insights_backend_batch": (os.getenv("INSIGHTS_BACKEND_BATCH", "").strip().lower()
                                    or os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
                                    or "groq"),
        "insights_endpoint_model": os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b"),
        "anthropic_model_live": os.getenv("ANTHROPIC_MODEL_LIVE", "claude-haiku-4-5"),
        "anthropic_model_batch": os.getenv("ANTHROPIC_MODEL_BATCH", "claude-sonnet-5"),
        "claude_cli_model_batch": os.getenv("CLAUDE_CLI_MODEL_BATCH", "sonnet"),
        "insights_fallback": os.getenv("INSIGHTS_FALLBACK", "true").strip().lower() == "true",
        "claude_cli_available": _insights._claude_cli_path() is not None,
        "has_groq_key": bool(os.getenv("GROQ_API_KEY", "").strip()),
        "has_openrouter_key": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        # Webhook saliente (unidad 6.1). El secreto NUNCA se devuelve: solo un booleano.
        "webhook_enabled": os.getenv("WEBHOOK_ENABLED", "false").strip().lower() == "true",
        "webhook_url": os.getenv("WEBHOOK_URL", ""),
        "webhook_scope": (os.getenv("WEBHOOK_SCOPE", "pendientes").strip().lower() or "pendientes"),
        "webhook_allow_local": os.getenv("WEBHOOK_ALLOW_LOCAL", "false").strip().lower() == "true",
        "has_webhook_secret": bool(os.getenv("WEBHOOK_SECRET", "").strip()),
        "pending_export_dir": os.getenv("PENDING_EXPORT_DIR", ""),
        # Modos de dictado por app activa (unidad 6.3)
        "dictation_modes_enabled": os.getenv("DICTATION_MODES_ENABLED", "false").strip().lower() == "true",
        "dictation_mode_map": os.getenv("DICTATION_MODE_MAP", _dictation_modes.DEFAULT_MODE_MAP),
        # Copiloto con contexto OPS — briefing v1 (unidad 7.1)
        "ops_briefing_path": os.getenv("OPS_BRIEFING_PATH", ""),
        # Identidad del usuario en prompts (unidad 5.2)
        "user_name": os.getenv("USER_NAME", ""),
        "user_role": os.getenv("USER_ROLE", ""),
        "user_domain": os.getenv("USER_DOMAIN", ""),
        # Retención opcional de reuniones (unidad 3.2). Default 0 = conservar
        # siempre; es la única operación destructiva de este plan, por eso
        # _safe_int_env nunca deja que un valor corrupto tumbe este GET.
        "meeting_retention_days": _safe_int_env("MEETING_RETENTION_DAYS", 0),
    })


@bp.route("/api/settings", methods=["POST"])
def update_settings():
    """Guarda configuración en .env y actualiza el proceso en ejecución."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    allowed = {
        "language": "WHISPER_LANGUAGE",
        "translate_target": "TRANSLATE_TARGET_LANG",
        "device_name": "AUDIO_DEVICE_NAME",
        "sounds_enabled": "SOUNDS_ENABLED",
        "beep_volume": "BEEP_VOLUME_STEPS",
        "save_history": "SAVE_HISTORY",
        "retention_days": "HISTORY_RETENTION_DAYS",
        "transcription_backend": "TRANSCRIPTION_BACKEND",
        "local_whisper_model": "LOCAL_WHISPER_MODEL",
        "groq_fallback": "GROQ_FALLBACK",
        "proactive_mode": "PROACTIVE_MODE",
        "insights_backend": "INSIGHTS_BACKEND",
        "insights_backend_live": "INSIGHTS_BACKEND_LIVE",
        "insights_backend_batch": "INSIGHTS_BACKEND_BATCH",
        "insights_fallback": "INSIGHTS_FALLBACK",
        "insights_endpoint_model": "INSIGHTS_ENDPOINT_MODEL",
        "audio_source": "AUDIO_SOURCE",
        "anthropic_model_live": "ANTHROPIC_MODEL_LIVE",
        "anthropic_model_batch": "ANTHROPIC_MODEL_BATCH",
        "claude_cli_model_batch": "CLAUDE_CLI_MODEL_BATCH",
        # Webhook saliente (unidad 6.1). El secreto NO va aquí: se guarda cifrado por
        # /api/keys (write-only). Estas son las opciones no-secretas del webhook.
        "webhook_enabled": "WEBHOOK_ENABLED",
        "webhook_url": "WEBHOOK_URL",
        "webhook_scope": "WEBHOOK_SCOPE",
        "webhook_allow_local": "WEBHOOK_ALLOW_LOCAL",
        "pending_export_dir": "PENDING_EXPORT_DIR",
        # Modos de dictado por app activa (unidad 6.3)
        "dictation_modes_enabled": "DICTATION_MODES_ENABLED",
        "dictation_mode_map": "DICTATION_MODE_MAP",
        # Copiloto con contexto OPS — briefing v1 (unidad 7.1)
        "ops_briefing_path": "OPS_BRIEFING_PATH",
        # Identidad del usuario en prompts (unidad 5.2)
        "user_name": "USER_NAME",
        "user_role": "USER_ROLE",
        "user_domain": "USER_DOMAIN",
        # Retención opcional de reuniones (unidad 3.2)
        "meeting_retention_days": "MEETING_RETENTION_DAYS",
    }
    errors: dict[str, str] = {}
    for field, env_key in allowed.items():
        if field not in data:
            continue
        value = str(data[field]).strip()
        validator = _SETTINGS_VALIDATORS.get(field)
        if validator is not None:
            err = validator(value)
            if err:
                errors[field] = err
                continue  # campo rechazado: no se persiste; los demás siguen su curso
        _set_env_key(env_key, value)

    # Si cambió la ruta del briefing (y fue válida), invalidar la caché para que
    # se vea sin esperar el TTL de 60s (core/ops_briefing.py).
    if "ops_briefing_path" in data and "ops_briefing_path" not in errors:
        _ops_briefing.invalidate()

    # Si se activó el backend local y el modelo está descargado, disparar warmup
    if data.get("transcription_backend") == "local":
        _trigger_local_warmup_if_ready()

    if errors:
        return jsonify({"error": errors}), 400

    return jsonify({"ok": True})


@bp.route("/api/microphones")
def get_microphones():
    """Lista los dispositivos de entrada de audio (excluye salidas/speakers)."""
    import sounddevice as sd
    mics = [
        {"index": i, "name": dev["name"]}
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0 and dev["max_output_channels"] == 0
    ]
    return jsonify(mics)


def _trigger_local_warmup_if_ready() -> None:
    """Lanza warmup del backend local en un thread de fondo si el modelo está descargado.

    Usa el singleton de ``get_backend('local')`` para precalentar la misma
    instancia que usa ``Transcriber``, de modo que el warmup sea efectivo.
    """
    def _do_warmup():
        try:
            from core.backends import get_backend  # noqa: PLC0415
            b = get_backend("local")
            if b.is_ready():
                b.warmup()
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("Warmup del backend local fallido: %s", exc)

    threading.Thread(target=_do_warmup, daemon=True).start()


def _run_model_download(model_name: str) -> None:
    """Descarga el modelo faster-whisper en un thread de fondo.

    Actualiza ``_download_state`` con progreso (basado en heurística de tiempo
    si huggingface_hub no reporta progreso granular) y estado final.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    with _download_lock:
        _download_state["downloading"] = True
        _download_state["model"] = model_name
        _download_state["progress"] = 0.0
        _download_state["error"] = None

    try:
        from core.backends.local_backend import _get_models_dir  # noqa: PLC0415
        from faster_whisper import WhisperModel                   # noqa: PLC0415

        models_dir = _get_models_dir()
        os.makedirs(models_dir, exist_ok=True)

        _logger.info("Iniciando descarga del modelo '%s' en '%s'", model_name, models_dir)

        # faster-whisper descarga automáticamente si el modelo no está en download_root.
        # No expone progreso granular, así que usamos una heurística de tiempo.
        # El progreso se simula de 0→0.9 durante la descarga.
        _progress_stop = threading.Event()

        def _fake_progress():
            start = __import__("time").time()
            # Tamaños aproximados: small~466MB, medium~1.5GB.  Asumimos ~5 MB/s.
            sizes = {"small": 466, "medium": 1500}
            mb = sizes.get(model_name, 500)
            total_est = mb / 5.0  # segundos estimados
            while not _progress_stop.is_set():
                elapsed = __import__("time").time() - start
                prog = min(0.9, elapsed / max(total_est, 1))
                with _download_lock:
                    _download_state["progress"] = prog
                __import__("time").sleep(1)

        prog_thread = threading.Thread(target=_fake_progress, daemon=True)
        prog_thread.start()

        try:
            # Cargar el modelo fuerza la descarga si no existe
            WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                download_root=models_dir,
            )
        finally:
            _progress_stop.set()

        with _download_lock:
            _download_state["progress"] = 1.0
            _download_state["downloading"] = False
            _download_state["error"] = None
        _logger.info("Descarga del modelo '%s' completada", model_name)

    except Exception as exc:
        _logger.error("Error durante descarga del modelo '%s': %s", model_name, exc)
        with _download_lock:
            _download_state["downloading"] = False
            _download_state["progress"] = None
            _download_state["error"] = str(exc)


@bp.route("/api/local-model/status")
def local_model_status():
    """Devuelve el estado del modelo local: descargado, descargando, progreso, error.

    Si ``_download_state`` corresponde a un modelo distinto del configurado
    actualmente (``LOCAL_WHISPER_MODEL``), no se exponen progress ni error de
    esa descarga para evitar mostrar información obsoleta.
    """
    model_name = os.getenv("LOCAL_WHISPER_MODEL", "small")
    try:
        from core.backends.local_backend import _is_model_downloaded  # noqa: PLC0415
        downloaded = _is_model_downloaded(model_name)
    except Exception:
        downloaded = False

    with _download_lock:
        state = dict(_download_state)

    # Si el estado de descarga pertenece a otro modelo, ignorarlo.
    state_for_current = state.get("model") == model_name or state.get("model") is None
    if not state_for_current:
        state = {"downloading": False, "model": model_name, "progress": None, "error": None}

    return jsonify({
        "model": model_name,
        "downloaded": downloaded,
        "downloading": state["downloading"] if state_for_current else False,
        "progress": state["progress"] if state_for_current else None,
        "error": state["error"] if state_for_current else None,
    })


@bp.route("/api/local-model/download", methods=["POST"])
def local_model_download():
    """Inicia la descarga del modelo local en un thread de fondo."""
    data = request.get_json() or {}
    model_name = data.get("model", os.getenv("LOCAL_WHISPER_MODEL", "small")).strip().lower()
    if model_name not in ("small", "medium"):
        return jsonify({"error": "Modelo no soportado; usa 'small' o 'medium'"}), 400

    with _download_lock:
        if _download_state["downloading"]:
            return jsonify({"ok": True, "message": "Descarga ya en curso"})

    # Actualizar la env si cambió el modelo seleccionado
    _set_env_key("LOCAL_WHISPER_MODEL", model_name)

    thread = threading.Thread(target=_run_model_download, args=(model_name,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "model": model_name})
