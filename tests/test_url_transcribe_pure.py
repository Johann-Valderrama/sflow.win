"""Tests de la unidad 2.3 (cobertura minima nueva): funciones puras de
core/url_transcribe.py — detect_platform() y _parse_vtt_to_text().

NO duplica tests/test_url_transcribe_robustness.py (unidad 1.8): ese archivo
cubre _dedupe_overlap_prefix (dedup de SOLAPE entre chunks de audio), el
reintento por chunk (_transcribe_pcm_chunked) y el refcount de
_clean_crypt32_argtypes. Esta unidad cubre una superficie distinta y anterior
en el pipeline: deteccion de plataforma por regex y el parser VTT->texto
(dedup de CUES rolling de subtitulos, no de chunks de audio).

Ambas funciones son puras (sin I/O, sin red, sin threads).
"""

from core.url_transcribe import _parse_vtt_to_text, detect_platform


class TestDetectPlatformYoutube:
    def test_watch_url_with_www(self):
        assert detect_platform("https://www.youtube.com/watch?v=abc123") == "youtube"

    def test_watch_url_without_www(self):
        assert detect_platform("https://youtube.com/watch?v=abc123") == "youtube"

    def test_watch_url_http_no_https(self):
        assert detect_platform("http://www.youtube.com/watch?v=abc123") == "youtube"

    def test_short_link_youtu_be(self):
        assert detect_platform("https://youtu.be/abc123") == "youtube"

    def test_mobile_subdomain(self):
        assert detect_platform("https://m.youtube.com/watch?v=abc123") == "youtube"

    def test_shorts_url(self):
        assert detect_platform("https://www.youtube.com/shorts/abc123") == "youtube"

    def test_embed_url(self):
        assert detect_platform("https://www.youtube.com/embed/abc123") == "youtube"

    def test_live_url(self):
        assert detect_platform("https://www.youtube.com/live/abc123") == "youtube"


class TestDetectPlatformTiktok:
    def test_standard_video_url(self):
        assert detect_platform("https://www.tiktok.com/@user/video/123456") == "tiktok"

    def test_short_vm_link(self):
        assert detect_platform("https://vm.tiktok.com/xyz123/") == "tiktok"

    def test_without_www(self):
        assert detect_platform("https://tiktok.com/@user/video/123456") == "tiktok"


class TestDetectPlatformInstagram:
    def test_reel_url(self):
        assert detect_platform("https://www.instagram.com/reel/abc123/") == "instagram"

    def test_post_url(self):
        assert detect_platform("https://www.instagram.com/p/abc123/") == "instagram"

    def test_without_www(self):
        assert detect_platform("https://instagram.com/reel/abc123/") == "instagram"


class TestDetectPlatformOtherAndInvalid:
    def test_unsupported_http_url_returns_other(self):
        assert detect_platform("https://example.com/some/video.mp4") == "other"

    def test_vimeo_url_returns_other(self):
        assert detect_platform("https://vimeo.com/123456") == "other"

    def test_garbage_string_returns_none(self):
        assert detect_platform("not a url at all") is None

    def test_empty_string_returns_none(self):
        assert detect_platform("") is None

    def test_none_returns_none(self):
        assert detect_platform(None) is None

    def test_non_string_returns_none(self):
        assert detect_platform(123) is None

    def test_non_http_scheme_returns_none(self):
        """ftp:// (u otro esquema no http/https) no es una URL soportada."""
        assert detect_platform("ftp://youtube.com/watch?v=x") is None

    def test_whitespace_around_url_is_stripped(self):
        assert detect_platform("  https://www.youtube.com/watch?v=abc123  ") == "youtube"


class TestParseVttToText:
    def test_basic_vtt_two_cues(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "Hola a todos\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "bienvenidos al video\n"
        )
        assert _parse_vtt_to_text(vtt) == "Hola a todos bienvenidos al video"

    def test_rolling_cues_deduplicate_repeated_previous_line(self):
        """Subtitulos auto-generados de YouTube repiten la linea anterior en
        cada cue (ventana deslizante); solo la parte NUEVA debe conservarse."""
        vtt = (
            "WEBVTT\n"
            "Kind: captions\n"
            "Language: es\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "hola a todos\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "hola a todos\n"
            "bienvenidos hoy\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "bienvenidos hoy\n"
            "vamos a empezar\n"
        )
        assert _parse_vtt_to_text(vtt) == "hola a todos bienvenidos hoy vamos a empezar"

    def test_numbered_cue_index_lines_are_ignored(self):
        """Cue con indice numerico antes del timestamp (formato SRT-like en VTT)."""
        vtt = (
            "WEBVTT\n\n"
            "1\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "primera linea\n\n"
            "2\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "segunda linea\n"
        )
        assert _parse_vtt_to_text(vtt) == "primera linea segunda linea"

    def test_inline_tags_and_timestamps_are_stripped(self):
        """Tags <c>...</c> y timestamps inline <00:00:01.000> se eliminan,
        dejando el espacio circundante (comportamiento verificado del regex)."""
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "<c>hola</c> <00:00:01.000><c> mundo</c>\n"
        )
        assert _parse_vtt_to_text(vtt) == "hola  mundo"

    def test_empty_content_returns_empty_string(self):
        assert _parse_vtt_to_text("") == ""

    def test_header_only_returns_empty_string(self):
        assert _parse_vtt_to_text("WEBVTT\n\n") == ""

    def test_consecutive_identical_lines_not_from_rolling_window_are_still_deduped(self):
        """La dedup compara solo contra el cue INMEDIATAMENTE anterior (seen[-1]):
        una repeticion exacta consecutiva se colapsa a una sola aparicion."""
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "misma linea\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "misma linea\n"
        )
        assert _parse_vtt_to_text(vtt) == "misma linea"

    def test_non_consecutive_repeat_is_not_deduped(self):
        """Solo se compara contra el cue anterior inmediato: si una linea
        identica reaparece DESPUES de una linea distinta, no se colapsa."""
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "linea A\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "linea B\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "linea A\n"
        )
        assert _parse_vtt_to_text(vtt) == "linea A linea B linea A"
