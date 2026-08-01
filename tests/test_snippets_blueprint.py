"""Tests para la unidad 4c de PLAN-DICTADO-2026-07-31: panel de gestión de
snippets, `web/blueprints/snippets.py` (HTTP sobre el CRUD de la unidad 4a).

Cubre:
(a) listado (incluye deshabilitados)
(b) alta: camino feliz, 400 con disparador/cuerpo vacío, 409 con disparador
    duplicado (mayúsculas/acentos no cuentan como distintos) y que el mensaje
    de error sea texto entendible, no un volcado de excepción
(c) edición (PUT): trigger y/o body, 404 sobre un id inexistente, 409 si el
    nuevo disparador choca con OTRO snippet
(d) activar/desactivar (PATCH): 404 sobre un id inexistente
(e) borrado (DELETE): 404 sobre un id inexistente
(f) cada escritura invalida la caché del matcher (`core.snippets_matcher`),
    para que el próximo dictado vea el cambio sin esperar el TTL perezoso

Aislamiento: mismo patrón que `tests/test_dictation_modes.py::TestSettingsEndpoint`
para el `test_client` de Flask (DB temporal bajo `tmp_path`, nunca la real de
dev) + el patrón de `tests/test_snippets_matcher.py` para resetear la caché
global de `core.snippets_matcher` antes y después de CADA test (si no,
snippets instalados por un test contaminarían otros, incluidos los de otro
archivo que corra en el mismo proceso de pytest).
"""

import pytest

flask = pytest.importorskip("flask")

import core.snippets_matcher as _snippets_matcher


def _reset_snippets_cache():
    _snippets_matcher._db = None
    _snippets_matcher._cache = _snippets_matcher._EMPTY_CACHE


@pytest.fixture(autouse=True)
def _aislar_cache_snippets():
    _reset_snippets_cache()
    yield
    _reset_snippets_cache()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from web.server import app, _db
    import config

    db_path = str(tmp_path / "test_snippets_blueprint.db")
    assert db_path != config.DB_PATH  # nunca la base real de producción
    _db.db_path = db_path
    _db._init_db()
    # El matcher tiene su PROPIA instancia lazy de TranscriptionDB (import
    # circular evitado a propósito, ver core/snippets_matcher.py::_get_db).
    # Sin esto, invalidate() la crearía apuntando a config.DB_PATH real.
    monkeypatch.setattr(_snippets_matcher, "_db", _db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ===========================================================================
# (a) Listado
# ===========================================================================
class TestListado:
    def test_lista_vacia(self, client):
        resp = client.get("/api/snippets")
        assert resp.status_code == 200
        assert resp.get_json() == {"snippets": []}

    def test_incluye_deshabilitados(self, client):
        add = client.post("/api/snippets", json={"trigger": "firma correo", "body": "Saludos, Johann"})
        sid = add.get_json()["id"]
        client.patch(f"/api/snippets/{sid}", json={"enabled": False})
        data = client.get("/api/snippets").get_json()
        assert len(data["snippets"]) == 1
        assert data["snippets"][0]["enabled"] == 0


# ===========================================================================
# (b) Alta
# ===========================================================================
class TestAlta:
    def test_camino_feliz(self, client):
        resp = client.post("/api/snippets", json={"trigger": "firma correo", "body": "Saludos,\nJohann"})
        assert resp.status_code == 201
        assert isinstance(resp.get_json()["id"], int)
        listado = client.get("/api/snippets").get_json()["snippets"]
        assert listado[0]["trigger"] == "firma correo"
        assert listado[0]["body"] == "Saludos,\nJohann"
        assert listado[0]["enabled"] == 1
        assert listado[0]["hit_count"] == 0

    def test_disparador_vacio_es_400(self, client):
        resp = client.post("/api/snippets", json={"trigger": "   ", "body": "algo"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_cuerpo_vacio_es_400(self, client):
        resp = client.post("/api/snippets", json={"trigger": "firma", "body": "   "})
        assert resp.status_code == 400

    def test_sin_body_en_json_es_400_no_500(self, client):
        resp = client.post("/api/snippets", json={"trigger": "firma"})
        assert resp.status_code == 400

    def test_duplicado_es_409_con_mensaje_entendible(self, client):
        client.post("/api/snippets", json={"trigger": "Firma Correo", "body": "uno"})
        resp = client.post("/api/snippets", json={"trigger": "firma   correo", "body": "dos"})
        assert resp.status_code == 409
        err = resp.get_json()["error"]
        assert "firma" in err.lower()
        # No es un volcado de excepción de sqlite3 ni un rastro Python.
        assert "Traceback" not in err
        assert "sqlite3" not in err

    def test_alta_invalida_la_cache_del_matcher(self, client):
        client.post("/api/snippets", json={"trigger": "pega firma", "body": "Johann Valderrama"})
        assert _snippets_matcher.expand_snippets("pega firma") == "Johann Valderrama"


# ===========================================================================
# (c) Edición (PUT)
# ===========================================================================
class TestEdicion:
    def test_edita_trigger_y_body(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.put(f"/api/snippets/{sid}", json={"trigger": "firma larga", "body": "v2"})
        assert resp.status_code == 200
        row = client.get("/api/snippets").get_json()["snippets"][0]
        assert row["trigger"] == "firma larga"
        assert row["body"] == "v2"

    def test_edita_solo_body(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.put(f"/api/snippets/{sid}", json={"body": "v2"})
        assert resp.status_code == 200
        row = client.get("/api/snippets").get_json()["snippets"][0]
        assert row["trigger"] == "firma"
        assert row["body"] == "v2"

    def test_id_inexistente_es_404(self, client):
        resp = client.put("/api/snippets/9999", json={"body": "algo"})
        assert resp.status_code == 404

    def test_sin_campos_es_400(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.put(f"/api/snippets/{sid}", json={})
        assert resp.status_code == 400

    def test_editar_a_disparador_de_otro_snippet_es_409(self, client):
        client.post("/api/snippets", json={"trigger": "firma corta", "body": "a"})
        sid_b = client.post("/api/snippets", json={"trigger": "firma larga", "body": "b"}).get_json()["id"]
        resp = client.put(f"/api/snippets/{sid_b}", json={"trigger": "Firma Corta"})
        assert resp.status_code == 409

    def test_edicion_invalida_la_cache_del_matcher(self, client):
        sid = client.post("/api/snippets", json={"trigger": "pega firma", "body": "v1"}).get_json()["id"]
        assert _snippets_matcher.expand_snippets("pega firma") == "v1"
        client.put(f"/api/snippets/{sid}", json={"body": "v2"})
        assert _snippets_matcher.expand_snippets("pega firma") == "v2"


# ===========================================================================
# (d) Activar/desactivar (PATCH)
# ===========================================================================
class TestToggle:
    def test_desactiva_y_reactiva(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.patch(f"/api/snippets/{sid}", json={"enabled": False})
        assert resp.status_code == 200
        assert client.get("/api/snippets").get_json()["snippets"][0]["enabled"] == 0
        client.patch(f"/api/snippets/{sid}", json={"enabled": True})
        assert client.get("/api/snippets").get_json()["snippets"][0]["enabled"] == 1

    def test_id_inexistente_es_404(self, client):
        resp = client.patch("/api/snippets/9999", json={"enabled": False})
        assert resp.status_code == 404

    def test_sin_campo_enabled_es_400(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.patch(f"/api/snippets/{sid}", json={})
        assert resp.status_code == 400

    def test_desactivar_invalida_la_cache_del_matcher(self, client):
        sid = client.post("/api/snippets", json={"trigger": "pega firma", "body": "v1"}).get_json()["id"]
        assert _snippets_matcher.expand_snippets("pega firma") == "v1"
        client.patch(f"/api/snippets/{sid}", json={"enabled": False})
        # Con el snippet desactivado, el matcher deja el texto intacto.
        assert _snippets_matcher.expand_snippets("pega firma") == "pega firma"


# ===========================================================================
# (e) Borrado (DELETE)
# ===========================================================================
class TestBorrado:
    def test_borra(self, client):
        sid = client.post("/api/snippets", json={"trigger": "firma", "body": "v1"}).get_json()["id"]
        resp = client.delete(f"/api/snippets/{sid}")
        assert resp.status_code == 204
        assert client.get("/api/snippets").get_json()["snippets"] == []

    def test_id_inexistente_es_404(self, client):
        resp = client.delete("/api/snippets/9999")
        assert resp.status_code == 404

    def test_borrado_invalida_la_cache_del_matcher(self, client):
        sid = client.post("/api/snippets", json={"trigger": "pega firma", "body": "v1"}).get_json()["id"]
        assert _snippets_matcher.expand_snippets("pega firma") == "v1"
        client.delete(f"/api/snippets/{sid}")
        assert _snippets_matcher.expand_snippets("pega firma") == "pega firma"
