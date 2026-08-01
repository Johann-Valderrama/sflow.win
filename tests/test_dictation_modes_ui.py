"""Tests guardián de la unidad `2c` (Ola 2 de docs/PLAN-DICTADO-2026-07-31.md).

`2c` no agrega lógica nueva de backend: el motor (5 presets, elección manual,
GET/POST /api/settings) ya lo cablearon `2a`/`2b`. Lo que agrega es DESCUBRIBILIDAD
sin tocar el default — así que lo que hay que vigilar es el HTML/JS servido, no
un endpoint nuevo. Cubre lo que un test de endpoint no vería:

  a. El panel de Ajustes explica los 5 presets con su línea de "cuándo se usa
     este y no el de al lado" (la tabla de la Ola 2), no solo sus nombres.
  b. El aviso de que el texto dictado sale a un modelo de lenguaje está en el
     documento servido, en texto plano (no solo en un comentario del .html
     fuente que Jinja podría no renderizar).
  c. La lista de apps que se reformatearían automáticamente (DEFAULT_MODE_MAP)
     tiene un contenedor dedicado para pintarse ANTES de que el usuario
     encienda el interruptor.
  d. El checkbox de activación NO trae `checked` horneado en el markup: el
     default vive en `settings.dictation_modes_enabled === true` (comparación
     estricta, JS), no en el HTML — si alguien lo hornea a mano, DICTATION_MODES_ENABLED
     pasaría a nacer ON en el navegador aunque el backend siga en "false".
"""

import pytest


@pytest.fixture()
def client():
    from web import server as web_server
    web_server.app.config["TESTING"] = True
    with web_server.app.test_client() as c:
        yield c


class TestPresetsExplicadosNoSoloNombrados:
    """Las 5 líneas de la tabla de la Ola 2 (el filtro de admisión de cada
    preset) tienen que estar en el documento servido, verbatim: si el usuario
    no puede elegir sin leer documentación, la unidad no cumplió."""

    _LINEAS = [
        "prosa formal de correo, y respeta el saludo o el cierre si los dictaste",
        "mensaje casual, se permite minúscula inicial y omitir el punto final",
        "deja los términos técnicos e identificadores literales, sin embellecer",
        "convierte lo dictado en viñetas, una por ítem, sin prosa alrededor",
        "prosa limpia y neutra: arregla la puntuación y nada más, sin "
        "formalidad de correo ni relajación de chat",
    ]

    def test_las_cinco_lineas_estan_en_el_dashboard(self, client):
        html = client.get("/").get_data(as_text=True)
        for linea in self._LINEAS:
            assert linea in html, f"falta la línea de justificación: {linea!r}"

    def test_los_cinco_nombres_de_preset_estan(self, client):
        html = client.get("/").get_data(as_text=True)
        for nombre in ("Email", "Chat", "Código", "Lista", "Notas"):
            assert f">{nombre}<" in html, f"falta el nombre de preset {nombre!r}"


class TestAvisoDeModeloDeLenguaje:
    def test_avisa_que_el_texto_sale_a_un_llm(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "modelo de lenguaje" in html
        assert "LLM" in html

    def test_avisa_que_por_defecto_es_nube_no_el_equipo(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "en la nube" in html
        assert "no en tu equipo" in html

    def test_nota_dinamica_de_backend_tiene_contenedor(self, client):
        """El párrafo que dice A QUÉ backend concreto se manda el texto
        (Groq/OpenRouter/local/...) lo rellena JS en runtime a partir del
        mismo select que gobierna el backend batch de reuniones — aquí solo
        se vigila que el contenedor y la función existan en el documento."""
        html = client.get("/").get_data(as_text=True)
        assert 'id="cfg-dictation-modes-cloud-note"' in html
        assert "function updateDictationModesCloudNote()" in html


class TestAppsAutoReformateadasVisiblesAntesDeActivar:
    def test_contenedor_de_preview_existe(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'id="cfg-dictation-apps-preview"' in html

    def test_preview_se_pinta_al_cargar_ajustes_no_solo_al_editar(self, client):
        """Si `renderDictationAppsPreview()` solo se llamara desde el
        `oninput` del textarea, la lista de apps quedaría vacía la primera
        vez que el usuario abre Ajustes (el caso que este plan exige cubrir:
        verla ANTES de tocar nada)."""
        html = client.get("/").get_data(as_text=True)
        assert (
            "document.getElementById('cfg-dictation-mode-map').value = "
            "settings.dictation_mode_map || '';\n            "
            "renderDictationAppsPreview();"
        ) in html

    def test_mencion_de_seleccion_manual_por_bandeja(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "Próximo dictado" in html


class TestDefaultSigueApagado:
    """`DICTATION_MODES_ENABLED` se queda en `false` (objeción A1 del debate
    adversarial): esta unidad es descubribilidad, no un cambio de default."""

    def test_checkbox_no_trae_checked_horneado_en_el_markup(self, client):
        html = client.get("/").get_data(as_text=True)
        assert (
            '<input type="checkbox" id="cfg-dictation-modes-enabled">' in html
        ), "el checkbox no debe traer 'checked' fijo en el HTML fuente"

    def test_js_usa_comparacion_estricta_contra_true(self, client):
        """Mismo patrón defensivo que ya usan `webhook_enabled` / `insights_fallback`
        en este archivo: `=== true` (nunca truthy a secas), así que un campo
        ausente o `undefined` en la respuesta de /api/settings cae del lado
        apagado, no del encendido."""
        html = client.get("/").get_data(as_text=True)
        assert (
            "document.getElementById('cfg-dictation-modes-enabled').checked = "
            "settings.dictation_modes_enabled === true;"
        ) in html
