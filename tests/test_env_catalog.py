"""Tests de sincronía del catálogo único de variables de entorno (unidad 4.3).

Opción A del PLAN-MEJORAS: ``config.ENV_CATALOG`` es un REGISTRO documentado,
NO una capa de indirección — los call-sites en producción (main.py, config.py,
core/*, web/*, db/*, mcp_server/*) siguen haciendo ``os.getenv(...)`` /
``os.environ.get(...)`` directamente, con relectura perezosa (hot-reload sin
reiniciar la app). Este archivo parsea con AST los mismos archivos de
producción y falla si:

  (a) existe una lectura de entorno cuya variable NO está en ``ENV_CATALOG``
      (ni en la lista manual de lecturas dinámicas, ni en la lista de
      variables de plataforma excluidas);
  (b) el default de un call-site diverge del default cataloreado, salvo las
      divergencias toleradas explícitamente en ``config.ENV_KNOWN_DIVERGENCES``;
  (c) una entrada de ``ENV_CATALOG`` ya no tiene ningún call-site real (catálogo
      con entradas fantasma que envejecen igual que el código muerto).

Valores NO-LITERALES (defaults computados, p. ej. ``os.path.join(...)`` o una
constante importada de otro módulo) se toleran: no se comparan por valor, solo
se registra que el call-site existe. ``ENV_CATALOG`` documenta esos casos con
``"default": None`` y una nota en ``"doc"``.

También cubre un smoke test de hot-reload para 5 variables representativas
(unidad 4.3, paso 5) y un test que PINEA el bug conocido de
CLAUDE_CLI_MODEL_BATCH/_LIVE (para avisar si algún día se corrige y la nota de
``known_divergence`` en ``config.ENV_CATALOG`` queda obsoleta).
"""
import ast
import os

import pytest

import config

_ROOT = os.path.dirname(os.path.abspath(config.__file__))

_PROD_TOP_LEVEL_FILES = {"main.py", "config.py"}
_PROD_TOP_LEVEL_DIRS = {"core", "web", "db", "mcp_server"}
_PRUNE_DIRS = {
    "venv", ".git", "build", "dist", "__pycache__", "tests", "node_modules",
    ".pytest_cache", "models",
}

# Lecturas con NOMBRE DE VARIABLE COMPUTADO (no un string literal en el propio
# os.getenv), documentadas a mano porque el AST no puede resolverlas como una
# lectura nombrada. Ver ENV_CATALOG[*]["dynamic"] = True para el lado del catálogo.
# Fuente: core/meeting.py MeetingSession._DETECTION_FLAGS (unidad 5.1) —
# ``os.getenv(env_name, default)`` en _detection_class_enabled(), con env_name
# resuelto desde ese dict, no un literal en la llamada.
_DYNAMIC_READ_VARS = {
    "PROACTIVE_DETECT_PREGUNTAS",
    "PROACTIVE_DETECT_COMPROMISOS",
    "PROACTIVE_DETECT_ACUERDOS",
}

# Sentinel para "el default de este call-site no es un literal AST" (distinto
# de python `None`, que SÍ es un default literal válido — os.getenv(x) sin 2do
# argumento).
_NONLITERAL = object()


def _iter_prod_files():
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        rel_dir = os.path.relpath(dirpath, _ROOT)
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            relpath = os.path.normpath(os.path.join(rel_dir, fn)) if rel_dir != "." else fn
            top = relpath.split(os.sep)[0]
            if relpath in _PROD_TOP_LEVEL_FILES or top in _PROD_TOP_LEVEL_DIRS:
                yield relpath


def _literal_default(node):
    """Default de un call-site: valor python si es ast.Constant, o _NONLITERAL."""
    if node is None:
        return None  # sin 2do argumento -> default real es None
    if isinstance(node, ast.Constant):
        return node.value
    return _NONLITERAL


def _literal_varname(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None  # nombre de variable computado (dinámico)


def _scan_env_reads():
    """Recorre los archivos de producción y devuelve
    {varname_o_None: [(default, relpath, lineno), ...]}.

    varname es None para lecturas con nombre computado (dinámicas); esas
    entradas se agrupan bajo la key None y se cruzan aparte con
    _DYNAMIC_READ_VARS (no podemos saber el nombre real solo con AST)."""
    reads = {}

    def _add(varname, default, relpath, lineno):
        reads.setdefault(varname, []).append((default, relpath, lineno))

    for relpath in _iter_prod_files():
        full = os.path.join(_ROOT, relpath)
        with open(full, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=relpath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "getenv"
                        and isinstance(func.value, ast.Name) and func.value.id == "os"):
                    args = node.args
                    varname = _literal_varname(args[0]) if args else None
                    default = _literal_default(args[1]) if len(args) > 1 else None
                    _add(varname, default, relpath, node.lineno)
                elif (isinstance(func, ast.Attribute) and func.attr == "get"
                        and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
                        and isinstance(func.value.value, ast.Name) and func.value.value.id == "os"):
                    args = node.args
                    varname = _literal_varname(args[0]) if args else None
                    default = _literal_default(args[1]) if len(args) > 1 else None
                    _add(varname, default, relpath, node.lineno)
            elif isinstance(node, ast.Subscript):
                val = node.value
                if (isinstance(val, ast.Attribute) and val.attr == "environ"
                        and isinstance(val.value, ast.Name) and val.value.id == "os"
                        and isinstance(node.ctx, ast.Load)):
                    slice_node = node.slice
                    if isinstance(slice_node, ast.Index):  # py<3.9 compat, no-op en 3.9+
                        slice_node = slice_node.value
                    varname = _literal_varname(slice_node)
                    _add(varname, _NONLITERAL, relpath, node.lineno)
    return reads


@pytest.fixture(scope="module")
def env_reads():
    return _scan_env_reads()


# ---------------------------------------------------------------------------
# (a) Toda lectura literal debe estar catalogada
# ---------------------------------------------------------------------------

def test_every_literal_env_read_is_cataloged(env_reads):
    uncataloged = []
    for varname, sites in env_reads.items():
        if varname is None:
            continue  # lectura dinámica, sin nombre AST — no evaluable aquí
        if varname in config.ENV_CATALOG_EXCLUDED_SYSTEM_VARS:
            continue  # variable de plataforma (APPDATA, SystemRoot...), fuera de alcance
        if varname not in config.ENV_CATALOG:
            uncataloged.append((varname, sites))
    assert not uncataloged, (
        "Variables de entorno leídas en producción sin entrada en ENV_CATALOG "
        f"(config.py): {[(v, s) for v, s in uncataloged]}"
    )


def test_dynamic_reads_have_expected_var_names(env_reads):
    """Sanity check de las lecturas con nombre computado (varname=None): deben
    existir (core/meeting.py sigue llamando os.getenv con env_name dinámico) y
    su cuenta coincide con lo documentado en _DYNAMIC_READ_VARS. Si este número
    cambia, _DETECTION_FLAGS (core/meeting.py) probablemente creció o se quitó
    una clase de detección — actualizar _DYNAMIC_READ_VARS y ENV_CATALOG."""
    dynamic_sites = env_reads.get(None, [])
    # Al menos una lectura dinámica real (no solo el caso legado no-default de
    # DICTATION_MODE_MAP, que SÍ tiene varname literal). Si baja a 0, alguien
    # quitó el patrón de _DETECTION_FLAGS o lo hizo literal — revisar y, si es
    # el segundo caso, borrar esta sección y mover las 3 vars a estáticas.
    assert len(dynamic_sites) >= 1

    # Las 3 variables de _DYNAMIC_READ_VARS deben seguir apareciendo LITERALMENTE
    # como strings en core/meeting.py (no podemos verificarlo por AST porque el
    # nombre es computado; un grep de texto es la red de seguridad barata).
    meeting_py = os.path.join(_ROOT, "core", "meeting.py")
    with open(meeting_py, "r", encoding="utf-8") as f:
        src = f.read()
    for varname in _DYNAMIC_READ_VARS:
        assert varname in src, (
            f"{varname} ya no aparece en core/meeting.py — "
            "actualiza _DYNAMIC_READ_VARS y ENV_CATALOG (unidad 4.3)."
        )


# ---------------------------------------------------------------------------
# (b) El default de cada call-site debe coincidir con ENV_CATALOG, salvo
#     ENV_KNOWN_DIVERGENCES; los defaults no-literales se toleran sin comparar.
# ---------------------------------------------------------------------------

def test_call_site_defaults_match_catalog(env_reads):
    mismatches = []
    for varname, sites in env_reads.items():
        if varname is None or varname in config.ENV_CATALOG_EXCLUDED_SYSTEM_VARS:
            continue
        entry = config.ENV_CATALOG.get(varname)
        if entry is None:
            continue  # ya reportado por test_every_literal_env_read_is_cataloged
        catalog_default = entry["default"]
        allowed = set()
        divergence = config.ENV_KNOWN_DIVERGENCES.get(varname)
        if divergence:
            allowed.update(divergence["allowed_defaults"])
        for default, relpath, lineno in sites:
            if default is _NONLITERAL:
                continue  # default computado, tolerado (documentado en catálogo)
            if default == catalog_default:
                continue
            if default in allowed:
                continue
            mismatches.append((varname, default, catalog_default, relpath, lineno))
    assert not mismatches, (
        "Defaults de call-site que divergen de ENV_CATALOG (o de "
        f"ENV_KNOWN_DIVERGENCES) — (var, default_encontrado, default_catálogo, "
        f"archivo, línea): {mismatches}"
    )


# ---------------------------------------------------------------------------
# (c) Ninguna entrada de ENV_CATALOG debe ser fantasma (sin call-site real)
# ---------------------------------------------------------------------------

def test_no_phantom_catalog_entries(env_reads):
    literal_varnames = {v for v in env_reads if v is not None}
    phantom = []
    for varname, entry in config.ENV_CATALOG.items():
        if entry.get("dynamic"):
            assert varname in _DYNAMIC_READ_VARS, (
                f"{varname} está marcada dynamic=True en ENV_CATALOG pero no está "
                "en _DYNAMIC_READ_VARS de este test — revisa ambas listas."
            )
            continue
        if varname not in literal_varnames:
            phantom.append(varname)
    assert not phantom, f"Entradas de ENV_CATALOG sin ningún call-site real: {phantom}"


def test_known_divergences_and_excluded_vars_are_still_relevant():
    """Si ENV_KNOWN_DIVERGENCES o ENV_CATALOG_EXCLUDED_SYSTEM_VARS acumulan una
    entrada que ya no aplica (nadie la lee así), es deuda que envejece en
    silencio. Chequeo barato: todas deben aparecer como texto en algún archivo
    de producción."""
    all_src = ""
    for relpath in _iter_prod_files():
        with open(os.path.join(_ROOT, relpath), "r", encoding="utf-8") as f:
            all_src += f.read()
    for varname in config.ENV_KNOWN_DIVERGENCES:
        assert varname in all_src, f"known_divergence {varname} ya no aparece en producción."
    for varname in config.ENV_CATALOG_EXCLUDED_SYSTEM_VARS:
        assert varname in all_src, f"variable de plataforma excluida {varname} ya no aparece en producción."


# ---------------------------------------------------------------------------
# Bug conocido pineado: CLAUDE_CLI_MODEL_BATCH/_LIVE (ver ENV_CATALOG["known_divergence"])
# ---------------------------------------------------------------------------

class TestClaudeCliModelBugPinned:
    """`core.insights._model()` (~línea 218) SIEMPRE lee CLAUDE_CLI_MODEL_BATCH
    para backend='claude-cli', sin importar el `task` pedido — nunca lee
    CLAUDE_CLI_MODEL_LIVE desde esa función (el dispatch REAL de inferencia en
    `_chat_claude_cli`, ~líneas 574-577, sí branchea bien por task). No se
    corrige en la unidad 4.3 (fuera de alcance — ver docs/PENDIENTES.md).

    Este test PINEA el bug: si algún día `_model()` se arregla para leer
    CLAUDE_CLI_MODEL_LIVE cuando task='live', este test EMPIEZA A FALLAR — esa
    es la señal para borrar el campo "known_divergence" de CLAUDE_CLI_MODEL_BATCH
    y CLAUDE_CLI_MODEL_LIVE en config.ENV_CATALOG y cerrar el ítem de
    docs/PENDIENTES.md.
    """

    def test_model_ignores_live_var_for_claude_cli_backend(self, monkeypatch):
        import core.insights as insights

        monkeypatch.setenv("CLAUDE_CLI_MODEL_BATCH", "sonnet-batch-default")
        monkeypatch.setenv("CLAUDE_CLI_MODEL_LIVE", "haiku-live-default")

        # BUG: pedir el modelo para task="live" con backend claude-cli debería
        # (idealmente) devolver el valor de CLAUDE_CLI_MODEL_LIVE, pero _model()
        # siempre resuelve CLAUDE_CLI_MODEL_BATCH sin importar el task.
        live_model = insights._model(task="live", backend="claude-cli")
        batch_model = insights._model(task="batch", backend="claude-cli")

        assert live_model == "sonnet-batch-default", (
            "_model() dejó de ignorar CLAUDE_CLI_MODEL_LIVE para task='live' — "
            "el bug parece corregido. Actualiza config.ENV_CATALOG (borra el "
            "campo known_divergence de CLAUDE_CLI_MODEL_BATCH/_LIVE) y cierra el "
            "ítem correspondiente en docs/PENDIENTES.md."
        )
        assert batch_model == "sonnet-batch-default"

    def test_catalog_documents_the_bug(self):
        assert config.ENV_CATALOG["CLAUDE_CLI_MODEL_BATCH"].get("known_divergence")
        assert config.ENV_CATALOG["CLAUDE_CLI_MODEL_LIVE"].get("known_divergence")


# ---------------------------------------------------------------------------
# Hot-reload — 5 variables representativas (unidad 4.3, paso 5)
# ---------------------------------------------------------------------------
# PROACTIVE_MODE y PROACTIVE_DETECT_PREGUNTAS YA tienen cobertura equivalente de
# hot-reload en la suite (no se duplica aquí):
#   - PROACTIVE_MODE: tests/test_proactive.py (TestGetMode, setea env DESPUÉS de
#     importar core.proactive y llama get_mode() en el mismo test — el módulo ya
#     estaba importado desde la colección de tests).
#   - PROACTIVE_DETECT_PREGUNTAS: tests/test_detections.py (setea
#     PROACTIVE_DETECT_PREGUNTAS="false" tras importar core.meeting).
# Este archivo solo verifica que las 3 PROACTIVE_DETECT_* están catalogadas
# como lazy (contrato explícito pedido en la unidad 4.3) y cubre con test
# NUEVO las 3 variables sin cobertura de hot-reload: TRANSCRIPTION_BACKEND,
# AUDIO_SOURCE y SAVE_HISTORY.

def test_proactive_detect_flags_are_cataloged_as_lazy():
    for varname in (
        "PROACTIVE_DETECT_PREGUNTAS",
        "PROACTIVE_DETECT_COMPROMISOS",
        "PROACTIVE_DETECT_ACUERDOS",
    ):
        entry = config.ENV_CATALOG[varname]
        assert entry["kind"] == "lazy"
        assert entry["killswitch"] is True


def test_transcription_backend_hot_reload(monkeypatch):
    """core.backends.get_backend() debe reflejar TRANSCRIPTION_BACKEND cambiado
    DESPUÉS de importar el módulo, sin reimport (el factory relee os.getenv en
    cada llamada — nunca cachea el NOMBRE del backend, solo la instancia por
    nombre resuelto)."""
    from core.backends import get_backend
    from core.backends.groq_backend import GroqBackend
    from core.backends.local_backend import LocalBackend

    monkeypatch.setenv("TRANSCRIPTION_BACKEND", "groq")
    assert isinstance(get_backend(), GroqBackend)

    monkeypatch.setenv("TRANSCRIPTION_BACKEND", "local")
    assert isinstance(get_backend(), LocalBackend)

    monkeypatch.setenv("TRANSCRIPTION_BACKEND", "groq")
    assert isinstance(get_backend(), GroqBackend)


def test_audio_source_hot_reload(monkeypatch):
    """GET /api/settings (web/blueprints/settings.py) relee AUDIO_SOURCE en cada
    request, sin caché a nivel de módulo ni de app Flask."""
    from web.server import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        monkeypatch.setenv("AUDIO_SOURCE", "mic")
        resp = client.get("/api/settings")
        assert resp.get_json()["audio_source"] == "mic"

        monkeypatch.setenv("AUDIO_SOURCE", "system")
        resp = client.get("/api/settings")
        assert resp.get_json()["audio_source"] == "system"


def test_save_history_hot_reload(monkeypatch):
    """GET /api/settings relee SAVE_HISTORY en cada request (toggle sin reiniciar
    la app, contrato documentado desde la unidad de Historial Privacy Mode)."""
    from web.server import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        monkeypatch.setenv("SAVE_HISTORY", "true")
        resp = client.get("/api/settings")
        assert resp.get_json()["save_history"] is True

        monkeypatch.setenv("SAVE_HISTORY", "false")
        resp = client.get("/api/settings")
        assert resp.get_json()["save_history"] is False
