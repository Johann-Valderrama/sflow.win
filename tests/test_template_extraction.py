"""Tests guardián de la unidad 4.1 (extracción de los templates inline a Jinja2).

Los dos documentos HTML pasaron de constantes Python en web/server.py
(HTML_TEMPLATE / MEETING_PAGE vía render_template_string) a archivos Jinja2
en web/templates/ (dashboard.html / reunion.html vía render_template). El
volcado fue del VALOR runtime de las strings (no del texto fuente: los escapes
``\\uXXXX`` del .py ya colapsados a caracteres reales), y las 3 inyecciones por
``.replace()`` pasaron a variables de contexto Jinja (``mt_js``,
``template_options_html``, ``chips_json``, todas con ``|safe``).

Regresiones que vigila este archivo:
  a. Tags Jinja sin resolver en el HTML servido ('{{', '{%', '{#'): una
     variable olvidada o un template roto se serviría literal al navegador.
  b. Template literals JS con ``${...}``: Jinja NO los procesa (no son su
     sintaxis), pero un volcado/escapado descuidado podría mutilarlos.
  c. Acentos reales: el template extraído vive en disco como UTF-8; un
     encoding equivocado al leerlo rompería todos los textos en español.
  d. Las inyecciones request-time de /reunion (options del dropdown de
     plantillas + chips JSON por plantilla) siguen llegando renderizadas.
"""

import pytest


@pytest.fixture()
def client():
    from web import server as web_server
    web_server.app.config["TESTING"] = True
    with web_server.app.test_client() as c:
        yield c


JINJA_TAGS = ("{{", "{%", "{#")


class TestNoUnresolvedJinja:
    def test_dashboard_has_no_jinja_tags(self, client):
        html = client.get("/").get_data(as_text=True)
        for tag in JINJA_TAGS:
            assert tag not in html, (
                f"'/' servido contiene '{tag}': variable Jinja sin resolver "
                "o template roto (unidad 4.1)"
            )

    def test_reunion_has_no_jinja_tags(self, client):
        html = client.get("/reunion").get_data(as_text=True)
        for tag in JINJA_TAGS:
            assert tag not in html, (
                f"'/reunion' servido contiene '{tag}': variable Jinja sin "
                "resolver o template roto (unidad 4.1)"
            )


class TestJsSnippetsIntact:
    def test_dashboard_preserves_template_literal(self, client):
        """Snippet REAL del dashboard con ${...} (template literal JS):
        Jinja no debe tocarlo ni el volcado haberlo mutilado."""
        html = client.get("/").get_data(as_text=True)
        assert 'tr[data-id="${id}"]' in html
        assert "${escapeHtml(t.text)}" in html

    def test_reunion_preserves_brace_heavy_js(self, client):
        """reunion.html no usa template literals ${...} (estilo concatenación),
        así que el guard equivalente son snippets REALES cargados de llaves
        simples (objeto JS + keyframe CSS) que un procesado erróneo rompería."""
        html = client.get("/reunion").get_data(as_text=True)
        assert "cur = { gen: null, since: 0, segments: [] }" in html
        assert "@keyframes mtFade { from{opacity:0;transform:translateY(3px);} to{opacity:1;transform:none;} }" in html


class TestEncodingIntact:
    def test_dashboard_has_real_accents(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "ó" in html

    def test_reunion_has_real_accents(self, client):
        html = client.get("/reunion").get_data(as_text=True)
        assert "ó" in html
        assert "Reunión" in html


class TestReunionRequestTimeInjection:
    def test_chips_json_injected_in_assignment(self, client):
        """La línea REAL de asignación (no el comentario que la precede) debe
        contener el JSON renderizado desde core.meeting_templates.chips_map()."""
        html = client.get("/reunion").get_data(as_text=True)
        assert "const MEETING_TEMPLATE_CHIPS = {" in html, (
            "la asignación de MEETING_TEMPLATE_CHIPS no recibió el JSON de chips"
        )
        assert "__MEETING_TEMPLATES_CHIPS_JSON__" not in html
        # Una key real del chips_map, dentro del documento servido:
        assert '"general"' in html

    def test_dropdown_options_injected(self, client):
        from core import meeting_templates as _mt
        html = client.get("/reunion").get_data(as_text=True)
        assert "__MEETING_TEMPLATE_OPTIONS_HTML__" not in html
        for name, tpl in _mt.TEMPLATES.items():
            assert f'<option value="{name}">{tpl["label"]}</option>' in html, (
                f"falta la option del dropdown para la plantilla '{name}'"
            )
