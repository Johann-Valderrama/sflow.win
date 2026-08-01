"""Tests para la unidad 4b (Ola 4 de PLAN-DICTADO-2026-07-31): matcher de
snippets (`core/snippets_matcher.py`) + su cableado en `main.py`.

Cubre:
(a) expansión funcional básica (disparador -> cuerpo guardado)
(b) fronteras de palabra: un disparador de una palabra NO dispara dentro de
    una palabra más larga
(c) disparador que es prefijo de otro: gana el MÁS LARGO, de forma
    determinista (no depende del orden de inserción/lectura de la DB)
(d) normalización compartida con db.database.normalize_trigger: mayúsculas y
    acentos no importan de ningún lado
(e) entrada vacía/None-ish
(f) tabla sin snippets activos: apagado natural, la DB ni se toca
(g) hit_count incrementado en background (fire-and-forget), sin bloquear
(h) presupuesto de latencia del Eje 3 (15ms sobre ~5.000 caracteres)
(i) cableado en main.py::_transcribe_final (unidad 4b): camino feliz, los
    gates (traducción / audio de sistema), la regla de raw_text y la guarda
    de seguridad contra una expansión vacía
(j) guardián ESTRUCTURAL sobre el fuente de main.py: que la llamada exista
    DENTRO de _transcribe_final, en el orden correcto (después de smart
    commands, antes del reformateo LLM) y que conserve sus gates — mismo
    criterio que TestCableadoExisteEnMainPy en tests/test_smart_commands.py
    y TestAlcanceEstructural en tests/test_pipeline_texto.py.

El orden 3-antes-de-4 (smart commands antes que snippets) ya está cubierto en
tests/test_pipeline_texto.py::TestOrden3AntesDe4 (Capa B); no se duplica aquí.

Aislamiento: cada test usa una DB SQLite bajo tmp_path, nunca la real de dev
(mismo patrón que tests/test_snippets.py). La caché en memoria del módulo
(`core.snippets_matcher._cache`/`_db`) se resetea antes y después de CADA test
(mismo patrón que `_reset_dictionary_cache` en tests/test_pipeline_texto.py):
sin esto, un snippet instalado por un test contaminaría los demás, incluidos
los de otros archivos de test que importen este módulo en el mismo proceso.
"""
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import core.snippets_matcher as snippets_matcher
from core.snippets_matcher import expand_snippets

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Aislamiento de la caché global del módulo (mismo patrón que
# tests/test_pipeline_texto.py::_reset_dictionary_cache).
# ---------------------------------------------------------------------------
def _reset_snippets_cache():
    snippets_matcher._db = None
    snippets_matcher._cache = snippets_matcher._EMPTY_CACHE


@pytest.fixture(autouse=True)
def _aislar_cache_snippets():
    _reset_snippets_cache()
    yield
    _reset_snippets_cache()


@pytest.fixture
def db(tmp_path):
    from db.database import TranscriptionDB
    import config

    db_path = str(tmp_path / "test_snippets_matcher.db")
    # Verificación de aislamiento: nunca la base real de producción.
    assert db_path != config.DB_PATH
    return TranscriptionDB(db_path=db_path)


def _install_snippets(monkeypatch, db_instance, triggers_and_bodies):
    """Inserta snippets en `db_instance`, apunta la caché del módulo a esa DB
    (en vez de la real) e invalida para reconstruir contra ella. Devuelve los
    ids insertados, en el mismo orden que `triggers_and_bodies`."""
    ids = []
    for trigger, body in triggers_and_bodies:
        ids.append(db_instance.add_snippet(trigger, body))
    monkeypatch.setattr(snippets_matcher, "_db", db_instance)
    snippets_matcher.invalidate()
    return ids


# ===========================================================================
# (a) Expansión funcional básica
# ===========================================================================
class TestExpansionBasica:
    def test_disparador_en_medio_del_texto_se_expande(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        assert expand_snippets("hasta pronto firma correo, gracias") == (
            "hasta pronto Saludos,\nJohann, gracias"
        )

    def test_disparador_al_inicio_y_al_final(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("saludo", "Hola equipo")])
        assert expand_snippets("saludo a todos") == "Hola equipo a todos"
        assert expand_snippets("les escribo con este saludo") == (
            "les escribo con este Hola equipo"
        )

    def test_varios_disparadores_distintos_en_el_mismo_texto(self, monkeypatch, db):
        _install_snippets(
            monkeypatch, db,
            [("firma", "Johann V."), ("cierre", "Quedo atento.")],
        )
        assert expand_snippets("un saludo firma y de una vez cierre") == (
            "un saludo Johann V. y de una vez Quedo atento."
        )

    def test_disparador_repetido_se_expande_cada_vez(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann")])
        assert expand_snippets("firma y otra vez firma") == "Johann y otra vez Johann"

    def test_sin_disparador_en_el_texto_lo_devuelve_intacto(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        texto = "esto no menciona ningún disparador"
        assert expand_snippets(texto) == texto


# ===========================================================================
# (b) Fronteras de palabra
# ===========================================================================
class TestFronteraDePalabra:
    def test_no_dispara_dentro_de_una_palabra_mas_larga(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        texto = "firmamos el contrato ayer"
        assert expand_snippets(texto) == texto, (
            "'firma' se activó DENTRO de 'firmamos' — viola la coincidencia de "
            "frase completa con fronteras de palabra."
        )

    def test_si_dispara_como_palabra_suelta(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        assert expand_snippets("mi firma es esta") == "mi Johann V. es esta"

    def test_no_dispara_como_sufijo_de_otra_palabra(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("correo", "email@ejemplo.com")])
        texto = "recibiste el anticorreo de spam"
        assert expand_snippets(texto) == texto


# ===========================================================================
# (c) Disparador que es prefijo de otro: gana el MÁS LARGO, determinista
# ===========================================================================
class TestPrefijoDeOtroGanaElMasLargo:
    def test_frase_larga_completa_dispara_la_larga(self, monkeypatch, db):
        _install_snippets(
            monkeypatch, db,
            [("firma", "CORTA"), ("firma larga", "LARGA")],
        )
        assert expand_snippets("aquí va mi firma larga de cierre") == (
            "aquí va mi LARGA de cierre"
        )

    def test_solo_la_palabra_corta_dispara_la_corta(self, monkeypatch, db):
        _install_snippets(
            monkeypatch, db,
            [("firma", "CORTA"), ("firma larga", "LARGA")],
        )
        assert expand_snippets("aquí va mi firma de cierre") == "aquí va mi CORTA de cierre"

    def test_determinismo_no_depende_del_orden_de_insercion(self, monkeypatch, db):
        """Mismo caso que arriba, pero insertando el disparador CORTO primero
        y el LARGO después (orden invertido). El resultado tiene que ser
        idéntico: la caché ordena sus candidatos por (palabras descendente,
        trigger_key ascendente) al construirse, nunca por el orden de
        llegada de list_snippets."""
        _install_snippets(
            monkeypatch, db,
            [("firma larga", "LARGA"), ("firma", "CORTA")],
        )
        assert expand_snippets("aquí va mi firma larga de cierre") == (
            "aquí va mi LARGA de cierre"
        )
        assert expand_snippets("aquí va mi firma de cierre") == "aquí va mi CORTA de cierre"


# ===========================================================================
# (d) Normalización compartida con db.database.normalize_trigger
# ===========================================================================
class TestNormalizacionMayusculasYAcentos:
    def test_dictado_sin_acento_dispara_disparador_guardado_con_acento(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("Petición", "SOLICITUD_URGENTE")])
        assert expand_snippets("envío mi peticion formal") == (
            "envío mi SOLICITUD_URGENTE formal"
        )

    def test_dictado_en_mayusculas_dispara_disparador_guardado_en_minusculas(
        self, monkeypatch, db
    ):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        assert expand_snippets("hasta pronto FIRMA CORREO") == (
            "hasta pronto Saludos,\nJohann"
        )

    def test_mayusculas_y_acentos_combinados(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("petición", "SOLICITUD_URGENTE")])
        assert expand_snippets("envío mi PETICIÓN formal") == (
            "envío mi SOLICITUD_URGENTE formal"
        )


# ===========================================================================
# (e) Entrada vacía / None-ish
# ===========================================================================
class TestEntradaVaciaONone:
    def test_none(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann")])
        assert expand_snippets(None) is None

    def test_cadena_vacia(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann")])
        assert expand_snippets("") == ""


# ===========================================================================
# (f) Tabla sin snippets activos: apagado natural — la DB ni se toca
# ===========================================================================
class TestTablaVaciaEsApagadoNatural:
    def test_sin_snippets_devuelve_el_texto_intacto_sin_tocar_la_db(self, monkeypatch):
        poison = MagicMock()
        poison.list_snippets.side_effect = AssertionError(
            "expand_snippets no debería tocar la DB cuando no hay snippets activos"
        )
        monkeypatch.setattr(snippets_matcher, "_db", poison)
        texto = "hola mundo, cualquier cosa"
        assert expand_snippets(texto) == texto
        poison.list_snippets.assert_not_called()

    def test_todos_los_snippets_deshabilitados_tambien_es_apagado(self, monkeypatch, db):
        sid = db.add_snippet("firma", "Johann")
        db.set_snippet_enabled(sid, False)
        monkeypatch.setattr(snippets_matcher, "_db", db)
        snippets_matcher.invalidate()
        texto = "hola firma"
        assert expand_snippets(texto) == texto


# ===========================================================================
# (g) hit_count en background, fire-and-forget
# ===========================================================================
class _SyncThread:
    """Doble de threading.Thread que corre el target SÍNCRONO en .start(), para
    poder verificar el efecto del hilo daemon sin sleep/polling."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


class TestHitCountEnBackground:
    def test_un_match_incrementa_hit_count_en_uno(self, monkeypatch, db):
        with patch.object(snippets_matcher.threading, "Thread", _SyncThread):
            ids = _install_snippets(monkeypatch, db, [("firma", "Johann")])
            expand_snippets("hola firma")
        row = db.get_snippet(ids[0])
        assert row["hit_count"] == 1

    def test_disparador_repetido_incrementa_una_vez_por_ocurrencia(self, monkeypatch, db):
        with patch.object(snippets_matcher.threading, "Thread", _SyncThread):
            ids = _install_snippets(monkeypatch, db, [("firma", "Johann")])
            expand_snippets("firma y otra vez firma y otra vez firma")
        row = db.get_snippet(ids[0])
        assert row["hit_count"] == 3

    def test_sin_match_no_incrementa_nada(self, monkeypatch, db):
        with patch.object(snippets_matcher.threading, "Thread", _SyncThread):
            ids = _install_snippets(monkeypatch, db, [("firma", "Johann")])
            expand_snippets("texto sin ningún disparador")
        row = db.get_snippet(ids[0])
        assert row["hit_count"] == 0


# ===========================================================================
# (h) Presupuesto de latencia del Eje 3: 15ms sobre ~5.000 caracteres.
# Margen 5x (75ms) para no volverse intermitente en máquina cargada — mismo
# criterio que TestPresupuestoDeLatencia en tests/test_smart_commands.py y
# TestPresupuestoDeLatenciaEje3 en tests/test_pipeline_texto.py.
# ===========================================================================
class TestPresupuestoDeLatencia:
    def test_bajo_el_techo_sobre_5000_caracteres_con_snippets_activos(self, monkeypatch, db):
        # 20 disparadores activos (caso realista de una lista personal), para
        # medir el costo REAL del lookup por token, no el caso trivial de
        # caché vacía (ese ya lo mide TestPresupuestoDeLatenciaEje3 en
        # tests/test_pipeline_texto.py, sin ningún snippet configurado).
        triggers = [(f"disparador numero {i}", f"cuerpo del snippet {i}") for i in range(20)]
        _install_snippets(monkeypatch, db, triggers)

        fragmento = (
            "hola disparador numero 3 qué tal una frase más sin disparadores "
            "de por medio, solo palabras normales del dictado diario. "
        )
        repeticiones = (5000 // (len(fragmento) + 1)) + 2
        texto_largo = fragmento * repeticiones
        assert len(texto_largo) >= 5000

        start = time.perf_counter()
        result = expand_snippets(texto_largo)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert result is not None
        assert elapsed_ms < 75, (
            f"expand_snippets tardó {elapsed_ms:.2f}ms sobre {len(texto_largo)} "
            "caracteres — muy por encima del techo de 15ms del Eje 3 (CLAUDE.md "
            "sección 19), incluso con margen 5x."
        )


# ===========================================================================
# (i) Cableado en main.py::_transcribe_final (unidad 4b).
#
# Replica la lógica que main.py añade entre el bloque de smart commands y el
# de dictation_modes, SIN instanciar VflowApp — mismo patrón que
# tests/test_smart_commands.py::_simulate_transcribe_final_wiring.
# ===========================================================================
def _simulate_snippets_wiring(text, raw_full, *, translate, source):
    if raw_full is not None and raw_full.strip() == text:
        raw_full = None

    # main.py: bloque de snippets (unidad 4b)
    if not translate and source != "system":
        expanded = snippets_matcher.expand_snippets(text)
        if expanded and expanded.strip() and expanded != text:
            if raw_full is None:
                raw_full = text
            text = expanded

    return text, raw_full


class TestCableadoCaminoFeliz:
    def test_disparador_sale_expandido(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        text, raw_full = _simulate_snippets_wiring(
            "hasta pronto firma correo", raw_full=None, translate=False, source="mic",
        )
        assert text == "hasta pronto Saludos,\nJohann"
        assert raw_full == "hasta pronto firma correo"


class TestCableadoGates:
    """Mismos dos gates que smart commands (Eje 2 del contrato, CLAUDE.md
    sección 19): sin ellos, un disparador dicho por OTRA persona en una
    reunión, o el audio del sistema, se expandiría a la firma/plantilla del
    usuario dentro de un acta que no es suya."""

    def test_translate_true_no_aplica(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        text, raw_full = _simulate_snippets_wiring(
            "hasta pronto firma correo", raw_full=None, translate=True, source="mic",
        )
        assert text == "hasta pronto firma correo"
        assert raw_full is None

    def test_audio_de_sistema_no_aplica(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma correo", "Saludos,\nJohann")])
        text, raw_full = _simulate_snippets_wiring(
            "hasta pronto firma correo", raw_full=None, translate=False, source="system",
        )
        assert text == "hasta pronto firma correo"
        assert raw_full is None


class TestCableadoRawText:
    """Regla de raw_text (CLAUDE.md sección 19, Eje 1): si snippets cambia el
    texto y no había crudo previo, el crudo pasa a ser el texto pre-cambio;
    si ya había crudo (smart commands/diccionario cambiaron algo antes), ese
    crudo anterior es MÁS crudo y se conserva sin pisarlo."""

    def test_sin_crudo_previo_el_crudo_pasa_a_ser_el_texto_pre_cambio(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        text, raw_full = _simulate_snippets_wiring(
            "hola firma", raw_full=None, translate=False, source="mic",
        )
        assert text == "hola Johann V."
        assert raw_full == "hola firma"

    def test_con_crudo_previo_de_smart_commands_no_se_pisa(self, monkeypatch, db):
        """Simula que smart commands ya cambió el texto antes de este bloque
        (raw_full = crudo pre-smart-commands). Snippets cambia el texto AÚN
        MÁS (expande el disparador), pero raw_full debe seguir siendo el
        crudo más antiguo de los dos."""
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        text, raw_full = _simulate_snippets_wiring(
            "hola, firma",
            raw_full="hola signo coma firma",
            translate=False, source="mic",
        )
        assert text == "hola, Johann V."
        assert raw_full == "hola signo coma firma"


class TestCableadoGuardaDeSeguridad:
    """Una expansión que devolviera vacío o solo espacios NUNCA reemplaza el
    dictado — un snippet mal configurado no puede borrar lo que el usuario
    dijo."""

    def test_pasada_vacia_no_borra_el_dictado(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        with patch.object(snippets_matcher, "expand_snippets", return_value=""):
            text, raw_full = _simulate_snippets_wiring(
                "hola firma", raw_full=None, translate=False, source="mic",
            )
        assert text == "hola firma"
        assert raw_full is None

    def test_pasada_solo_espacios_no_borra_el_dictado(self, monkeypatch, db):
        _install_snippets(monkeypatch, db, [("firma", "Johann V.")])
        with patch.object(snippets_matcher, "expand_snippets", return_value="   "):
            text, raw_full = _simulate_snippets_wiring(
                "hola firma", raw_full=None, translate=False, source="mic",
            )
        assert text == "hola firma"
        assert raw_full is None


# ---------------------------------------------------------------------------
# (j) Guardián ESTRUCTURAL sobre main.py (hallazgo del coordinador
# 2026-07-31, mismo motivo que TestCableadoExisteEnMainPy en
# tests/test_smart_commands.py): las pruebas de (i) de arriba REPLICAN el
# algoritmo del bloque de main.py dentro del propio test — verifican que la
# lógica que escribimos es correcta, no que main.py la ejecute de verdad. Una
# mutación que borre o desactive el bloque de snippets en _transcribe_final
# las deja TODAS en verde, porque ninguna de ellas lee main.py.
# ---------------------------------------------------------------------------
class TestCableadoExisteEnMainPy:
    @staticmethod
    def _transcribe_final_source() -> str:
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("def _transcribe_final(")
        next_def = source.index("\n    def ", start)
        return source[start:next_def]

    def test_main_py_llama_a_expand_snippets_dentro_de_transcribe_final(self):
        body = self._transcribe_final_source()
        assert "snippets_matcher.expand_snippets(" in body, (
            "main.py::_transcribe_final ya NO llama a "
            "snippets_matcher.expand_snippets(...). El cableado de la unidad "
            "4b desapareció: la expansión de disparadores dejó de aplicarse "
            "en el dictado real, aunque toda la suite de "
            "tests/test_snippets_matcher.py siga en verde (esos tests, salvo "
            "este, prueban el ALGORITMO, no que main.py lo ejecute). Ver "
            "CLAUDE.md sección 19, 'Contrato del pipeline de texto', unidad 4b."
        )

    def test_expand_snippets_corre_despues_de_smart_commands(self):
        body = self._transcribe_final_source()
        idx_snip = body.find("snippets_matcher.expand_snippets(")
        idx_sc = body.find("smart_commands.apply_smart_commands(")
        assert idx_sc != -1 and idx_snip != -1, (
            "No se encontraron ambas llamadas (smart_commands.apply_smart_commands "
            "y snippets_matcher.expand_snippets) dentro de "
            "main.py::_transcribe_final — no se puede verificar el orden entre "
            "las pasadas 3 y 4. Ver CLAUDE.md sección 19, Eje 1 (ORDEN)."
        )
        assert idx_sc < idx_snip, (
            "snippets_matcher.expand_snippets aparece ANTES de "
            "smart_commands.apply_smart_commands en main.py::_transcribe_final. "
            "Eso invierte el Eje 1 del contrato (CLAUDE.md sección 19): la pasada "
            "3 (smart commands) tiene que correr ANTES que la pasada 4 "
            "(snippets), o esta pasada volvería a escanear texto que el usuario "
            "ya escribió dentro de un snippet y lo mutilaría."
        )

    def test_expand_snippets_corre_antes_que_reformat_text(self):
        body = self._transcribe_final_source()
        idx_snip = body.find("snippets_matcher.expand_snippets(")
        idx_llm = body.find("dictation_modes.reformat_text(")
        assert idx_snip != -1 and idx_llm != -1, (
            "No se encontraron ambas llamadas (snippets_matcher.expand_snippets "
            "y dictation_modes.reformat_text) dentro de "
            "main.py::_transcribe_final — no se puede verificar el orden entre "
            "las pasadas 4 y 5. Ver CLAUDE.md sección 19, Eje 1 (ORDEN)."
        )
        assert idx_snip < idx_llm, (
            "snippets_matcher.expand_snippets aparece DESPUÉS de "
            "dictation_modes.reformat_text en main.py::_transcribe_final. Eso "
            "invierte el Eje 1 del contrato (CLAUDE.md sección 19): la pasada 4 "
            "(snippets) tiene que correr ANTES que la pasada 5 (reformateo LLM), "
            "para que el LLM reciba el texto ya expandido."
        )

    def test_bloque_de_snippets_conserva_sus_gates(self):
        body = self._transcribe_final_source()
        idx_snip = body.find("snippets_matcher.expand_snippets(")
        assert idx_snip != -1, (
            "No se encontró la llamada a snippets_matcher.expand_snippets en "
            "main.py::_transcribe_final; no se puede verificar el bloque de "
            "gates que la envuelve."
        )
        idx_if = body.rfind("\n                if ", 0, idx_snip)
        assert idx_if != -1, (
            "No se encontró un 'if' de 16 espacios de indentación antes de "
            "snippets_matcher.expand_snippets(...) en main.py. Sin un bloque de "
            "gates explícito envolviendo la llamada, no hay forma de confirmar "
            "que translate/source siguen protegiendo la pasada. Ver CLAUDE.md "
            "sección 19, Eje 2 (ALCANCE)."
        )
        gate_block = body[idx_if:idx_snip]
        for gate in ("translate", 'source != "system"'):
            assert gate in gate_block, (
                f"El bloque de gates que envuelve la llamada a "
                f"snippets_matcher.expand_snippets en main.py ya no menciona "
                f"{gate!r}. Sin ese gate, la expansión de disparadores se "
                "escaparía a modo traducción (translate=True) y/o a dictado de "
                "audio del sistema (AUDIO_SOURCE=system, donde quien 'dicta' no "
                "es necesariamente el usuario), expandiendo la firma/plantilla "
                "del usuario dentro de habla que no es suya. Ver CLAUDE.md "
                "sección 19, Eje 2 (ALCANCE) — el mismo daño que "
                "TestAlcanceEstructural en tests/test_pipeline_texto.py vigila "
                "del lado de core/transcriber.py."
            )
