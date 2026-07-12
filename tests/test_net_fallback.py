"""Tests del fallback simétrico de transcripción Groq -> local (unidad 5.5).

Espejo del fallback local->Groq existente (GROQ_FALLBACK): con backend primario
GROQ, si la transcripción de DICTADO falla por un error de RED, cae
automáticamente al modelo local (si ya está descargado). Diseño cerrado en el
debate adversarial (ver PLAN-MEJORAS, unidad 5.5):

  D1 (scope): SOLO aplica cuando el caller pasa net_fallback=True (dictado en
      main.py). Con net_fallback=False (default), cero cambio de comportamiento
      aunque TRANSCRIPTION_FALLBACK esté en "true".
  D2: el wav_buffer se reposiciona (seek(0)) antes de pasar al backend local.
  D3: solo errores de RED disparan el fallback; errores de la API (auth,
      rate-limit, bad request) se propagan tal cual, sin fallback.
  D4: al abrirse el breaker (primer fallo de red) se dispara warmup() del
      backend local en un hilo de fondo, fire-and-forget.

Todo hermético: los backends se sustituyen por dobles de prueba (_FakeBackend)
vía monkeypatch de ``core.transcriber.get_backend`` — sin red, sin modelo real.
"""
import io
import socket
import threading
import time

import pytest

from core.transcriber import Transcriber, _is_network_error


# ---------------------------------------------------------------------------
# Doble de prueba para TranscriptionBackend
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Backend falso configurable: no toca red ni disco."""

    def __init__(self, *, ready: bool = True):
        self.ready = ready
        self.transcribe_calls = 0
        self.translate_calls = 0
        self.warmup_calls = 0
        self.release_calls = 0
        self.transcribe_result = "resultado local"
        self.transcribe_exc: Exception | None = None
        self.translate_result = "resultado local traducido"
        self.translate_exc: Exception | None = None
        self.seen_buffer_positions_transcribe = []
        self.seen_buffer_positions_translate = []

    def transcribe(self, wav_buffer, language=None, prompt=None):
        self.transcribe_calls += 1
        self.seen_buffer_positions_transcribe.append(wav_buffer.tell())
        if self.transcribe_exc is not None:
            raise self.transcribe_exc
        return self.transcribe_result

    def translate(self, wav_buffer, target_lang="en", prompt=None):
        self.translate_calls += 1
        self.seen_buffer_positions_translate.append(wav_buffer.tell())
        if self.translate_exc is not None:
            raise self.translate_exc
        return self.translate_result

    def is_ready(self):
        return self.ready

    def warmup(self):
        self.warmup_calls += 1

    def release(self):
        self.release_calls += 1

    def get_model_name(self):
        return "fake"


@pytest.fixture
def fake_backends(monkeypatch):
    """Sustituye core.transcriber.get_backend por un registro de _FakeBackend.

    _run_net_fallback / self._get_backend() SIEMPRE resuelven a través de este
    nombre importado (``from core.backends import get_backend``), así que
    parchear ``core.transcriber.get_backend`` cubre tanto el backend primario
    (self._get_backend() -> get_backend(current_name)) como el acceso directo
    al local (get_backend("local")) usado por el fallback.
    """
    registry = {"groq": _FakeBackend(), "local": _FakeBackend()}

    def _fake_get_backend(name=None):
        resolved = (name or "groq").strip().lower()
        return registry[resolved]

    monkeypatch.setattr("core.transcriber.get_backend", _fake_get_backend)
    return registry


@pytest.fixture
def transcriber(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_BACKEND", "groq")
    return Transcriber()


def _buf(n=200):
    return io.BytesIO(b"x" * n)


# ---------------------------------------------------------------------------
# _is_network_error — clasificación por clase de excepción
# ---------------------------------------------------------------------------

class TestIsNetworkError:
    def test_builtin_connection_error_is_network(self):
        assert _is_network_error(ConnectionError("no route")) is True

    def test_builtin_timeout_error_is_network(self):
        assert _is_network_error(TimeoutError("timed out")) is True

    def test_socket_timeout_is_network(self):
        assert _is_network_error(socket.timeout()) is True

    def test_socket_gaierror_is_network(self):
        assert _is_network_error(socket.gaierror()) is True

    def test_groq_api_connection_error_is_network(self):
        import groq
        import httpx
        req = httpx.Request("POST", "https://api.groq.com/x")
        assert _is_network_error(groq.APIConnectionError(request=req)) is True

    def test_groq_api_timeout_error_is_network(self):
        import groq
        import httpx
        req = httpx.Request("POST", "https://api.groq.com/x")
        assert _is_network_error(groq.APITimeoutError(request=req)) is True

    def test_groq_authentication_error_is_not_network(self):
        import groq
        import httpx
        req = httpx.Request("POST", "https://api.groq.com/x")
        resp = httpx.Response(401, request=req)
        exc = groq.AuthenticationError("bad key", response=resp, body=None)
        assert _is_network_error(exc) is False

    def test_groq_rate_limit_error_is_not_network(self):
        import groq
        import httpx
        req = httpx.Request("POST", "https://api.groq.com/x")
        resp = httpx.Response(429, request=req)
        exc = groq.RateLimitError("rate limited", response=resp, body=None)
        assert _is_network_error(exc) is False

    def test_groq_bad_request_error_is_not_network(self):
        import groq
        import httpx
        req = httpx.Request("POST", "https://api.groq.com/x")
        resp = httpx.Response(400, request=req)
        exc = groq.BadRequestError("bad request", response=resp, body=None)
        assert _is_network_error(exc) is False

    def test_httpx_connect_error_is_network(self):
        import httpx
        assert _is_network_error(httpx.ConnectError("boom")) is True

    def test_httpx_read_timeout_is_network(self):
        import httpx
        assert _is_network_error(httpx.ReadTimeout("boom")) is True

    def test_requests_connection_error_is_network(self):
        import requests
        assert _is_network_error(requests.exceptions.ConnectionError("boom")) is True

    def test_generic_runtime_error_is_not_network(self):
        assert _is_network_error(RuntimeError("boom")) is False

    def test_value_error_is_not_network(self):
        assert _is_network_error(ValueError("boom")) is False


# ---------------------------------------------------------------------------
# transcribe(): scope D1 — net_fallback=False es un no-op absoluto
# ---------------------------------------------------------------------------

class TestScopeD1DefaultOff:
    def test_net_fallback_false_propagates_even_with_flag_on(self, monkeypatch, fake_backends, transcriber):
        """Con net_fallback=False (default), TRANSCRIPTION_FALLBACK=true no debe
        tener NINGÚN efecto: el error de red se propaga igual que hoy."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=False)
        assert fake_backends["local"].transcribe_calls == 0

    def test_net_fallback_false_success_path_unchanged(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_result = "hola mundo"
        result = transcriber.transcribe(_buf(), net_fallback=False)
        assert result == "hola mundo"
        assert fake_backends["local"].transcribe_calls == 0


# ---------------------------------------------------------------------------
# transcribe(): fallback exitoso, guard de modelo no descargado, breaker
# ---------------------------------------------------------------------------

class TestTranscribeNetFallback:
    def test_network_failure_flag_on_model_ready_falls_back_to_local(
        self, monkeypatch, fake_backends, transcriber
    ):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True
        fake_backends["local"].transcribe_result = "hola desde el modelo local"

        result = transcriber.transcribe(_buf(), net_fallback=True)

        assert result == "hola desde el modelo local"
        assert fake_backends["local"].transcribe_calls == 1
        # Breaker abierto tras el fallo
        assert transcriber._net_breaker_open() is True

    def test_network_failure_flag_off_propagates_without_fallback(
        self, monkeypatch, fake_backends, transcriber
    ):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "false")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=True)
        assert fake_backends["local"].transcribe_calls == 0
        assert transcriber._net_breaker_open() is False

    def test_api_error_flag_on_propagates_without_fallback(
        self, monkeypatch, fake_backends, transcriber
    ):
        """D3: un fallo de la API (no de red) NUNCA dispara el fallback."""
        import groq
        import httpx
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        req = httpx.Request("POST", "https://api.groq.com/x")
        resp = httpx.Response(401, request=req)
        fake_backends["groq"].transcribe_exc = groq.AuthenticationError(
            "bad key", response=resp, body=None
        )
        with pytest.raises(groq.AuthenticationError):
            transcriber.transcribe(_buf(), net_fallback=True)
        assert fake_backends["local"].transcribe_calls == 0
        assert transcriber._net_breaker_open() is False

    def test_model_not_downloaded_propagates_and_notifies_once(
        self, monkeypatch, fake_backends, transcriber
    ):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = False

        notices = []
        transcriber.on_net_fallback_event = notices.append

        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=True)
        assert fake_backends["local"].transcribe_calls == 0
        assert len(notices) == 1
        assert "no descargado" in notices[0] or "sin modelo local" in notices[0]
        # El modelo no listo nunca abre el breaker (no hay fallback usable)
        assert transcriber._net_breaker_open() is False

        # Dedup: un segundo fallo inmediato NO vuelve a notificar (mismo cooldown)
        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=True)
        assert len(notices) == 1

    def test_local_backend_never_asked_to_download(self, monkeypatch, fake_backends, transcriber):
        """Guard: el fallback SOLO consulta is_ready(); nunca dispara una descarga."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = False
        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=True)
        # _FakeBackend no tiene método de descarga; si el código intentara
        # invocar alguno inexistente, este test ya habría lanzado AttributeError.
        assert fake_backends["local"].warmup_calls == 0

    def test_seek_zero_called_before_local_fallback(self, monkeypatch, fake_backends, transcriber):
        """D2: el wav_buffer se reposiciona a 0 antes de pasar al backend local
        (el backend Groq pudo haber consumido/posicionado el buffer)."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        buf = _buf()
        buf.read(50)  # simula que Groq avanzó el cursor antes de fallar
        assert buf.tell() == 50

        transcriber.transcribe(buf, net_fallback=True)
        assert fake_backends["local"].seen_buffer_positions_transcribe == [0]

    def test_both_backends_fail_raises_original_network_error(
        self, monkeypatch, fake_backends, transcriber
    ):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        net_exc = ConnectionError("sin red")
        fake_backends["groq"].transcribe_exc = net_exc
        fake_backends["local"].ready = True
        fake_backends["local"].transcribe_exc = RuntimeError("local también roto")

        with pytest.raises(ConnectionError):
            transcriber.transcribe(_buf(), net_fallback=True)


# ---------------------------------------------------------------------------
# Breaker: cooldown, salto directo a local, reset en éxito de Groq
# ---------------------------------------------------------------------------

class TestBreakerCooldown:
    def test_within_cooldown_goes_directly_to_local_without_calling_groq(
        self, monkeypatch, fake_backends, transcriber
    ):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_COOLDOWN", "120")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True
        fake_backends["local"].transcribe_result = "primer fallback"

        transcriber.transcribe(_buf(), net_fallback=True)
        assert fake_backends["groq"].transcribe_calls == 1
        assert fake_backends["local"].transcribe_calls == 1

        # Segundo dictado dentro del cooldown: NO debe volver a intentar Groq.
        fake_backends["local"].transcribe_result = "segundo directo a local"
        result = transcriber.transcribe(_buf(), net_fallback=True)
        assert result == "segundo directo a local"
        assert fake_backends["groq"].transcribe_calls == 1  # sin cambio
        assert fake_backends["local"].transcribe_calls == 2

    def test_after_cooldown_retries_groq_first(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_COOLDOWN", "0.05")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        transcriber.transcribe(_buf(), net_fallback=True)
        assert fake_backends["groq"].transcribe_calls == 1

        time.sleep(0.08)  # superar el cooldown de 0.05s

        fake_backends["groq"].transcribe_exc = None
        fake_backends["groq"].transcribe_result = "groq recuperado"
        result = transcriber.transcribe(_buf(), net_fallback=True)
        assert result == "groq recuperado"
        assert fake_backends["groq"].transcribe_calls == 2  # reintentó Groq

    def test_groq_success_resets_breaker(self, monkeypatch, fake_backends, transcriber):
        """El breaker solo se resetea con un éxito REAL de Groq — dentro del
        cooldown el atajo va directo a local sin volver a intentar Groq (por
        diseño, para no pagar su timeout), así que el reset solo puede
        observarse tras expirar el cooldown."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_COOLDOWN", "0.05")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True
        transcriber.transcribe(_buf(), net_fallback=True)
        assert transcriber._net_breaker_open() is True

        time.sleep(0.08)  # superar el cooldown para que se reintente Groq
        fake_backends["groq"].transcribe_exc = None
        fake_backends["groq"].transcribe_result = "de vuelta"
        transcriber.transcribe(_buf(), net_fallback=True)
        assert transcriber._net_breaker_open() is False

    def test_warmup_triggered_once_on_first_failure(self, monkeypatch, fake_backends, transcriber):
        """D4: warmup se dispara en la transición cerrado->abierto del breaker,
        en un hilo de fondo (fire-and-forget) — se espera brevemente a que
        termine para poder verificarlo de forma determinista."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        transcriber.transcribe(_buf(), net_fallback=True)
        # Esperar a que el hilo daemon de warmup termine (best-effort, con margen)
        deadline = time.monotonic() + 2.0
        while fake_backends["local"].warmup_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake_backends["local"].warmup_calls == 1

        # Un segundo fallo (dentro del cooldown, vía el atajo de breaker abierto)
        # no debe volver a disparar warmup.
        transcriber.transcribe(_buf(), net_fallback=True)
        time.sleep(0.05)
        assert fake_backends["local"].warmup_calls == 1


# ---------------------------------------------------------------------------
# Notificación de "usando modelo local" — una vez por episodio
# ---------------------------------------------------------------------------

class TestUsingLocalNotification:
    def test_notifies_once_per_breaker_episode(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        notices = []
        transcriber.on_net_fallback_event = notices.append

        transcriber.transcribe(_buf(), net_fallback=True)
        transcriber.transcribe(_buf(), net_fallback=True)  # mismo episodio (breaker abierto)
        assert len(notices) == 1
        assert "modelo local" in notices[0]

    def test_notifies_again_after_breaker_reset(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_COOLDOWN", "0.05")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        notices = []
        transcriber.on_net_fallback_event = notices.append

        transcriber.transcribe(_buf(), net_fallback=True)
        assert len(notices) == 1

        # Esperar a que expire el cooldown para que el siguiente intento
        # vuelva a llamar a Groq de verdad (el atajo de breaker-abierto no
        # reintenta Groq por diseño — ver test_groq_success_resets_breaker).
        time.sleep(0.08)

        # Groq se recupera: resetea el breaker y el dedup
        fake_backends["groq"].transcribe_exc = None
        fake_backends["groq"].transcribe_result = "ok"
        transcriber.transcribe(_buf(), net_fallback=True)
        assert transcriber._net_breaker_open() is False

        # Nuevo episodio de fallo: debe notificar otra vez
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red de nuevo")
        transcriber.transcribe(_buf(), net_fallback=True)
        assert len(notices) == 2

    def test_callback_exception_does_not_break_dictation(self, monkeypatch, fake_backends, transcriber):
        """Un callback de notificación que lanza no debe romper el fallback."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].transcribe_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True
        fake_backends["local"].transcribe_result = "resultado pese al callback roto"

        def _broken(_msg):
            raise RuntimeError("boom en el callback")

        transcriber.on_net_fallback_event = _broken
        result = transcriber.transcribe(_buf(), net_fallback=True)
        assert result == "resultado pese al callback roto"


# ---------------------------------------------------------------------------
# translate(): solo aplica cuando target_lang == "en"
# ---------------------------------------------------------------------------

class TestTranslateNetFallback:
    def test_target_en_falls_back_to_local(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].translate_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True
        fake_backends["local"].translate_result = "translated locally"

        result = transcriber.translate(_buf(), target_lang="en", net_fallback=True)
        assert result == "translated locally"
        assert fake_backends["local"].translate_calls == 1

    def test_target_non_en_never_falls_back(self, monkeypatch, fake_backends, transcriber):
        """Punto de diseño 8: el backend local solo traduce a inglés, así que
        para cualquier otro target el fallback NUNCA aplica, sin importar los
        flags — el comportamiento es idéntico al actual (propaga el error)."""
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].translate_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        with pytest.raises(ConnectionError):
            transcriber.translate(_buf(), target_lang="es", net_fallback=True)
        assert fake_backends["local"].translate_calls == 0

    def test_translate_seek_zero_before_local_fallback(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].translate_exc = ConnectionError("sin red")
        fake_backends["local"].ready = True

        buf = _buf()
        buf.read(30)
        transcriber.translate(buf, target_lang="en", net_fallback=True)
        assert fake_backends["local"].seen_buffer_positions_translate == [0]

    def test_translate_net_fallback_false_is_noop(self, monkeypatch, fake_backends, transcriber):
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK", "true")
        fake_backends["groq"].translate_exc = ConnectionError("sin red")
        with pytest.raises(ConnectionError):
            transcriber.translate(_buf(), target_lang="en", net_fallback=False)
        assert fake_backends["local"].translate_calls == 0


# ---------------------------------------------------------------------------
# ENV_CATALOG sincronizado (unidad 4.3)
# ---------------------------------------------------------------------------

class TestEnvCatalogSync:
    def test_transcription_fallback_cataloged(self):
        import config
        entry = config.ENV_CATALOG["TRANSCRIPTION_FALLBACK"]
        assert entry["default"] == "false"
        assert entry["kind"] == "lazy"
        assert entry["killswitch"] is True

    def test_transcription_fallback_cooldown_cataloged(self):
        import config
        entry = config.ENV_CATALOG["TRANSCRIPTION_FALLBACK_COOLDOWN"]
        assert entry["default"] == "120"
        assert entry["kind"] == "lazy"
        assert entry["killswitch"] is False
