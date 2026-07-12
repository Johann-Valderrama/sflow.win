"""Tests de la unidad 2.3 (cobertura minima nueva): mapeo error_kind -> HTTP
del endpoint POST /api/youtube-transcript (web/server.py).

El mapeo vive INLINE en la vista Flask (web/server.py ~4883-4890), decision
del debate: no se refactoriza a produccion para esta unidad. Se testea via
Flask test client, monkeypatcheando core.url_transcribe.transcribe_url (el
nombre importado dentro de la funcion vista, por eso se parchea en el modulo
"core.url_transcribe" y no en "web.server") para devolver cada error_kind.

Mapa real verificado contra el codigo (web/server.py:4883-4889):
    invalid_url -> 400
    no_subtitles -> 404
    needs_auth  -> 401
    network     -> 502
    empty       -> 404   (NO estaba en el enunciado original; existe en el codigo)
    <desconocido o None> -> 500 (default de _kind_to_http.get(..., 500))

Sin red, sin yt-dlp real: transcribe_url esta completamente mockeado. Se usa
una URL que pasa la validacion previa del endpoint (_YOUTUBE_RE) para llegar
al bloque que mapea error_kind.
"""

import pytest

import core.url_transcribe as ut


@pytest.fixture
def client(tmp_path):
    from web.server import app, _db

    _db.db_path = str(tmp_path / "test.db")
    _db._init_db()
    return app.test_client()


_VALID_YOUTUBE_URL = "https://www.youtube.com/watch?v=abc12345678"


def _fake_result(error_kind, error="boom"):
    return {
        "ok": False,
        "title": None,
        "source": "youtube",
        "method": None,
        "language": None,
        "text": "",
        "duration": None,
        "error": error,
        "error_kind": error_kind,
    }


class TestErrorKindToHttpMapping:
    def test_invalid_url_maps_to_400(self, client, monkeypatch):
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("invalid_url"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 400

    def test_no_subtitles_maps_to_404(self, client, monkeypatch):
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("no_subtitles"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 404

    def test_needs_auth_maps_to_401(self, client, monkeypatch):
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("needs_auth"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 401

    def test_network_maps_to_502(self, client, monkeypatch):
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("network"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 502

    def test_empty_maps_to_404(self, client, monkeypatch):
        """error_kind='empty' (transcripcion vacia) tambien mapea a 404,
        igual que 'no_subtitles' — hallazgo: no estaba en el enunciado original."""
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("empty"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 404

    def test_unknown_error_kind_maps_to_500(self, client, monkeypatch):
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result("something_never_seen"))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 500

    def test_none_error_kind_maps_to_500(self, client, monkeypatch):
        """error_kind=None (no seteado) cae al default 500, no lanza KeyError."""
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: _fake_result(None))
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 500

    def test_error_body_contains_message(self, client, monkeypatch):
        monkeypatch.setattr(
            ut, "transcribe_url", lambda url, **kw: _fake_result("network", error="fallo de red simulado")
        )
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.get_json()["error"] == "fallo de red simulado"


class TestOkPath:
    def test_ok_result_returns_200_with_expected_shape(self, client, monkeypatch):
        ok_result = {
            "ok": True,
            "title": "Un video de prueba",
            "source": "youtube",
            "method": "subtitles",
            "language": "es",
            "text": "texto transcrito de prueba",
            "duration": 42.0,
            "error": None,
            "error_kind": None,
        }
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: ok_result)
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["title"] == "Un video de prueba"
        assert data["text"] == "texto transcrito de prueba"
        assert data["auto_generated"] is True  # method == "subtitles"

    def test_ok_audio_method_auto_generated_false(self, client, monkeypatch):
        ok_result = {
            "ok": True,
            "title": "Otro video",
            "source": "youtube",
            "method": "audio",
            "language": "es",
            "text": "texto via audio",
            "duration": 10.0,
            "error": None,
            "error_kind": None,
        }
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: ok_result)
        resp = client.post("/api/youtube-transcript", json={"url": _VALID_YOUTUBE_URL})
        assert resp.status_code == 200
        assert resp.get_json()["auto_generated"] is False


class TestUrlValidationBeforeTranscribe:
    def test_missing_url_field_returns_400(self, client):
        resp = client.post("/api/youtube-transcript", json={})
        assert resp.status_code == 400

    def test_non_youtube_url_returns_400_without_calling_transcribe(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(ut, "transcribe_url", lambda url, **kw: called.append(url) or _fake_result("network"))
        resp = client.post("/api/youtube-transcript", json={"url": "https://vimeo.com/12345"})
        assert resp.status_code == 400
        assert called == []
