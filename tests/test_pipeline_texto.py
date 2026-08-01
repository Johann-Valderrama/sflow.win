"""Tests de la unidad 0b del plan `docs/PLAN-DICTADO-2026-07-31.md`.

Hace ejecutable el contrato de `CLAUDE.md`, sección 19 ("Contrato del pipeline de
texto"), que tiene TRES ejes: ORDEN, ALCANCE y PRESUPUESTO de latencia. Nace del
hallazgo E3 de la evaluación del plan: **cero fixtures del repo contienen una frase
disparadora** ("hay dos puntos importantes", "entró en coma profundo", "signo
coma", "nueva línea"), así que la suite entera seguía en verde con el pipeline de
texto ya corrompido — nadie lo habría notado.

Dos capas:

- **CAPA A** (corre HOY, es la mayoría del valor): guardián de ALCANCE por
  reunión y por URL (con un backend mockeado que devuelve texto CON frases
  disparadoras), guardián estructural (transcriber.py no puede importar las
  pasadas 3/4), y el caso de dictado con lo que YA existe (orden filtro->
  diccionario, ensamblado de chunks completo).
- **CAPA B** (se activa sola cuando aterricen las Olas 1 y 4): orden 3 antes
  de 4, el caso que originó la Ola 0 (salto de línea explícito sobrevive al
  reformateo), los negativos de smart commands (prefijo obligatorio), y el
  presupuesto de latencia del Eje 3 (50ms para 3+4 juntas). Usa
  `pytest.mark.skipif` sobre la existencia del módulo, con un `reason` que
  dice literalmente qué unidad falta.

Todo mockeado (sin red, sin Groq/LLM real): reusa el patrón ya establecido en
`tests/test_raw_undo.py` (parchear `core.backends.groq_backend.Groq` + resetear
la caché global de `core.dictionary` y el singleton de `core.backends`) y en
`tests/test_dictation_modes.py`/`tests/test_chunk_assembly.py` (replicar lógica
de `main.py` sin instanciar `VflowApp`, que exige `QApplication`/hotkeys/recorder
reales).
"""
import importlib.util
import io
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fixture central: texto con las CUATRO frases disparadoras del hallazgo E3.
# Deliberadamente > 80 caracteres para no rozar el umbral de
# `_HALLUCINATION_MAX_LENGTH` de core/transcriber.py.
# ---------------------------------------------------------------------------
TRIGGER_TEXT = (
    "hay dos puntos importantes que revisar antes de la reunión, "
    "dijo signo coma y siguió hablando aunque después entró en coma profundo "
    "y pidió una nueva línea para cerrar la nota."
)
assert len(TRIGGER_TEXT) > 80
for _frase in (
    "hay dos puntos importantes que revisar",
    "entró en coma profundo",
    "dijo signo coma y siguió",
    "nueva línea",
):
    assert _frase in TRIGGER_TEXT


def _reset_dictionary_cache():
    """Aísla la caché global de core.dictionary (patrón de test_raw_undo.py):
    sin esto, `apply_replacements` podría recompilar contra la DB real del
    dev (o quedarse con pares de un test anterior) y romper la igualdad
    exacta que estos guardianes verifican."""
    from core import dictionary
    dictionary._db = None
    dictionary._cache = dictionary._EMPTY_CACHE


def _reset_backend_singletons():
    """Limpia el singleton de core.backends (patrón de test_raw_undo.py): sin
    esto, un backend mockeado por un test anterior puede quedar cacheado y
    contaminar el siguiente."""
    from core.backends import _instances, _instances_lock
    with _instances_lock:
        _instances.clear()


@pytest.fixture(autouse=True)
def _aislar_estado_global():
    _reset_dictionary_cache()
    _reset_backend_singletons()
    yield
    _reset_dictionary_cache()
    _reset_backend_singletons()


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


_SMART_COMMANDS_AVAILABLE = _module_available("core.smart_commands")
_SNIPPETS_AVAILABLE = _module_available("core.snippets_matcher")

_SMART_COMMANDS_REASON = "la Ola 1 (unidad 1a, core/smart_commands.py) aún no existe"
_SNIPPETS_REASON = "la Ola 4 (unidad 4a, core/snippets_matcher.py) aún no existe"
_AMBAS_OLAS_REASON = (
    "las Olas 1 y 4 (core/smart_commands.py y core/snippets_matcher.py) aún no existen"
)


def _apply_smart_commands_or_fail(text: str) -> str:
    """Contrato ASUMIDO para `core.smart_commands` (unidad 1a, aún no escrita):
    una función `apply_smart_commands(text: str) -> str`. Si la Ola 1 aterriza
    con otro nombre, este helper hace FALLAR el test (no lo saltea en
    silencio) señalando exactamente qué ajustar aquí — esa es la señal
    correcta para quien construya la unidad 1a, no un falso verde."""
    import core.smart_commands as _sc  # noqa: PLC0415
    fn = getattr(_sc, "apply_smart_commands", None)
    if fn is None:
        pytest.fail(
            "core.smart_commands existe pero no expone apply_smart_commands(text). "
            "Ajusta el nombre asumido en tests/test_pipeline_texto.py (unidad 0b) "
            "al contrato real que definió la unidad 1a."
        )
    return fn(text)


def _expand_snippets_or_fail(text: str) -> str:
    """Contrato ASUMIDO para `core.snippets_matcher` (unidad 4a, aún no
    escrita): una función `expand_snippets(text: str) -> str`. Mismo criterio
    que `_apply_smart_commands_or_fail`."""
    import core.snippets_matcher as _sm  # noqa: PLC0415
    fn = getattr(_sm, "expand_snippets", None)
    if fn is None:
        pytest.fail(
            "core.snippets_matcher existe pero no expone expand_snippets(text). "
            "Ajusta el nombre asumido en tests/test_pipeline_texto.py (unidad 0b) "
            "al contrato real que definió la unidad 4a."
        )
    return fn(text)


# ===========================================================================
# CAPA A — corre HOY. Es la mayoría del valor de esta unidad.
# ===========================================================================

# ---------------------------------------------------------------------------
# Guardián ESTRUCTURAL (Eje 2, barato y durable): transcriber.py no puede
# saber que smart commands/snippets existen.
# ---------------------------------------------------------------------------
class TestAlcanceEstructural:
    """Si `core/transcriber.py` llega a mencionar `smart_commands` o
    `snippets_matcher`, reunión (`core/meeting.py`) y URL
    (`core/url_transcribe.py`) las heredarían por construcción — comparten la
    misma clase `Transcriber`. Ver CLAUDE.md sección 19, "Por qué el eje de
    ALCANCE existe"."""

    def test_transcriber_no_referencia_smart_commands_ni_snippets(self):
        source = (REPO_ROOT / "core" / "transcriber.py").read_text(encoding="utf-8")
        for prohibido in ("smart_commands", "snippets_matcher"):
            assert prohibido not in source, (
                f"core/transcriber.py menciona {prohibido!r}. Eso viola el Eje 2 "
                "(ALCANCE) del contrato del pipeline de texto (CLAUDE.md sección "
                "19): smart commands y snippets SOLO pueden vivir en main.py, "
                "bajo los mismos gates que usa dictation_modes "
                "('not translate' y 'recorder.source != \"system\"'). Si viven "
                "en Transcriber, reunión y URL las heredan por construcción, "
                "metiendo puntuación/expansión de snippets en habla de TERCEROS "
                "dentro de actas y transcripciones — el defecto grave que casi "
                "entró en la primera versión de este plan."
            )


# ---------------------------------------------------------------------------
# Guardián de ALCANCE por flujo: reunión y URL reciben el texto del backend
# TAL CUAL (salvo filtro de alucinaciones y diccionario, que sí aplican a los
# tres flujos por diseño). Esto FALLA en el instante en que alguien cablee
# smart commands o snippets dentro de core/transcriber.py.
#
# Nota de diseño: NO se mockea Transcriber.transcribe() completo (eso
# ocultaría exactamente la mutación que este guardián debe cazar). Se mockea
# solo la CAPA DE BACKEND (core.transcriber.get_backend), así el código REAL
# de Transcriber.transcribe() -incluida cualquier pasada que alguien cuele
# ahí- corre de verdad.
# ---------------------------------------------------------------------------
class TestAlcanceReunionPreservaTextoDelBackend:
    def test_meeting_flow_entrega_texto_identico_al_backend(self, monkeypatch):
        import core.transcriber as _transcriber_mod
        from core.meeting import MeetingSession
        from core import vad as _vad_mod

        fake_backend = MagicMock()
        fake_backend.transcribe.return_value = TRIGGER_TEXT
        monkeypatch.setattr(_transcriber_mod, "get_backend", lambda name=None: fake_backend)
        # VAD real no aporta nada aquí (frames sintéticos en silencio) y sería
        # el único motivo de lentitud/flakiness; se mockea a "sin voz" y el
        # pipeline de texto sigue igual (el VAD de métricas es ortogonal al
        # contrato de esta unidad).
        monkeypatch.setattr(_vad_mod, "speech_timestamps", lambda audio: [])

        session = MeetingSession()
        mic_frames = [np.zeros((1600, 1), dtype=np.int16)]
        session._process_window(0.0, mic_frames, [])

        assert len(session._segments) == 1, "el segmento no se agregó al transcript de la reunión"
        assert session._segments[0]["text"] == TRIGGER_TEXT, (
            "El texto que llegó al acta de la reunión NO es idéntico al que "
            "devolvió el backend (mockeado). Si esto falla, alguien cableó una "
            "pasada nueva (smart commands o snippets) DENTRO de "
            "core/transcriber.py: reunión la heredaría por construcción, y ese "
            "texto -habla de OTRA persona, no del usuario- terminaría con "
            "puntuación o expansiones que nadie pidió. Ver CLAUDE.md sección "
            "19, Eje 2 (ALCANCE)."
        )


class TestAlcanceUrlPreservaTextoDelBackend:
    def test_url_flow_entrega_texto_identico_al_backend(self, monkeypatch):
        import core.transcriber as _transcriber_mod
        from core.transcriber import Transcriber
        from core.url_transcribe import _transcribe_pcm_chunked

        fake_backend = MagicMock()
        fake_backend.transcribe.return_value = TRIGGER_TEXT
        monkeypatch.setattr(_transcriber_mod, "get_backend", lambda name=None: fake_backend)

        transcriber = Transcriber()
        # Muy por debajo de _CHUNK_SECONDS*_SAMPLE_RATE: fuerza el camino de
        # "audio corto, un solo chunk", que llama al backend UNA vez y
        # devuelve su resultado sin más transformación.
        pcm = np.zeros(1600, dtype=np.int16)
        result = _transcribe_pcm_chunked(pcm, transcriber, None)

        assert result == TRIGGER_TEXT, (
            "El texto que salió del flujo de URL/YouTube NO es idéntico al que "
            "devolvió el backend (mockeado). Mismo argumento que la reunión: "
            "quien dicta en el video es un TERCERO, y si smart commands o "
            "snippets se colaran en core/transcriber.py los heredaría por "
            "construcción. Ver CLAUDE.md sección 19, Eje 2 (ALCANCE)."
        )


# ---------------------------------------------------------------------------
# Caso de dictado HOY: lo que ya existe (filtro -> diccionario, ensamblado).
# ---------------------------------------------------------------------------
class TestDictadoOrdenFiltroAntesQueDiccionario:
    """Eje 1 (ORDEN) del contrato, en el camino real de main.py (transcribe()
    con return_raw=True/net_fallback=True, llamado desde main.py:777/854)."""

    @patch("core.backends.groq_backend.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_filtro_de_alucinaciones_corre_antes_que_el_diccionario(
        self, mock_groq_cls, tmp_path, monkeypatch
    ):
        """Caso donde el ORDEN sí cambia el resultado: el backend devuelve una
        alucinación EXACTA conocida ("gracias"), y hay una entrada de
        diccionario que -si corriera ANTES del filtro- la transformaría en un
        texto que ya no coincide con la lista de alucinaciones, dejando pasar
        ruido de Whisper al usuario. Con el orden correcto (filtro primero),
        el resultado es "" antes de que el diccionario vea el texto."""
        from core import dictionary
        from db.database import TranscriptionDB
        from core.transcriber import Transcriber

        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        db.add_dictionary_entry(replace_to="Gracias totales", replace_from="gracias")
        monkeypatch.setattr(dictionary, "_db", db)
        dictionary.invalidate()

        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "gracias"

        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        text = t.transcribe(buf)

        assert text == "", (
            "El backend devolvió 'gracias' (alucinación EXACTA conocida de "
            "Whisper en silencio, ver _HALLUCINATION_EXACT en "
            "core/transcriber.py). Si el diccionario corriera ANTES del "
            "filtro (orden invertido), 'gracias' se habría convertido en "
            "'Gracias totales' -que ya NO coincide con la lista de "
            "alucinaciones- y ese ruido habría llegado al usuario. Eje 1 del "
            "contrato (CLAUDE.md sección 19): 1 (filtro) antes que 2 "
            "(diccionario), siempre."
        )


class TestDictadoEnsambladoLlegaCompleto:
    """Replica la lógica de ensamblado + gates de `main.py::_transcribe_final`
    (el join de main.py:882, los gates de dictation_modes en main.py:897-918),
    SIN instanciar `VflowApp` completo (exige QApplication/hotkeys/recorder
    reales) — mismo criterio que `tests/test_chunk_assembly.py` (unidad 0.2) y
    `tests/test_dictation_modes.py::TestRawTextMostRaw` (unidad 6.3)."""

    @staticmethod
    def _simulate_assembly_and_gates(chunk_results, *, translate, source,
                                      modes_on, preset, reformatted):
        from core import dictation_modes as _dm
        with patch.object(_dm, "modes_enabled", return_value=modes_on), \
             patch.object(_dm, "preset_for_exe", return_value=preset), \
             patch.object(_dm, "reformat_text", return_value=reformatted):
            # main.py:882 — ensamblado en orden de índice, sin perder ningún chunk.
            text = " ".join(chunk_results[k] for k in sorted(chunk_results))
            # main.py:897-918 — gates de dictation_modes (idénticos a los que
            # main.py reusará para smart commands/snippets, Ola 1/4).
            if not translate and source != "system" and _dm.modes_enabled():
                p = _dm.preset_for_exe("dummy.exe")
                if p:
                    new_text = _dm.reformat_text(text, p)
                    if new_text and new_text != text:
                        text = new_text
            return text

    def test_chunks_desordenados_se_ensamblan_en_orden_y_completos(self):
        chunk_results = {2: "tercera parte", 0: "primera parte", 1: "segunda parte"}
        text = self._simulate_assembly_and_gates(
            chunk_results, translate=False, source="mic",
            modes_on=False, preset=None, reformatted=None,
        )
        assert text == "primera parte segunda parte tercera parte"

    def test_frase_disparadora_llega_entera_sin_reformateo(self):
        """Con dictation_modes apagado (default de fábrica), el texto
        ensamblado -incluida una frase disparadora completa- llega intacto:
        nada lo toca entre el ensamblado y el pegado."""
        chunk_results = {0: TRIGGER_TEXT}
        text = self._simulate_assembly_and_gates(
            chunk_results, translate=False, source="mic",
            modes_on=False, preset=None, reformatted=None,
        )
        assert text == TRIGGER_TEXT

    def test_gate_traduccion_nunca_reformatea(self):
        chunk_results = {0: "hello world"}
        text = self._simulate_assembly_and_gates(
            chunk_results, translate=True, source="mic",
            modes_on=True, preset="email", reformatted="Should not apply",
        )
        assert text == "hello world"

    def test_gate_audio_de_sistema_nunca_reformatea(self):
        chunk_results = {0: "algo del video"}
        text = self._simulate_assembly_and_gates(
            chunk_results, translate=False, source="system",
            modes_on=True, preset="chat", reformatted="Should not apply",
        )
        assert text == "algo del video"


# ===========================================================================
# CAPA B — se activa sola cuando aterricen las Olas 1 (smart commands) y 4
# (snippets). Hoy ninguno de los dos módulos existe: estos tests se SALTAN
# con un motivo explícito (nunca se omiten en silencio ni fallan por
# ImportError).
# ===========================================================================

@pytest.mark.skipif(not _SMART_COMMANDS_AVAILABLE, reason=_SMART_COMMANDS_REASON)
class TestSmartCommandsNegativos:
    """Los disparadores llevan PREFIJO obligatorio ('signo'/'puntuación')
    -decisión de Johann, PLAN-DICTADO-2026-07-31 Ola 1- precisamente para no
    atrapar habla normal en español. Casos negativos citados en el propio
    plan (línea de la unidad 1a)."""

    @pytest.mark.parametrize("frase", [
        "hay dos puntos importantes que revisar",
        "entró en coma profundo",
    ])
    def test_palabra_pelada_no_se_toca(self, frase):
        resultado = _apply_smart_commands_or_fail(frase)
        assert resultado == frase, (
            f"{frase!r} se modificó sin llevar el prefijo 'signo'/'puntuación'. "
            "El plan (Ola 1, decisión de Johann 2026-07-31) exige que SOLO la "
            "forma con prefijo dispare la regla, para no atrapar habla normal "
            "en español."
        )


@pytest.mark.skipif(not _SMART_COMMANDS_AVAILABLE, reason=_SMART_COMMANDS_REASON)
class TestSaltoDeLineaExplicitoSobreviveAlReformateo:
    """El caso concreto que originó la Ola 0 (CLAUDE.md sección 19): si dictas
    "nueva línea", smart commands mete un '\\n', y el preset 'email' puede
    comerse ese salto al re-fluir párrafos.

    Esta suite NO llama LLMs reales, así que no puede probar que el modelo
    real respete la instrucción del prompt (eso es responsabilidad de la Ola
    2, en los PROMPTS de los presets). Lo que SÍ es verificable con mocks es
    el CABLEADO: que el texto que llega a `reformat_text` ya trae el '\\n' que
    puso smart commands (orden 3 antes que 5), y que si el LLM (mockeado) lo
    devuelve intacto, nadie más lo vuelve a tocar."""

    def test_salto_de_linea_llega_intacto_a_reformat_text(self, monkeypatch):
        from core import dictation_modes as _dm

        dictated = "primer párrafo signo nueva línea segundo párrafo"
        with_break = _apply_smart_commands_or_fail(dictated)
        assert "\n" in with_break, (
            "Este fixture asume que smart_commands convierte 'signo nueva "
            "línea' en un salto de línea real ('\\n'). Si la Ola 1 cambió el "
            "disparador o el carácter insertado, ajusta este fixture."
        )

        captured = {}

        def _fake_reformat(text, preset, timeout=None):
            captured["text"] = text
            return text  # simula un LLM que SÍ respeta el salto explícito

        monkeypatch.setattr(_dm, "reformat_text", _fake_reformat)
        result = _dm.reformat_text(with_break, "email")

        assert captured["text"] == with_break, (
            "reformat_text NO recibió el texto CON el salto de línea que puso "
            "smart commands — el orden 3 antes que 5 (CLAUDE.md sección 19, "
            "Eje 1) está roto."
        )
        assert "\n" in result


@pytest.mark.skipif(
    not (_SMART_COMMANDS_AVAILABLE and _SNIPPETS_AVAILABLE), reason=_AMBAS_OLAS_REASON
)
class TestOrden3AntesDe4:
    """Eje 1 del contrato: smart commands (3) corre ANTES que snippets (4).
    Razón (CLAUDE.md sección 19): el texto que expande un snippet es texto
    que el usuario ESCRIBIÓ y ya viene puntuado; si los snippets corrieran
    primero, smart commands volvería a escanear ese texto guardado y
    mutilaría cualquier palabra literal que contenga (p. ej. un snippet cuyo
    texto diga literalmente "signo coma"). Con el orden correcto, lo que
    inserta un snippet no lo vuelve a tocar nadie."""

    def test_texto_insertado_por_snippet_no_se_reprocesa_por_smart_commands(self, monkeypatch):
        import core.snippets_matcher as _sm

        # El snippet guardado por el usuario contiene LITERALMENTE la cadena
        # "signo coma" (el usuario lo escribió así a propósito, ya puntuado).
        snippet_expansion = "recuerda que el disparador se escribe como signo coma"
        monkeypatch.setattr(
            _sm, "expand_snippets",
            lambda text: text.replace("mi firma", snippet_expansion),
        )

        dictated_text = "hasta pronto mi firma"

        def _simulate_main_py_order(text):
            # Orden del contrato: 3 (smart commands) corre sobre el texto
            # ensamblado ANTES de que 4 (snippets) inserte nada.
            after_smart_commands = _apply_smart_commands_or_fail(text)
            after_snippets = _expand_snippets_or_fail(after_smart_commands)
            return after_snippets

        result = _simulate_main_py_order(dictated_text)

        assert "signo coma" in result, (
            "El texto literal insertado por el snippet ('signo coma') no "
            "llegó intacto: algo lo mutiló. Con el orden correcto (3 antes "
            "que 4), smart_commands ya corrió ANTES de que snippets "
            "insertara este texto, así que nadie más debería tocarlo. Si "
            "esto falla, sospecha de que 4 corre antes que 3 — orden "
            "invertido respecto al Eje 1 del contrato."
        )


@pytest.mark.skipif(
    not (_SMART_COMMANDS_AVAILABLE and _SNIPPETS_AVAILABLE), reason=_AMBAS_OLAS_REASON
)
class TestPresupuestoDeLatenciaEje3:
    """Eje 3 del contrato (CLAUDE.md sección 19): las pasadas locales NUEVAS
    (smart commands + snippets) no pueden superar 50ms JUNTAS, sobre un
    dictado largo (~5.000 caracteres) — el peor caso realista.

    Por qué esto vive en Capa B y no se mide hoy con lo que ya existe: el
    presupuesto de 50ms es específicamente sobre las pasadas 3+4, que no
    existen todavía; medir hoy el filtro de alucinaciones + el diccionario
    (que sí existen) mediría un número real pero DISTINTO al que exige el
    contrato, y reportarlo como "el assert del Eje 3" sería una mentira por
    omisión — mejor saltarlo con un motivo explícito que fingir cobertura.

    Margen: se afirma un techo de 250ms (5x el número del contrato) para no
    volverse un test intermitente en una máquina cargada, mientras sigue
    cazando el riesgo real que describe el contrato: que una de estas pasadas
    se convierta en una llamada de red sin que nadie lo note (eso tardaría
    cientos de ms o segundos, no los pocos ms de un regex local)."""

    def test_smart_commands_mas_snippets_bajo_el_techo(self):
        # Repite TRIGGER_TEXT hasta pasar de ~5.000 caracteres (peor caso
        # realista: un dictado largo con varias frases disparadoras).
        repeticiones = (5000 // (len(TRIGGER_TEXT) + 1)) + 2
        texto_largo = (TRIGGER_TEXT + " ") * repeticiones
        assert len(texto_largo) >= 5000

        start = time.perf_counter()
        after_commands = _apply_smart_commands_or_fail(texto_largo)
        after_snippets = _expand_snippets_or_fail(after_commands)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert after_snippets is not None
        assert elapsed_ms < 250, (
            f"smart_commands + snippets tardaron {elapsed_ms:.1f}ms sobre "
            "~5.000 caracteres — muy por encima del techo de 50ms del Eje 3 "
            "(CLAUDE.md sección 19), incluso con el margen 5x de este test. "
            "Sospecha de una llamada de red o de I/O de disco colada en una "
            "pasada que el contrato exige puramente local."
        )
