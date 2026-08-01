"""Tests de LOCAL_DEVICE (auto/cpu/cuda) y su fallback a CPU (unidad 7b de
PLAN-DICTADO-2026-07-31, Ola 7).

Contexto: el banco de la unidad 7a (docs/benchmarks/local-backend-gpu-2026-08-01.md)
midió que CUDA gana 3.6x-4.9x sobre CPU en esta máquina (GTX 1060), y que
compute_type="int8" es la recomendación (float16/int8_float16 NI SIQUIERA EXISTEN
en esta GPU Pascal). Esta unidad construye la resolución de device + el fallback:
un fallo de CUDA al cargar (driver, VRAM ocupada, DLL de cuBLAS ausente del PATH)
JAMÁS puede dejar al usuario sin dictado, así que siempre cae a CPU.

**Ampliado el mismo día (fix del verificador independiente):** el fallback de
`_load_model()` protege la CARGA, no la INFERENCIA. `TestInferenceFallback`
cubre el hueco real que encontró: un modelo que carga bien en CUDA y falla
DESPUÉS, a media sesión, al llamar `model.transcribe()` (VRAM tomada por otra
app, reset de driver). El fix atrapa ese fallo en `transcribe()`/`translate()`,
libera el modelo roto de forma segura (`release()`, respeta `_inflight`) y
reintenta la MISMA llamada en CPU; y abre un breaker con cooldown
(`LOCAL_CUDA_FALLBACK_COOLDOWN`) para no reintentar CUDA en cada carga
mientras el problema persiste.

Hermético: WhisperModel se sustituye por un doble de prueba vía
``_import_whisper_model()`` (seam explícito para esto). Estos tests corren en
CUALQUIER máquina, con o sin GPU real: nunca dependen de que CUDA esté
instalado. La única excepción es TestEnsureCudaOnPath, que tampoco toca la GPU:
solo ejercita el glob de archivos sobre un directorio temporal.
"""
import io
import os

import pytest

import core.backends.local_backend as local_backend
from core.backends.local_backend import LocalBackend, _ensure_cuda_on_path, _requested_device


# ---------------------------------------------------------------------------
# Doble de prueba para WhisperModel: nunca toca GPU/CPU real ni descarga nada.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_whisper(monkeypatch):
    """Sustituye ``_import_whisper_model()`` por una fábrica configurable.

    ``fake.calls``            -> lista de dicts (kwargs de cada intento de
                                 CONSTRUCCIÓN, en orden). Es el guardián real
                                 de los tests de carga: si _load_model()
                                 dejara de intentar CUDA, o dejara de caer a
                                 CPU tras un fallo, la lista no coincidiría.
    ``fake.fail_on``           -> set de devices que deben lanzar al
                                 CONSTRUIRSE (simula un fallo de CARGA).
    ``fake.transcribe_calls``  -> lista de dicts {"device", "kwargs"} de cada
                                 llamada a ``.transcribe()`` (INFERENCIA), en
                                 orden. Guardián de los tests de fallback de
                                 inferencia.
    ``fake.fail_transcribe_on`` -> set de devices cuya llamada ``.transcribe()``
                                 debe lanzar (simula un fallo de INFERENCIA:
                                 el modelo cargó bien pero falla al usarse,
                                 el hueco real que encontró el verificador).
    ``fake.text_for_device``   -> dict device -> texto que "transcribe" ese
                                 device; permite comprobar CUÁL device produjo
                                 el resultado final devuelto al caller.
    """
    calls = []
    fail_on = set()
    transcribe_calls = []
    fail_transcribe_on = set()
    text_for_device = {}

    class _FakeSegment:
        def __init__(self, text):
            self.text = text

    class _FakeModel:
        def __init__(self, model_size, **kwargs):
            self.device = kwargs.get("device")
            calls.append({"model_size": model_size, **kwargs})
            if self.device in fail_on:
                raise RuntimeError(f"fake: fallo simulado cargando en {self.device}")

        def transcribe(self, wav_buffer, **kwargs):
            transcribe_calls.append({"device": self.device, "kwargs": kwargs})
            if self.device in fail_transcribe_on:
                raise RuntimeError(f"fake: fallo simulado de INFERENCIA en {self.device}")
            text = text_for_device.get(self.device, f"texto-{self.device}")
            return iter([_FakeSegment(text)]), None

    monkeypatch.setattr(local_backend, "_import_whisper_model", lambda: _FakeModel)

    class _Handle:
        pass

    handle = _Handle()
    handle.calls = calls
    handle.fail_on = fail_on
    handle.transcribe_calls = transcribe_calls
    handle.fail_transcribe_on = fail_transcribe_on
    handle.text_for_device = text_for_device
    return handle


@pytest.fixture
def stub_cuda_path_search(monkeypatch):
    """Sustituye _ensure_cuda_on_path() por un doble que solo cuenta llamadas.

    Necesario porque esta MÁQUINA (la de Johann) tiene CUDA instalado de
    verdad (ver el banco de la unidad 7a): sin este stub, los tests de
    resolución de device mutarían el PATH real del proceso de pytest. La
    función real se prueba aparte en TestEnsureCudaOnPath, con un directorio
    temporal, sin tocar este stub."""
    calls = []
    monkeypatch.setattr(local_backend, "_ensure_cuda_on_path", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("LOCAL_WHISPER_MODEL", "small")
    return LocalBackend()


# ---------------------------------------------------------------------------
# _requested_device(): normalización pura de LOCAL_DEVICE
# ---------------------------------------------------------------------------

class TestRequestedDevice:
    def test_default_is_auto_when_unset(self, monkeypatch):
        monkeypatch.delenv("LOCAL_DEVICE", raising=False)
        assert _requested_device() == "auto"

    def test_explicit_auto(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DEVICE", "auto")
        assert _requested_device() == "auto"

    def test_explicit_cpu(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DEVICE", "cpu")
        assert _requested_device() == "cpu"

    def test_explicit_cuda(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        assert _requested_device() == "cuda"

    def test_case_and_whitespace_tolerant(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DEVICE", "  CUDA  ")
        assert _requested_device() == "cuda"

    def test_unrecognized_value_falls_back_to_auto(self, monkeypatch):
        """Fail-open deliberado: LOCAL_DEVICE no es un control de seguridad
        (al revés de DASHBOARD_AUTH_ENABLED), así que un typo no debe dejar
        al usuario sin dictado."""
        monkeypatch.setenv("LOCAL_DEVICE", "gpu")  # typo común, no es un valor válido
        assert _requested_device() == "auto"


# ---------------------------------------------------------------------------
# LocalBackend._load_model(): resolución de device + fallback a CPU
# ---------------------------------------------------------------------------

class TestLoadModelDeviceResolution:
    def test_cuda_requested_and_succeeds_uses_cuda(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        backend._load_model()

        assert backend.get_device() == "cuda"
        assert len(fake_whisper.calls) == 1
        assert fake_whisper.calls[0]["device"] == "cuda"
        assert fake_whisper.calls[0]["compute_type"] == "int8"
        # cpu_threads es un concepto solo-CPU: no debe viajar en la llamada CUDA.
        assert "cpu_threads" not in fake_whisper.calls[0]
        assert len(stub_cuda_path_search) == 1  # se intentó el blindaje de PATH

    def test_auto_prefers_cuda_when_it_loads(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.delenv("LOCAL_DEVICE", raising=False)
        backend._load_model()

        assert backend.get_device() == "cuda"
        assert len(fake_whisper.calls) == 1
        assert fake_whisper.calls[0]["device"] == "cuda"

    def test_cuda_requested_but_fails_falls_back_to_cpu(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """El caso central de la unidad: un fallo de CUDA al cargar (driver,
        VRAM ocupada, DLL ausente) NUNCA deja al usuario sin dictado."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        fake_whisper.fail_on.add("cuda")

        model = backend._load_model()  # no debe lanzar

        assert model is not None
        assert backend.get_device() == "cpu"
        assert [c["device"] for c in fake_whisper.calls] == ["cuda", "cpu"]
        cpu_call = fake_whisper.calls[1]
        assert cpu_call["compute_type"] == "int8"
        assert "cpu_threads" in cpu_call

    def test_auto_falls_back_to_cpu_when_cuda_fails(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.delenv("LOCAL_DEVICE", raising=False)
        fake_whisper.fail_on.add("cuda")

        backend._load_model()

        assert backend.get_device() == "cpu"
        assert [c["device"] for c in fake_whisper.calls] == ["cuda", "cpu"]

    def test_cpu_forced_never_attempts_cuda(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.setenv("LOCAL_DEVICE", "cpu")
        backend._load_model()

        assert backend.get_device() == "cpu"
        assert len(fake_whisper.calls) == 1
        assert fake_whisper.calls[0]["device"] == "cpu"
        assert fake_whisper.calls[0]["compute_type"] == "int8"
        assert "cpu_threads" in fake_whisper.calls[0]
        # LOCAL_DEVICE=cpu no intenta CUDA en absoluto: ni construcción ni blindaje de PATH.
        assert len(stub_cuda_path_search) == 0

    def test_unrecognized_value_behaves_like_auto(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.setenv("LOCAL_DEVICE", "gpu")
        backend._load_model()

        assert backend.get_device() == "cuda"
        assert fake_whisper.calls[0]["device"] == "cuda"

    def test_get_device_is_none_before_first_load(self, backend):
        assert backend.get_device() is None

    def test_model_cached_after_first_load(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """_load_model() es idempotente: una segunda llamada no reconstruye
        el modelo (comportamiento preexistente, no debe romperse)."""
        monkeypatch.setenv("LOCAL_DEVICE", "cpu")
        first = backend._load_model()
        second = backend._load_model()

        assert first is second
        assert len(fake_whisper.calls) == 1

    def test_release_clears_device(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        monkeypatch.setenv("LOCAL_DEVICE", "cpu")
        backend._load_model()
        assert backend.get_device() == "cpu"

        backend.release()
        assert backend.get_device() is None


# ---------------------------------------------------------------------------
# _ensure_cuda_on_path(): blindaje de PATH, con search_bases inyectado (seam
# de testabilidad) para no depender de si ESTA máquina tiene CUDA instalado.
# ---------------------------------------------------------------------------

class TestEnsureCudaOnPath:
    def test_noop_when_dll_already_on_path(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "already_on_path"
        bin_dir.mkdir()
        (bin_dir / "cublas64_12.dll").write_bytes(b"")
        monkeypatch.setenv("PATH", str(bin_dir))

        _ensure_cuda_on_path(search_bases=[str(tmp_path / "nonexistent_base")])

        assert os.environ["PATH"] == str(bin_dir)  # sin cambios

    def test_prepends_bin_dir_when_found_under_search_base(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", r"C:\some\unrelated\dir")
        toolkit = tmp_path / "Toolkit" / "CUDA"
        bin_dir = toolkit / "v12.9" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cublas64_12.dll").write_bytes(b"")

        _ensure_cuda_on_path(search_bases=[str(toolkit)])

        new_path_dirs = os.environ["PATH"].split(os.pathsep)
        assert str(bin_dir) == new_path_dirs[0]
        assert r"C:\some\unrelated\dir" in new_path_dirs

    def test_does_not_hardcode_minor_version(self, tmp_path, monkeypatch):
        """No hardcodea v12.9: busca por patrón v12.*, un v12.4 (versión menor
        distinta) también debe encontrarse."""
        monkeypatch.setenv("PATH", r"C:\some\unrelated\dir")
        toolkit = tmp_path / "Toolkit" / "CUDA"
        bin_dir = toolkit / "v12.4" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cublas64_12.dll").write_bytes(b"")

        _ensure_cuda_on_path(search_bases=[str(toolkit)])

        assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)

    def test_noop_when_nothing_found_anywhere(self, tmp_path, monkeypatch):
        original_path = r"C:\some\unrelated\dir"
        monkeypatch.setenv("PATH", original_path)

        _ensure_cuda_on_path(search_bases=[str(tmp_path / "empty_base")])

        assert os.environ["PATH"] == original_path  # nunca falla, y no inventa nada

    def test_ignores_v13_toolkit(self, tmp_path, monkeypatch):
        """El Toolkit v13.x instalado en la máquina de Johann (medido en el
        banco de 7a) NO lo usan faster-whisper/ctranslate2: solo v12.* cuenta."""
        monkeypatch.setenv("PATH", r"C:\some\unrelated\dir")
        toolkit = tmp_path / "Toolkit" / "CUDA"
        v13_bin = toolkit / "v13.3" / "bin"
        v13_bin.mkdir(parents=True)
        (v13_bin / "cublas64_12.dll").write_bytes(b"")  # improbable en la vida real, pero prueba el patrón
        # Además probamos el caso realista: v13 SIN el dll de la serie 12.
        (v13_bin / "cublas64_12.dll").unlink()

        _ensure_cuda_on_path(search_bases=[str(toolkit)])

        assert os.environ["PATH"] == r"C:\some\unrelated\dir"

    def test_never_raises_on_unexpected_error(self, monkeypatch):
        """Hallazgo del verificador independiente: la garantía de "nunca
        falla" no debe descansar en que os.path.exists/glob.glob de la
        stdlib jamás lancen. Simula un fallo inesperado de glob.glob y
        confirma que _ensure_cuda_on_path lo atrapa sin propagar."""
        monkeypatch.setenv("PATH", r"C:\some\unrelated\dir")

        def _boom(*_args, **_kwargs):
            raise OSError("fallo simulado de glob.glob")

        monkeypatch.setattr(local_backend.glob, "glob", _boom)

        _ensure_cuda_on_path(search_bases=["cualquier-cosa"])  # no debe lanzar

        assert os.environ["PATH"] == r"C:\some\unrelated\dir"  # sin cambios


# ---------------------------------------------------------------------------
# Fallback de INFERENCIA (fix del verificador independiente, 2026-08-01): un
# modelo que carga bien en CUDA y falla DESPUÉS, a media sesión, al llamar
# model.transcribe() (VRAM tomada por otra app, reset de driver). Distinto
# del fallback de CARGA que ya cubre TestLoadModelDeviceResolution.
# ---------------------------------------------------------------------------

class TestInferenceFallback:
    def test_cuda_inference_failure_retries_on_cpu_and_succeeds(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """El caso central del hueco: la carga en CUDA fue perfecta, pero
        model.transcribe() revienta (p. ej. VRAM tomada por OBS a mitad de
        sesión). No debe perderse el audio: se reintenta en CPU."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        fake_whisper.fail_transcribe_on.add("cuda")
        fake_whisper.text_for_device["cpu"] = "recuperado en cpu"

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        text = backend.transcribe(wav, language="es")

        assert text == "recuperado en cpu"
        assert [c["device"] for c in fake_whisper.transcribe_calls] == ["cuda", "cpu"]
        # El modelo CUDA roto quedó liberado (release() diferido se ejecuta
        # al salir de _exit_inflight, sin más trabajos en curso).
        assert backend.get_device() is None

    def test_cpu_inference_failure_propagates_unchanged(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """Si el device en uso YA era CPU, el fallo se propaga tal cual: no
        hay una tercera vía, y este comportamiento NO debe cambiar."""
        monkeypatch.setenv("LOCAL_DEVICE", "cpu")
        fake_whisper.fail_transcribe_on.add("cpu")

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        with pytest.raises(RuntimeError, match="fallo simulado de INFERENCIA en cpu"):
            backend.transcribe(wav, language="es")

        # Un solo intento: nunca hay "de dónde más" reintentar si ya era CPU.
        assert [c["device"] for c in fake_whisper.transcribe_calls] == ["cpu"]

    def test_cuda_inference_failure_and_cpu_retry_also_fails_propagates(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """Si el reintento en CPU TAMBIÉN falla, es un fallo genuino: se
        propaga (la excepción del reintento, no la original de CUDA)."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        fake_whisper.fail_transcribe_on.update({"cuda", "cpu"})

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        with pytest.raises(RuntimeError, match="fallo simulado de INFERENCIA en cpu"):
            backend.transcribe(wav, language="es")

        assert [c["device"] for c in fake_whisper.transcribe_calls] == ["cuda", "cpu"]

    def test_translate_native_branch_cuda_failure_retries_on_cpu(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """Mismo fallback en translate() (rama nativa task=translate)."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        fake_whisper.fail_transcribe_on.add("cuda")
        fake_whisper.text_for_device["cpu"] = "translated on cpu"

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        text = backend.translate(wav, target_lang="en")

        assert text == "translated on cpu"
        assert [c["device"] for c in fake_whisper.transcribe_calls] == ["cuda", "cpu"]

    def test_translate_non_en_branch_cuda_failure_retries_on_cpu(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """Mismo fallback en translate() (rama target != "en", que degrada a
        transcripción en idioma original sin traducir)."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        fake_whisper.fail_transcribe_on.add("cuda")
        fake_whisper.text_for_device["cpu"] = "transcrito en cpu"

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        text = backend.translate(wav, target_lang="fr")

        assert text == "transcrito en cpu"
        assert [c["device"] for c in fake_whisper.transcribe_calls] == ["cuda", "cpu"]

    def test_breaker_skips_cuda_on_next_load_after_inference_failure(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """No repetir el fallo cada vez: tras un fallo de inferencia en CUDA,
        la SIGUIENTE carga del modelo (p. ej. tras el release diferido) no
        vuelve a intentar CUDA mientras el breaker esté abierto."""
        monkeypatch.setenv("LOCAL_DEVICE", "auto")
        fake_whisper.fail_transcribe_on.add("cuda")

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        backend.transcribe(wav, language="es")  # dispara el fallback, abre el breaker
        assert backend.get_device() is None  # modelo roto liberado

        fake_whisper.calls.clear()  # medir SOLO la siguiente carga, limpia
        backend._load_model()

        assert backend.get_device() == "cpu"
        assert [c["device"] for c in fake_whisper.calls] == ["cpu"]  # nunca intentó CUDA

    def test_successful_cuda_inference_resets_open_breaker(
        self, monkeypatch, backend, fake_whisper, stub_cuda_path_search
    ):
        """Un éxito de inferencia en CUDA resetea el breaker (mismo criterio
        que Transcriber._run_net_fallback en core/transcriber.py)."""
        monkeypatch.setenv("LOCAL_DEVICE", "cuda")
        backend._load_model()
        assert backend.get_device() == "cuda"

        # Simula que un fallo anterior ya había abierto el breaker, sin
        # destruir el modelo actualmente cargado (que sigue funcionando).
        backend._cuda_breaker_trip()
        assert backend._cuda_breaker_open() is True

        wav = io.BytesIO(b"contenido-wav-de-prueba")
        text = backend.transcribe(wav, language="es")

        assert text  # la inferencia tuvo éxito
        assert backend._cuda_breaker_open() is False  # y reseteó el breaker

    def test_cuda_breaker_cooldown_reads_env_var(self, monkeypatch, backend):
        monkeypatch.setenv("LOCAL_CUDA_FALLBACK_COOLDOWN", "45")
        assert backend._cuda_retry_cooldown() == 45.0

    def test_cuda_breaker_cooldown_default_is_300(self, monkeypatch, backend):
        monkeypatch.delenv("LOCAL_CUDA_FALLBACK_COOLDOWN", raising=False)
        assert backend._cuda_retry_cooldown() == 300.0
