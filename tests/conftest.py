"""Conftest de la suite de Vflow.

Importa onnxruntime ANTES de que cualquier test module cargue PyQt6: en este
entorno (Python 3.14 + PyQt6 + onnxruntime), importar PyQt6 primero hace que
la import de onnxruntime falle con "DLL initialization routine failed", lo que
rompía los tests de VAD/métricas (test_meeting_metrics.py) solo cuando corrían
en la suite completa (test_highlight.py importa core.hotkey → PyQt6 en la
colección). Importarlo aquí primero fija el orden y evita el conflicto.

NOTA (hallazgo 3.1): el mismo conflicto aplica potencialmente al proceso de la
app (main.py carga PyQt6 antes que cualquier VAD); apply_vad hace fail-open,
pero las métricas de reunión quedarían vacías si onnxruntime no importa. Ver
reporte de la unidad 3.1 — decisión del orquestador, fuera del alcance aquí.
"""
try:
    import onnxruntime  # noqa: F401
except Exception:  # noqa: BLE001
    # Sin onnxruntime los tests de audio se saltan/fail-open por su cuenta.
    pass

import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def _aislar_entorno_real(tmp_path_factory):
    """Aísla la suite del entorno real del usuario (sesión completa).

    Dos fugas reales que esta fixture cierra:

    1. POST /api/settings persiste vía web.state._set_env_key al .env REAL
       (en dev, APP_DATA_DIR = raíz del repo). Los tests de settings dejaban
       rutas tmp de pytest guardadas en el .env del usuario entre corridas.
       → _ENV_PATH se redirige a un .env temporal de la sesión.

    2. dispatch_async (core/webhook.py) corre en un hilo daemon fire-and-forget
       que puede leer PENDING_EXPORT_DIR y APP_DATA_DIR DESPUÉS del teardown de
       monkeypatch del test que lo lanzó. Si el .env real traía un
       PENDING_EXPORT_DIR (p. ej. el contaminado por la fuga 1), el hilo escribía
       machine_id.txt en la raíz del repo — intermitente, según timing.
       → Baseline de sesión: dead-drop y webhook apagados, y el APP_DATA_DIR que
       ve core.webhook apunta a un tmp. Los tests que necesitan otro valor lo
       parchean por encima con su monkeypatch function-scoped, como ya hacen.

    Se usa pytest.MonkeyPatch de sesión (la fixture monkeypatch es function-scoped).
    Los imports van aquí y no arriba: la colección ya cargó estos módulos vía los
    test modules, y el orden onnxruntime-antes-de-PyQt6 del top de este archivo
    debe seguir intacto.
    """
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("vflow-aislamiento")

    mp.setenv("PENDING_EXPORT_DIR", "")
    mp.setenv("WEBHOOK_ENABLED", "false")

    # 3. El guard de auth local (web.state._auth_check) responde 401 a todo lo
    #    que llegue sin token, y ~29 archivos de test usan el test_client de
    #    Flask sin cookie ni cabecera. Baseline de sesión: guard APAGADO. El
    #    guard tiene su propio archivo (tests/test_dashboard_auth.py), que lo
    #    enciende explícitamente con su monkeypatch function-scoped.
    mp.setenv("DASHBOARD_AUTH_ENABLED", "false")

    import core.webhook as _webhook
    mp.setattr(_webhook, "APP_DATA_DIR", str(tmp / "appdata"))

    if "web.state" in sys.modules:
        mp.setattr(sys.modules["web.state"], "_ENV_PATH", str(tmp / ".env"))
    else:
        try:
            import web.state as _state
            mp.setattr(_state, "_ENV_PATH", str(tmp / ".env"))
        except Exception:  # noqa: BLE001  (sin flask instalado, no hay endpoint que persista)
            pass

    yield
    mp.undo()
