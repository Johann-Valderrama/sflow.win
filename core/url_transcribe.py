"""Motor unificado de transcripción desde URL (Fase 3).

Dado una URL, decide si usar subtítulos (gratis, sin Groq) o ruta de audio
(descarga con yt-dlp + decodificación PyAV + transcripción via Transcriber).

Contrato público:
    detect_platform(url) -> str | None
    transcribe_url(url, *, allow_instagram=False, on_progress=None) -> dict

El caller (endpoint o cola bulk) es responsable de persistir el resultado en DB.
Este módulo NO toca la DB ni la UI.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import wave
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detección de plataforma
# ---------------------------------------------------------------------------

_YT_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch|shorts|embed|live)|youtu\.be/)",
    re.IGNORECASE,
)
_TT_RE = re.compile(r"^https?://(?:www\.|vm\.)?tiktok\.com/", re.IGNORECASE)
_IG_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def detect_platform(url: str) -> Optional[str]:
    """Detecta la plataforma de la URL.

    Devuelve:
        "youtube" | "tiktok" | "instagram" | "other" | None
        None si la cadena no parece una URL HTTP/S.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not _URL_RE.match(url):
        return None
    if _YT_RE.match(url):
        return "youtube"
    if _TT_RE.match(url):
        return "tiktok"
    if _IG_RE.match(url):
        return "instagram"
    return "other"


# ---------------------------------------------------------------------------
# Parser VTT (extraído de web/server.py para reutilización)
# ---------------------------------------------------------------------------

def _parse_vtt_to_text(content: str) -> str:
    """Convierte subtítulos VTT a texto plano deduplicando cues rolling de YouTube."""
    lines = content.splitlines()
    seen: list[str] = []
    in_cue = False
    for line in lines:
        line = line.strip()
        if line.startswith("WEBVTT") or not line:
            in_cue = False
            continue
        if re.match(r"^\d{2}:\d{2}", line) or "-->" in line:
            in_cue = True
            continue
        if re.match(r"^\d+$", line):
            in_cue = True
            continue
        if in_cue and line:
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean and (not seen or seen[-1] != clean):
                seen.append(clean)
    return " ".join(seen)


def _parse_json3_to_text(content: str) -> str:
    """Convierte formato json3 de YouTube a texto plano deduplicando cues rolling."""
    import json as _json
    data = _json.loads(content)
    events = data.get("events", [])
    seen: list[str] = []
    for ev in events:
        segs = ev.get("segs")
        if not segs:
            continue
        line = "".join(s.get("utf8", "") for s in segs).strip()
        if not line or line == "\n":
            continue
        line = line.replace("\n", " ").strip()
        if line and (not seen or seen[-1] != line):
            seen.append(line)
    return " ".join(seen)


# ---------------------------------------------------------------------------
# Ruta de subtítulos (YouTube)
# ---------------------------------------------------------------------------

def _try_subtitles(url: str, ydl_mod, preferred_lang: str, on_progress: Optional[Callable]) -> Optional[dict]:
    """Intenta descargar y parsear subtítulos de YouTube.

    Devuelve un dict parcial {title, language, auto_generated, text, duration}
    o None si no hay subtítulos disponibles.

    Lanza excepciones de red para que el caller las capture.
    """
    if on_progress:
        on_progress("buscando subtítulos")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Primera pasada: solo metadatos para ver qué subtítulos hay
        ydl_probe_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "vtt",
            "subtitleslangs": [preferred_lang, "en", "es"],
        }
        with ydl_mod.YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "")
        duration = info.get("duration")
        subs_manual = info.get("subtitles") or {}
        subs_auto = info.get("automatic_captions") or {}

        # Elegir pista: manuales > automáticas, idioma preferido > en > primero
        chosen_lang = None
        auto_generated = False

        for lang in [preferred_lang, "en"]:
            if lang in subs_manual and subs_manual[lang]:
                chosen_lang = lang
                auto_generated = False
                break

        if chosen_lang is None:
            if subs_manual:
                chosen_lang = next(iter(subs_manual))
                auto_generated = False
            elif preferred_lang in subs_auto and subs_auto[preferred_lang]:
                chosen_lang = preferred_lang
                auto_generated = True
            elif "en" in subs_auto and subs_auto["en"]:
                chosen_lang = "en"
                auto_generated = True
            elif subs_auto:
                chosen_lang = next(iter(subs_auto))
                auto_generated = True

        if chosen_lang is None:
            return None  # sin subtítulos → fallback a audio

        if on_progress:
            on_progress("descargando subtítulos")

        # Segunda pasada: descargar la pista elegida
        ydl_dl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "writesubtitles": not auto_generated,
            "writeautomaticsub": auto_generated,
            "subtitlesformat": "vtt",
            "subtitleslangs": [chosen_lang],
        }
        with ydl_mod.YoutubeDL(ydl_dl_opts) as ydl2:
            ydl2.download([url])

        # Leer el archivo descargado
        sub_text = None
        for fname in os.listdir(tmpdir):
            fpath = os.path.join(tmpdir, fname)
            if fname.endswith(".vtt"):
                with open(fpath, encoding="utf-8") as f:
                    sub_text = _parse_vtt_to_text(f.read())
                break
            elif fname.endswith(".json3"):
                with open(fpath, encoding="utf-8") as f:
                    sub_text = _parse_json3_to_text(f.read())
                break

        if not sub_text:
            return None  # archivo vacío → fallback a audio

        return {
            "title": title,
            "language": chosen_lang,
            "auto_generated": auto_generated,
            "text": sub_text,
            "duration": float(duration) if duration is not None else None,
        }


# ---------------------------------------------------------------------------
# Ruta de audio: descarga + decodificación PCM + transcripción por chunks
# ---------------------------------------------------------------------------

_CHUNK_SECONDS = 240   # ~7.7 MB por chunk a 16 kHz 16-bit mono
_OVERLAP_SECONDS = 2   # solape entre chunks para continuidad de contexto
_SAMPLE_RATE = 16000


def _pcm_to_wav_bytes(pcm: np.ndarray) -> io.BytesIO:
    """Empaqueta un array int16 mono 16 kHz como WAV en memoria."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    buf.seek(0)
    return buf


def _decode_audio_to_pcm(audio_path: str) -> np.ndarray:
    """Decodifica un archivo de audio a PCM int16 mono 16 kHz usando PyAV."""
    import av  # noqa: PLC0415

    container = av.open(audio_path)
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=_SAMPLE_RATE)
    chunks = []
    for frame in container.decode(stream):
        for rs in resampler.resample(frame):
            chunks.append(rs.to_ndarray())
    # Flush del resampler
    for rs in resampler.resample(None):
        chunks.append(rs.to_ndarray())
    container.close()

    if not chunks:
        return np.array([], dtype=np.int16)
    return np.concatenate([c.reshape(-1) for c in chunks]).astype(np.int16)


def _transcribe_pcm_chunked(pcm: np.ndarray, transcriber_instance, on_progress: Optional[Callable]) -> str:
    """Transcribe PCM completo dividiéndolo en ventanas si es necesario.

    Para audios cortos (≤ CHUNK_SECONDS) envía un único WAV.
    Para audios largos parte en ventanas con OVERLAP_SECONDS de solape y usa
    el final del texto previo como prompt de contexto (mejora continuidad).
    """
    total_samples = len(pcm)
    chunk_samples = _CHUNK_SECONDS * _SAMPLE_RATE
    overlap_samples = _OVERLAP_SECONDS * _SAMPLE_RATE

    if total_samples <= chunk_samples:
        # Audio corto: un solo chunk
        if on_progress:
            on_progress("transcribiendo")
        wav_buf = _pcm_to_wav_bytes(pcm)
        return transcriber_instance.transcribe(wav_buf)

    # Audio largo: transcripción por ventanas con carryover
    parts: list[str] = []
    start = 0
    chunk_idx = 0
    while start < total_samples:
        end = min(start + chunk_samples, total_samples)
        chunk_pcm = pcm[start:end]
        chunk_idx += 1
        total_chunks = (total_samples + chunk_samples - 1) // chunk_samples
        if on_progress:
            on_progress(f"transcribiendo ({chunk_idx}/{total_chunks})")

        # Prompt de contexto: últimas ~200 chars del texto acumulado
        carry_prompt = " ".join(parts)[-200:] if parts else None
        wav_buf = _pcm_to_wav_bytes(chunk_pcm)
        chunk_text = transcriber_instance.transcribe(wav_buf, prompt=carry_prompt)
        if chunk_text:
            parts.append(chunk_text)

        # Avanzar con solape hacia atrás para no perder palabras en el corte
        start = end - overlap_samples if end < total_samples else total_samples

    return " ".join(parts)


def _try_audio(
    url: str,
    ydl_mod,
    platform: str,
    allow_instagram: bool,
    on_progress: Optional[Callable],
) -> dict:
    """Descarga audio y transcribe.  Devuelve dict parcial con text, language, duration, title."""
    import av  # noqa: PLC0415 — validar disponibilidad antes de descargar
    from core.transcriber import Transcriber  # noqa: PLC0415

    ydl_opts: dict = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        # Sin postprocessors → yt-dlp no invoca ffmpeg externo
    }

    # Instagram: necesita cookies del navegador para contenido privado/semi-privado
    if platform == "instagram" and allow_instagram:
        # Intentar con cookies de Chrome; si falla, el error de auth lo captura el caller
        ydl_opts["cookiesfrombrowser"] = ("chrome",)

    if on_progress:
        on_progress("descargando audio")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            ydl_opts["outtmpl"] = outtmpl

            with ydl_mod.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            title = info.get("title", "")
            duration_raw = info.get("duration")
            duration = float(duration_raw) if duration_raw is not None else None
            vid_id = info.get("id", "unknown")
            ext = info.get("ext", "")
            audio_path = os.path.join(tmpdir, f"{vid_id}.{ext}")

            if not os.path.isfile(audio_path):
                # yt-dlp a veces usa ext diferente; buscar cualquier archivo de audio
                candidates = [
                    f for f in os.listdir(tmpdir)
                    if not f.endswith(".json") and not f.endswith(".vtt")
                ]
                if not candidates:
                    return {"ok": False, "error": "No se encontró el archivo de audio descargado.",
                            "error_kind": "unsupported", "title": title, "duration": duration}
                audio_path = os.path.join(tmpdir, candidates[0])

            if on_progress:
                on_progress("decodificando audio")

            try:
                pcm = _decode_audio_to_pcm(audio_path)
            except Exception as exc:
                logger.warning("PyAV no pudo decodificar %s: %s", audio_path, exc)
                return {
                    "ok": False,
                    "error": f"Formato de audio no soportado o archivo dañado: {exc}",
                    "error_kind": "unsupported",
                    "title": title,
                    "duration": duration,
                }

            if len(pcm) < _SAMPLE_RATE * 0.3:
                return {
                    "ok": False,
                    "error": "El audio descargado es demasiado corto para transcribir.",
                    "error_kind": "empty",
                    "title": title,
                    "duration": duration,
                }

            transcriber = Transcriber()
            text = _transcribe_pcm_chunked(pcm, transcriber, on_progress)

            if not text or not text.strip():
                return {
                    "ok": False,
                    "error": "La transcripción del audio resultó vacía. El video puede no tener habla clara.",
                    "error_kind": "empty",
                    "title": title,
                    "duration": duration,
                }

            # Idioma detectado: WHISPER_LANGUAGE o "unknown"
            lang = os.getenv("WHISPER_LANGUAGE", "es")

            return {
                "ok": True,
                "title": title,
                "language": lang,
                "text": text,
                "duration": duration,
            }

        except ydl_mod.utils.DownloadError as exc:
            msg = str(exc)
            if "private" in msg.lower() or "login" in msg.lower() or "cookies" in msg.lower():
                return {
                    "ok": False,
                    "error": "El video requiere autenticación. Para Instagram activa el modo experimental con cookies.",
                    "error_kind": "needs_auth",
                    "title": None,
                    "duration": None,
                }
            return {
                "ok": False,
                "error": f"Error de red al descargar el video: {exc}",
                "error_kind": "network",
                "title": None,
                "duration": None,
            }


# ---------------------------------------------------------------------------
# Punto de entrada principal
# ---------------------------------------------------------------------------

def transcribe_url(
    url: str,
    *,
    allow_instagram: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Motor unificado: dado una URL, transcribe su contenido.

    Flujo:
    1. Detectar plataforma. URL inválida → error inmediato.
    2. Instagram sin allow_instagram → error needs_auth.
    3. YouTube → intentar subtítulos primero; si no hay, caer a ruta audio.
    4. TikTok / Instagram con cookies / other → ruta audio directamente.

    No escribe en DB. El caller es responsable de persistir el resultado.

    Argumentos:
        url: URL del video a transcribir.
        allow_instagram: Activar ruta experimental de Instagram (cookies Chrome).
        on_progress: Callable opcional que recibe strings de etapa (no garantiza
            llamadas entre backends locales).

    Devuelve siempre un dict con las claves:
        ok, title, source, method, language, text, duration, error, error_kind
    """
    # Estructura base garantizada
    result: dict = {
        "ok": False,
        "title": None,
        "source": None,
        "method": None,
        "language": None,
        "text": "",
        "duration": None,
        "error": None,
        "error_kind": None,
    }

    # 1. Detectar plataforma
    platform = detect_platform(url)
    if platform is None:
        result["error"] = "La cadena proporcionada no parece una URL válida (debe comenzar con http:// o https://)."
        result["error_kind"] = "invalid_url"
        return result

    result["source"] = platform

    # 2. Instagram sin permiso explícito
    if platform == "instagram" and not allow_instagram:
        result["error"] = (
            "Instagram es experimental y requiere cookies del navegador. "
            "Activa la opción 'Permitir Instagram' para intentar con cookies de Chrome."
        )
        result["error_kind"] = "needs_auth"
        return result

    # Importar yt-dlp (lazy para no retrasar el arranque de la app)
    try:
        import yt_dlp as _yt_dlp  # noqa: PLC0415
    except ImportError:
        result["error"] = "yt-dlp no está instalado. Ejecuta: pip install yt-dlp"
        result["error_kind"] = "unsupported"
        return result

    preferred_lang = os.getenv("WHISPER_LANGUAGE", "es")

    # 3. YouTube: intentar subtítulos primero
    if platform == "youtube":
        try:
            sub_result = _try_subtitles(url, _yt_dlp, preferred_lang, on_progress)
        except _yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "private" in msg.lower() or "login" in msg.lower():
                result["error"] = f"El video es privado o requiere autenticación: {exc}"
                result["error_kind"] = "needs_auth"
                return result
            logger.warning("DownloadError buscando subtítulos, cayendo a audio: %s", exc)
            sub_result = None
        except Exception as exc:
            logger.warning("Error inesperado buscando subtítulos, cayendo a audio: %s", exc)
            sub_result = None

        if sub_result is not None:
            # Subtítulos encontrados: aplicar diccionario personal
            from core import dictionary as _dict  # noqa: PLC0415
            text = _dict.apply_replacements(sub_result["text"])
            result.update({
                "ok": True,
                "title": sub_result["title"],
                "method": "subtitles",
                "language": sub_result["language"],
                "text": text,
                "duration": sub_result["duration"],
            })
            return result
        # Sin subtítulos → caer a ruta audio (continúa abajo)
        logger.info("Sin subtítulos para %s; usando ruta de audio.", url)

    # 4. Ruta audio: YouTube-sin-subs, TikTok, Instagram habilitado, other
    try:
        audio_result = _try_audio(url, _yt_dlp, platform, allow_instagram, on_progress)
    except Exception as exc:
        logger.error("Error inesperado en ruta audio para %s: %s", url, exc, exc_info=True)
        result["error"] = f"Error inesperado al procesar el audio: {exc}"
        result["error_kind"] = "network"
        return result

    if audio_result.get("ok"):
        result.update({
            "ok": True,
            "title": audio_result["title"],
            "method": "audio",
            "language": audio_result.get("language"),
            "text": audio_result["text"],
            "duration": audio_result.get("duration"),
        })
    else:
        # Propagar error de la ruta audio; source ya está fijado
        result["title"] = audio_result.get("title")
        result["duration"] = audio_result.get("duration")
        result["error"] = audio_result.get("error", "Error desconocido al transcribir el audio.")
        result["error_kind"] = audio_result.get("error_kind", "unsupported")
        result["method"] = "audio"  # intentamos audio aunque falló

    return result
