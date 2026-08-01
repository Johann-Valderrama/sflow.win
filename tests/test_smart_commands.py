"""Tests para la unidad 1a (Ola 1 de PLAN-DICTADO-2026-07-31): smart commands.

Cubre:
(a) cada comando de las dos tablas (español + inglés), positivo
(b) los negativos sin prefijo, que son la razón de ser de esta unidad: habla
    normal en español con "coma"/"dos puntos"/"punto"/"nueva línea"/"puntos
    suspensivos" pelados queda INTACTA
(c) el regalo del prefijo: dictar la palabra literal sin el prefijo
(d) orden de la tabla: la frase larga no se la come la corta
(e) case-insensitive
(f) prefijos alternativos (con y sin tilde)
(g) espaciado exacto alrededor del reemplazo
(h) killswitch SMART_COMMANDS_ENABLED (default ON, solo "false" apaga)
(i) presupuesto de latencia del Eje 3 (5 ms sobre ~5.000 caracteres)
(j) cableado en main.py::_transcribe_final (unidad 1b): camino feliz, los
    gates (traducción / audio de sistema / killswitch), la regla de raw_text,
    el orden respecto al reformateo LLM (dictation_modes) y la guarda de
    seguridad contra una pasada vacía
"""
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import core.smart_commands as smart_commands
from core.smart_commands import apply_smart_commands, smart_commands_enabled

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (a) Cada comando de las dos tablas, positivo.
# ---------------------------------------------------------------------------
class TestComandosEspanol:
    def test_punto_y_aparte(self):
        assert apply_smart_commands("primera parte signo punto y aparte segunda parte") == (
            "primera parte.\n\nsegunda parte"
        )

    def test_nuevo_parrafo_con_tilde(self):
        assert apply_smart_commands("uno signo nuevo párrafo dos") == "uno\n\ndos"

    def test_nuevo_parrafo_sin_tilde(self):
        assert apply_smart_commands("uno signo nuevo parrafo dos") == "uno\n\ndos"

    def test_nueva_linea_con_tilde(self):
        assert apply_smart_commands("uno signo nueva línea dos") == "uno\ndos"

    def test_nueva_linea_sin_tilde(self):
        assert apply_smart_commands("uno signo nueva linea dos") == "uno\ndos"

    def test_salto_de_linea_con_tilde(self):
        assert apply_smart_commands("uno signo salto de línea dos") == "uno\ndos"

    def test_salto_de_linea_sin_tilde(self):
        assert apply_smart_commands("uno signo salto de linea dos") == "uno\ndos"

    def test_punto_y_coma(self):
        assert apply_smart_commands("uno signo punto y coma dos") == "uno; dos"

    def test_dos_puntos(self):
        assert apply_smart_commands("uno signo dos puntos dos") == "uno: dos"

    def test_puntos_suspensivos(self):
        assert apply_smart_commands("uno signo puntos suspensivos dos") == "uno… dos"

    def test_coma(self):
        assert apply_smart_commands("uno signo coma dos") == "uno, dos"

    def test_punto(self):
        assert apply_smart_commands("uno signo punto dos") == "uno. dos"


class TestComandosIngles:
    def test_new_paragraph(self):
        assert apply_smart_commands("one symbol new paragraph two") == "one\n\ntwo"

    def test_new_line(self):
        assert apply_smart_commands("one symbol new line two") == "one\ntwo"

    def test_semicolon(self):
        assert apply_smart_commands("one symbol semicolon two") == "one; two"

    def test_colon(self):
        assert apply_smart_commands("one symbol colon two") == "one: two"

    def test_ellipsis(self):
        assert apply_smart_commands("one symbol ellipsis two") == "one… two"

    def test_comma(self):
        assert apply_smart_commands("one symbol comma two") == "one, two"

    def test_period(self):
        assert apply_smart_commands("one symbol period two") == "one. two"

    def test_full_stop(self):
        assert apply_smart_commands("one symbol full stop two") == "one. two"


# ---------------------------------------------------------------------------
# (b) Los negativos SIN prefijo: la razón de ser de esta unidad. Deben quedar
# INTACTOS, palabra por palabra.
# ---------------------------------------------------------------------------
class TestNegativosSinPrefijo:
    @pytest.mark.parametrize("frase", [
        "hay dos puntos importantes que revisar",
        "entró en coma profundo",
        "el punto de encuentro",
        "necesito una nueva línea de crédito",
        "vamos a ver los puntos suspensivos del contrato",
        # Caso real que hizo que el director sacara "puntuación"/"puntuacion" del
        # conjunto de prefijos (2026-07-31): "puntuación" tiene significado
        # genérico en español y dispara sin que el usuario quiera insertar nada.
        # Con el prefijo reducido a "signo"/"signos"/"symbol", esta frase queda
        # intacta. Ver el docstring de core/smart_commands.py.
        "revisemos la puntuación coma por coma",
    ])
    def test_frase_sin_prefijo_queda_intacta(self, frase):
        assert apply_smart_commands(frase) == frase


# ---------------------------------------------------------------------------
# (c) El regalo del prefijo: se puede dictar la palabra literal.
# ---------------------------------------------------------------------------
class TestRegaloDelPrefijo:
    def test_palabra_coma_literal_sin_prefijo(self):
        frase = "la palabra coma se escribe así"
        assert apply_smart_commands(frase) == frase


# ---------------------------------------------------------------------------
# (d) Orden de la tabla: la frase larga no se la come la corta.
# ---------------------------------------------------------------------------
class TestOrdenDeLaTabla:
    def test_signo_punto_y_aparte_no_dispara_punto_suelto(self):
        result = apply_smart_commands("uno signo punto y aparte dos")
        assert result == "uno.\n\ndos"
        assert "y aparte" not in result

    def test_signo_punto_y_coma_no_dispara_punto_suelto(self):
        result = apply_smart_commands("uno signo punto y coma dos")
        assert result == "uno; dos"
        assert "y coma" not in result


# ---------------------------------------------------------------------------
# (e) Case-insensitive.
# ---------------------------------------------------------------------------
class TestCaseInsensitive:
    def test_signo_coma_capitalizado(self):
        assert apply_smart_commands("uno Signo Coma dos") == "uno, dos"

    def test_signo_coma_mayusculas(self):
        assert apply_smart_commands("uno SIGNO COMA dos") == "uno, dos"


# ---------------------------------------------------------------------------
# (f) Prefijos: "signos" plural SÍ funciona; "puntuación"/"puntuacion" NO son
# prefijos válidos (decisión del director, 2026-07-31 — ver docstring del módulo).
# Antes esta clase afirmaba que "puntuación coma"/"puntuacion coma" disparaban;
# se invirtió a negativo cuando se quitaron del conjunto de prefijos.
# ---------------------------------------------------------------------------
class TestPrefijosAlternativos:
    def test_signos_plural(self):
        assert apply_smart_commands("uno signos coma dos") == "uno, dos"

    def test_puntuacion_con_tilde_ya_no_es_prefijo(self):
        frase = "uno puntuación coma dos"
        assert apply_smart_commands(frase) == frase

    def test_puntuacion_sin_tilde_ya_no_es_prefijo(self):
        frase = "uno puntuacion coma dos"
        assert apply_smart_commands(frase) == frase

    def test_punctuation_en_ingles_ya_no_es_prefijo(self):
        frase = "one punctuation comma two"
        assert apply_smart_commands(frase) == frase


# ---------------------------------------------------------------------------
# (g) Espaciado exacto: una sola coma, un solo espacio, sin espacio antes.
# ---------------------------------------------------------------------------
class TestEspaciadoExacto:
    def test_hola_signo_coma_que_tal(self):
        assert apply_smart_commands("hola signo coma qué tal") == "hola, qué tal"


# ---------------------------------------------------------------------------
# (g-bis) Borde derecho del texto: el reemplazo NO deja un espacio colgando
# cuando el comando es lo ÚLTIMO del dictado (hallazgo del director al leer el
# código; fijado en el borde, no con un .strip() global — ver docstring de
# _replacement_for_match en core/smart_commands.py).
# ---------------------------------------------------------------------------
class TestBordeDerechoSinEspacioColgando:
    def test_punto_al_final_no_deja_espacio_colgando(self):
        assert apply_smart_commands("esto es todo signo punto") == "esto es todo."

    def test_coma_al_final_no_deja_espacio_colgando(self):
        assert apply_smart_commands("esto es todo signo coma") == "esto es todo,"

    def test_dos_puntos_al_final_no_deja_espacio_colgando(self):
        assert apply_smart_commands("revisa esto signo dos puntos") == "revisa esto:"

    def test_punto_y_coma_al_final_no_deja_espacio_colgando(self):
        assert apply_smart_commands("revisa esto signo punto y coma") == "revisa esto;"

    def test_puntos_suspensivos_al_final_no_deja_espacio_colgando(self):
        assert apply_smart_commands("y así signo puntos suspensivos") == "y así…"

    def test_espacios_extra_originales_al_final_tambien_se_recortan(self):
        """El \\s* de cierre del regex ya consume espacios sobrantes del texto
        original antes del borde; con eso, match.end() sigue tocando el final."""
        assert apply_smart_commands("esto es todo signo punto   ") == "esto es todo."

    def test_comando_no_al_final_conserva_su_espacio(self):
        """Control: el mismo comando NO al final sigue dejando su espacio de
        separación normal (el recorte es solo para el borde derecho)."""
        assert apply_smart_commands("esto es todo signo punto y algo más") == (
            "esto es todo. y algo más"
        )


# ---------------------------------------------------------------------------
# (h) Killswitch SMART_COMMANDS_ENABLED — default ON, solo "false" apaga.
# ---------------------------------------------------------------------------
class TestKillswitch:
    def test_ausente_devuelve_true(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        assert smart_commands_enabled() is True

    def test_false_devuelve_false(self, monkeypatch):
        monkeypatch.setenv("SMART_COMMANDS_ENABLED", "false")
        assert smart_commands_enabled() is False

    def test_false_case_insensitive_y_con_espacios(self, monkeypatch):
        monkeypatch.setenv("SMART_COMMANDS_ENABLED", "  FALSE  ")
        assert smart_commands_enabled() is False

    def test_true_explicito_devuelve_true(self, monkeypatch):
        monkeypatch.setenv("SMART_COMMANDS_ENABLED", "true")
        assert smart_commands_enabled() is True

    @pytest.mark.parametrize("basura", ["sí", "1", "", "no", "0", "apagado"])
    def test_valores_basura_dejan_encendido(self, monkeypatch, basura):
        """Decisión documentada en el docstring de smart_commands_enabled(): SOLO
        el valor exacto "false" apaga. Cualquier otra cosa -incluida una cadena
        vacía o un typo como "no"- deja la pasada ENCENDIDA (fail-open: es regex
        local sin red, el peor caso es quedar activa quien quiso apagarla)."""
        monkeypatch.setenv("SMART_COMMANDS_ENABLED", basura)
        assert smart_commands_enabled() is True


# ---------------------------------------------------------------------------
# (i) Presupuesto de latencia del Eje 3: 5 ms sobre ~5.000 caracteres.
# Margen amplio para no volverse intermitente en máquina cargada (mismo criterio
# que TestPresupuestoDeLatenciaEje3 en tests/test_pipeline_texto.py, que mide
# smart_commands + snippets juntos bajo un techo de 250ms / 5x el contrato).
# ---------------------------------------------------------------------------
class TestPresupuestoDeLatencia:
    def test_bajo_el_techo_sobre_5000_caracteres(self):
        fragmento = "hola signo coma qué tal signo punto y aparte una frase más. "
        repeticiones = (5000 // (len(fragmento) + 1)) + 2
        texto_largo = (fragmento + " ") * repeticiones
        assert len(texto_largo) >= 5000

        start = time.perf_counter()
        result = apply_smart_commands(texto_largo)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert result is not None
        assert elapsed_ms < 25, (
            f"apply_smart_commands tardó {elapsed_ms:.2f}ms sobre "
            f"{len(texto_largo)} caracteres — muy por encima del techo de 5ms "
            "del Eje 3 (CLAUDE.md sección 19), incluso con margen 5x."
        )


# ---------------------------------------------------------------------------
# Entrada vacía / None-ish: no revienta, devuelve lo que entró.
# ---------------------------------------------------------------------------
class TestEntradaVaciaONone:
    def test_none(self):
        assert apply_smart_commands(None) is None

    def test_cadena_vacia(self):
        assert apply_smart_commands("") == ""


# ---------------------------------------------------------------------------
# (j) Cableado en main.py::_transcribe_final (unidad 1b, Ola 1).
#
# Replica la lógica que main.py añade entre "text = text.strip()" y el bloque
# de dictation_modes, SIN instanciar VflowApp (exige QApplication/hotkeys/
# recorder reales) — mismo patrón que
# tests/test_dictation_modes.py::TestRawTextMostRaw y
# tests/test_pipeline_texto.py::TestDictadoEnsambladoLlegaCompleto.
#
# Llama a las funciones REALES de smart_commands (apply_smart_commands +
# smart_commands_enabled, que lee SMART_COMMANDS_ENABLED del entorno) y
# mockea dictation_modes, para poder verificar el ORDEN entre las dos pasadas
# sin depender de un LLM real.
# ---------------------------------------------------------------------------
def _simulate_transcribe_final_wiring(
    text, raw_full, *, translate, source,
    modes_on=False, preset=None, reformatted=None,
):
    from core import dictation_modes as _dm

    with patch.object(_dm, "modes_enabled", return_value=modes_on), \
         patch.object(_dm, "preset_for_exe", return_value=preset), \
         patch.object(_dm, "reformat_text", return_value=reformatted):

        # main.py:895-896
        if raw_full is not None and raw_full.strip() == text:
            raw_full = None

        # main.py: bloque de smart commands (unidad 1b)
        if (
            not translate
            and source != "system"
            and smart_commands.smart_commands_enabled()
        ):
            with_commands = smart_commands.apply_smart_commands(text)
            if with_commands and with_commands.strip() and with_commands != text:
                if raw_full is None:
                    raw_full = text
                text = with_commands

        # main.py: bloque de dictation_modes (unidad 6.3, ya existente)
        if not translate and source != "system" and _dm.modes_enabled():
            p = _dm.preset_for_exe("dummy.exe")
            if p:
                new_text = _dm.reformat_text(text, p)
                if new_text and new_text != text:
                    if raw_full is None:
                        raw_full = text
                    text = new_text

        return text, raw_full


class TestCableadoCaminoFeliz:
    def test_signo_coma_sale_con_la_coma_puesta(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        text, raw_full = _simulate_transcribe_final_wiring(
            "uno signo coma dos", raw_full=None, translate=False, source="mic",
        )
        assert text == "uno, dos"
        assert raw_full == "uno signo coma dos"


class TestCableadoGates:
    """Los tres gates que deciden si el bloque de smart commands corre. Son la
    mitad del valor de la unidad 1b (Eje 2 del contrato, CLAUDE.md sección
    19): sin ellos, reunión/URL heredarían la pasada por construcción si
    alguien la moviera al sitio equivocado — aquí se verifica el lado
    `main.py`, que es el que sí debe aplicarla cuando corresponde."""

    def test_translate_true_no_aplica(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        text, raw_full = _simulate_transcribe_final_wiring(
            "uno signo coma dos", raw_full=None, translate=True, source="mic",
        )
        assert text == "uno signo coma dos"
        assert raw_full is None

    def test_audio_de_sistema_no_aplica(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        text, raw_full = _simulate_transcribe_final_wiring(
            "uno signo coma dos", raw_full=None, translate=False, source="system",
        )
        assert text == "uno signo coma dos"
        assert raw_full is None

    def test_killswitch_false_no_aplica(self, monkeypatch):
        monkeypatch.setenv("SMART_COMMANDS_ENABLED", "false")
        text, raw_full = _simulate_transcribe_final_wiring(
            "uno signo coma dos", raw_full=None, translate=False, source="mic",
        )
        assert text == "uno signo coma dos"
        assert raw_full is None


class TestCableadoRawText:
    """La regla de raw_text (CLAUDE.md sección 19, Eje 1): si smart commands
    cambia el texto y no había crudo previo, el crudo pasa a ser el texto
    pre-cambio; si ya había crudo (el diccionario cambió algo antes), ese
    crudo anterior es MÁS crudo y se conserva sin pisarlo."""

    def test_sin_crudo_previo_el_crudo_pasa_a_ser_el_texto_pre_cambio(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        text, raw_full = _simulate_transcribe_final_wiring(
            "hola signo coma qué tal", raw_full=None, translate=False, source="mic",
        )
        assert text == "hola, qué tal"
        assert raw_full == "hola signo coma qué tal"

    def test_con_crudo_previo_del_diccionario_no_se_pisa(self, monkeypatch):
        """Simula que el diccionario ya cambió 'Johan'->'Johann' antes de este
        bloque (raw_full = crudo pre-diccionario). Smart commands cambia el
        texto AÚN MÁS (agrega puntuación), pero raw_full debe seguir siendo el
        crudo pre-diccionario, que es el más crudo de los dos."""
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        text, raw_full = _simulate_transcribe_final_wiring(
            "hola Johann signo coma qué tal",
            raw_full="hola Johan signo coma qué tal",
            translate=False, source="mic",
        )
        assert text == "hola Johann, qué tal"
        assert raw_full == "hola Johan signo coma qué tal"


class TestCableadoOrdenRespectoAlReformateoLLM:
    """Eje 1 del contrato: smart commands (pasada 3) corre ANTES que el
    reformateo LLM de dictation_modes (pasada 5)."""

    def test_reformat_text_recibe_el_texto_ya_puntuado_por_smart_commands(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)

        captured = {}

        def _fake_reformat(text, preset, timeout=None):
            captured["text"] = text
            return text.upper()

        from core import dictation_modes as _dm
        with patch.object(_dm, "modes_enabled", return_value=True), \
             patch.object(_dm, "preset_for_exe", return_value="email"), \
             patch.object(_dm, "reformat_text", _fake_reformat):

            text = "hola signo coma qué tal"
            if smart_commands.smart_commands_enabled():
                with_commands = smart_commands.apply_smart_commands(text)
                if with_commands and with_commands.strip() and with_commands != text:
                    text = with_commands

            if _dm.modes_enabled():
                preset = _dm.preset_for_exe("dummy.exe")
                if preset:
                    text = _dm.reformat_text(text, preset)

        assert captured["text"] == "hola, qué tal", (
            "reformat_text no recibió el texto YA puntuado por smart commands "
            "— el orden 3 antes que 5 (CLAUDE.md sección 19, Eje 1) está roto."
        )
        assert text == "HOLA, QUÉ TAL"


class TestCableadoGuardaDeSeguridad:
    """Una pasada de smart commands que devolviera vacío o solo espacios
    NUNCA reemplaza el dictado."""

    def test_pasada_vacia_no_borra_el_dictado(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        with patch.object(smart_commands, "apply_smart_commands", return_value=""):
            text, raw_full = _simulate_transcribe_final_wiring(
                "uno signo coma dos", raw_full=None, translate=False, source="mic",
            )
        assert text == "uno signo coma dos"
        assert raw_full is None

    def test_pasada_solo_espacios_no_borra_el_dictado(self, monkeypatch):
        monkeypatch.delenv("SMART_COMMANDS_ENABLED", raising=False)
        with patch.object(smart_commands, "apply_smart_commands", return_value="   "):
            text, raw_full = _simulate_transcribe_final_wiring(
                "uno signo coma dos", raw_full=None, translate=False, source="mic",
            )
        assert text == "uno signo coma dos"
        assert raw_full is None


# ---------------------------------------------------------------------------
# (k) Guardián ESTRUCTURAL sobre main.py (unidad 1b, hallazgo del coordinador
# 2026-07-31): las pruebas de (j) de arriba REPLICAN el algoritmo del bloque
# de main.py dentro del propio test — verifican que la lógica que escribimos
# es correcta, no que `main.py` la ejecute de verdad. Una mutación que borre
# o desactive el bloque de smart commands en `_transcribe_final` las deja
# TODAS en verde, porque ninguna de ellas lee `main.py`.
#
# Mismo criterio que TestAlcanceEstructural en tests/test_pipeline_texto.py:
# lee el código FUENTE de main.py (sin instanciar Qt) y afirma una propiedad
# de UBICACIÓN y ORDEN, no de comportamiento en runtime. Cubre exactamente lo
# que el contrato de CLAUDE.md sección 19 exige de la unidad 1b: que el
# cableado EXISTA dentro de `_transcribe_final`, que esté en el ORDEN correcto
# respecto al reformateo LLM (Eje 1), y que conserve sus GATES (Eje 2).
# ---------------------------------------------------------------------------
class TestCableadoExisteEnMainPy:
    @staticmethod
    def _transcribe_final_source() -> str:
        """Extrae el cuerpo del método `_transcribe_final` de main.py (desde su
        `def` hasta el siguiente método al mismo nivel de indentación), para no
        confundir una llamada DENTRO de ese método con una en otro lado del
        archivo."""
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("def _transcribe_final(")
        next_def = source.index("\n    def ", start)
        return source[start:next_def]

    def test_main_py_llama_a_apply_smart_commands_dentro_de_transcribe_final(self):
        body = self._transcribe_final_source()
        assert "smart_commands.apply_smart_commands(" in body, (
            "main.py::_transcribe_final ya NO llama a "
            "smart_commands.apply_smart_commands(...). El cableado de la unidad "
            "1b desapareció: la pasada de voz->puntuación dejó de aplicarse en "
            "el dictado real, aunque toda la suite de tests/test_smart_commands.py "
            "siga en verde (esos tests, salvo este, prueban el ALGORITMO, no que "
            "main.py lo ejecute). Ver CLAUDE.md sección 19, 'Contrato del "
            "pipeline de texto', unidad 1b."
        )

    def test_apply_smart_commands_corre_antes_que_reformat_text(self):
        body = self._transcribe_final_source()
        idx_sc = body.find("smart_commands.apply_smart_commands(")
        idx_llm = body.find("dictation_modes.reformat_text(")
        assert idx_sc != -1 and idx_llm != -1, (
            "No se encontraron ambas llamadas (smart_commands.apply_smart_commands "
            "y dictation_modes.reformat_text) dentro de main.py::_transcribe_final "
            "— no se puede verificar el orden entre las pasadas 3 y 5. Ver "
            "CLAUDE.md sección 19, Eje 1 (ORDEN)."
        )
        assert idx_sc < idx_llm, (
            "smart_commands.apply_smart_commands aparece DESPUÉS de "
            "dictation_modes.reformat_text en main.py::_transcribe_final. Eso "
            "invierte el Eje 1 del contrato (CLAUDE.md sección 19): la pasada 3 "
            "(smart commands) tiene que correr ANTES que la pasada 5 (reformateo "
            "LLM), para que el LLM reciba el texto ya puntuado por el usuario."
        )

    def test_bloque_de_smart_commands_conserva_sus_gates(self):
        body = self._transcribe_final_source()
        idx_sc = body.find("smart_commands.apply_smart_commands(")
        assert idx_sc != -1, (
            "No se encontró la llamada a smart_commands.apply_smart_commands "
            "en main.py::_transcribe_final; no se puede verificar el bloque de "
            "gates que la envuelve."
        )
        idx_if = body.rfind("if (", 0, idx_sc)
        assert idx_if != -1, (
            "No se encontró un 'if (' antes de "
            "smart_commands.apply_smart_commands(...) en main.py. Sin un bloque "
            "de gates explícito envolviendo la llamada, no hay forma de "
            "confirmar que translate/source siguen protegiendo la pasada. Ver "
            "CLAUDE.md sección 19, Eje 2 (ALCANCE)."
        )
        gate_block = body[idx_if:idx_sc]
        for gate in ("translate", 'source != "system"'):
            assert gate in gate_block, (
                f"El bloque de gates que envuelve la llamada a "
                f"smart_commands.apply_smart_commands en main.py ya no "
                f"menciona {gate!r}. Sin ese gate, la pasada de voz->puntuación "
                "se escaparía a modo traducción (translate=True) y/o a dictado "
                "de audio del sistema (AUDIO_SOURCE=system, donde quien "
                "'dicta' no es necesariamente el usuario), metiendo puntuación "
                "inventada en habla que no es la del usuario dictando en su "
                "propio idioma para su propia ventana. Ver CLAUDE.md sección "
                "19, Eje 2 (ALCANCE) — el mismo daño que TestAlcanceEstructural "
                "en tests/test_pipeline_texto.py vigila del lado de "
                "core/transcriber.py."
            )
