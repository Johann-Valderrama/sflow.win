"""Blueprint de páginas HTML: dashboard (/), reunión (/reunion) y el logo (/logo)."""

import json as _json

from flask import Blueprint, render_template, send_file

from core import meeting_templates as _meeting_templates
from web.state import _MT_INCREMENTAL_JS

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    """Sirve la página principal del dashboard de transcripciones."""
    return render_template("dashboard.html", mt_js=_MT_INCREMENTAL_JS)


def _meeting_page_html() -> str:
    """Renderiza web/templates/reunion.html con las variables de plantillas (unidad 4.3)
    generadas desde ``core.meeting_templates.TEMPLATES`` — una sola fuente de verdad para
    los textos de las 4 plantillas, sin duplicarlos a mano en el JS."""
    options_html = "\n        ".join(
        f'<option value="{name}">{tpl["label"]}</option>'
        for name, tpl in _meeting_templates.TEMPLATES.items()
    )
    chips_json = _json.dumps(_meeting_templates.chips_map(), ensure_ascii=False)
    return render_template(
        "reunion.html",
        mt_js=_MT_INCREMENTAL_JS,
        template_options_html=options_html,
        chips_json=chips_json,
    )


@bp.route("/reunion")
def reunion():
    """Ventana dedicada al modo reunión: en vivo (transcript + análisis + acta) + historial."""
    return _meeting_page_html()


@bp.route("/logo")
def logo():
    """Sirve el logo de la app para el dashboard."""
    from config import LOGO_PATH
    return send_file(LOGO_PATH, mimetype="image/png")
