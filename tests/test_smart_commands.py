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
"""
import os
import time

import pytest

from core.smart_commands import apply_smart_commands, smart_commands_enabled


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
