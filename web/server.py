"""App factory + shim de re-exportación (unidad 4.2).

``web/server.py`` dejó de contener rutas: cada feature vive en su propio
blueprint bajo ``web/blueprints/`` y el estado compartido (DB única, singletons,
helpers transversales de CSRF/settings) vive en ``web/state.py``. Este módulo
solo ensambla la app Flask (``create_app()``), registra el hook de CSRF una
única vez a nivel de aplicación, registra los 8 blueprints, y RE-EXPORTA los
nombres que la suite de tests importa directamente de ``web.server`` (compat
hacia atrás; ver imports abajo).
"""

import socket
import threading

from flask import Flask

from config import WEB_STATIC_DIR, WEB_TEMPLATES_DIR
from core import assistant as _assistant  # noqa: F401 — re-exportado (monkeypatch en tests)

from web.blueprints import dictionary as _bp_dictionary
from web.blueprints import media as _bp_media
from web.blueprints import meeting as _bp_meeting
from web.blueprints import meetings as _bp_meetings
from web.blueprints import pages as _bp_pages
from web.blueprints import settings as _bp_settings
from web.blueprints import transcriptions as _bp_transcriptions
from web.blueprints import url_queue as _bp_url_queue
from web.blueprints.url_queue import _process_next_url_item  # noqa: F401 — re-exportado (test)
from web.state import (  # noqa: F401 — MEETING/PROACTIVE/_db/_validate_* re-exportados (tests)
    MEETING,
    PROACTIVE,
    _auth_check,
    _csrf_check,
    _db,
    _validate_briefing_path,
    _validate_export_dir,
)


def create_app() -> Flask:
    """Ensambla la app Flask: config, hook de CSRF único, y los 8 blueprints por feature."""
    import secrets as _secrets

    flask_app = Flask(
        __name__,
        static_folder=WEB_STATIC_DIR,
        static_url_path="/static",
        template_folder=WEB_TEMPLATES_DIR,
    )
    flask_app.config["JSON_AS_ASCII"] = False
    flask_app.config["SECRET_KEY"] = _secrets.token_hex(32)

    # CSRF: UN solo hook global a nivel de app (nunca @bp.before_request, eso
    # dejaría blueprints sin CSRF — decisión del debate de la unidad 4.2).
    flask_app.before_request(_csrf_check)

    # Auth local por token: mismo criterio (hook único a nivel de app, nunca
    # @bp.before_request). Va DESPUÉS del CSRF: los hooks corren en orden de
    # registro, así que un origen cruzado se rechaza antes de mirar el token.
    flask_app.before_request(_auth_check)

    flask_app.register_blueprint(_bp_pages.bp)
    flask_app.register_blueprint(_bp_transcriptions.bp)
    flask_app.register_blueprint(_bp_settings.bp)
    flask_app.register_blueprint(_bp_dictionary.bp)
    flask_app.register_blueprint(_bp_url_queue.bp)
    flask_app.register_blueprint(_bp_meeting.bp)
    flask_app.register_blueprint(_bp_meetings.bp)
    flask_app.register_blueprint(_bp_media.bp)

    return flask_app


app = create_app()


def _find_free_port(start: int = 5678, attempts: int = 50) -> int:
    """Find an available port starting from `start`."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + attempts - 1}")


def start_web_server(port: int = None) -> int:
    """Start Flask in a daemon thread so it doesn't block the Qt event loop."""
    if port is None:
        port = _find_free_port()
    _bp_url_queue._start_url_queue_worker()
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return port
