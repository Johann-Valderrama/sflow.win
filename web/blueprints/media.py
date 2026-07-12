"""Blueprint de transcripción de medios externos: cookies de Instagram y YouTube."""

import os
import re

from flask import Blueprint, jsonify, request

from web.state import _db

bp = Blueprint("media", __name__)

_YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)",
    re.IGNORECASE,
)


@bp.route("/api/instagram-cookies/sync", methods=["POST"])
def instagram_cookies_sync():
    """Extrae las cookies de Instagram del navegador y las guarda cifradas (DPAPI).

    Fallback duradero: una vez guardadas, la transcripción de Instagram funciona
    aunque el navegador esté cerrado. Responde {ok, browser, count, error}.
    """
    from core.url_transcribe import sync_instagram_cookies  # noqa: PLC0415
    r = sync_instagram_cookies()
    status = 200 if r.get("ok") else 400
    return jsonify({"ok": r.get("ok", False), "browser": r.get("browser"),
                    "count": r.get("count", 0), "error": r.get("error")}), status


@bp.route("/api/youtube-transcript", methods=["POST"])
def youtube_transcript():
    """Transcribe contenido de YouTube (subtítulos o audio) y lo devuelve como texto plano.

    Delega en core.url_transcribe.transcribe_url y persiste en DB si SAVE_HISTORY=true.

    Body JSON: {url: str}
    Responde: {ok, title, language, auto_generated, text}
    Errores: 400 URL inválida, 401 requiere auth, 404 sin contenido, 502 red, 500 otro.
    """
    import logging as _log
    from core.url_transcribe import transcribe_url  # noqa: PLC0415

    _logger = _log.getLogger(__name__)

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "url field required"}), 400

    url = data["url"].strip()

    # Validación rápida: solo YouTube en este endpoint (compatibilidad con la UI existente)
    if not _YOUTUBE_RE.match(url):
        return jsonify({"error": "URL no reconocida como YouTube. Solo se admiten URLs de youtube.com o youtu.be"}), 400

    result = transcribe_url(url)

    if not result["ok"]:
        _kind_to_http = {
            "invalid_url": 400,
            "no_subtitles": 404,
            "needs_auth": 401,
            "network": 502,
            "empty": 404,
        }
        http_code = _kind_to_http.get(result.get("error_kind") or "", 500)
        return jsonify({"error": result.get("error", "Error desconocido")}), http_code

    # Persistir en DB si SAVE_HISTORY=true
    if os.getenv("SAVE_HISTORY", "true").lower() == "true":
        model_label = "youtube-subtitles" if result.get("method") == "subtitles" else "youtube-audio"
        _db.insert(
            text=result["text"],
            language=result.get("language"),
            duration_seconds=result.get("duration"),
            model=model_label,
            source=result.get("source", "youtube"),
        )

    return jsonify({
        "ok": True,
        "title": result.get("title", ""),
        "language": result.get("language", ""),
        "auto_generated": result.get("method") == "subtitles",
        "text": result["text"],
    })
