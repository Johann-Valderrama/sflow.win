"""Tests de los endpoints nuevos de la unidad 5.3 (web/blueprints/meetings.py):

  - ``POST /api/meetings/chat-multi``: validación (400s) + happy path con
    ``core.assistant.answer_multi`` monkeypatcheado (mismo patrón que
    tests/test_meeting_incremental.py: ``web.server.app.test_client()``).
  - ``POST /api/meetings/deliverable-export``: crea el .md en ``entregables/``
    con el prefijo correcto, 400 sin contenido / sin PENDING_EXPORT_DIR, y
    JAMÁS escribe con el prefijo ``vflow-pendientes-`` (ese naming es el
    contrato de tareas del dead-drop de core/webhook.py, unidad 5.4 — un
    consumidor externo lo vigila; mezclarlos rompería ese contrato).
"""
import os

import pytest


@pytest.fixture()
def client():
    from web import server as web_server
    web_server.app.config["TESTING"] = True
    with web_server.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# POST /api/meetings/chat-multi
# ---------------------------------------------------------------------------

class TestChatMultiValidation:
    def test_empty_message_400(self, client):
        r = client.post("/api/meetings/chat-multi", json={"meeting_ids": [1]})
        assert r.status_code == 400

    def test_no_ids_no_query_400(self, client):
        r = client.post("/api/meetings/chat-multi", json={"message": "hola"})
        assert r.status_code == 400

    def test_unknown_template_400(self, client):
        r = client.post("/api/meetings/chat-multi",
                        json={"message": "hola", "meeting_ids": [1], "template": "no-existe"})
        assert r.status_code == 400

    def test_non_integer_ids_discarded_falls_back_to_400(self, client):
        """Todos los ids son basura -> se descartan -> sin ids ni query -> 400."""
        r = client.post("/api/meetings/chat-multi",
                        json={"message": "hola", "meeting_ids": ["a", "b", None]})
        assert r.status_code == 400


class TestChatMultiHappyPath:
    def test_happy_path_calls_answer_multi(self, client, monkeypatch):
        from web.blueprints import meetings as bp_meetings

        captured = {}

        def stub_answer_multi(db, message, meeting_ids=None, fts_query=None,
                              history=None, template=None, **kw):
            captured["message"] = message
            captured["meeting_ids"] = meeting_ids
            captured["fts_query"] = fts_query
            captured["template"] = template
            return {
                "ok": True, "answer": "[stub] respuesta",
                "reuniones_incluidas": [{"id": 1, "titulo": "Reunión A", "fecha": "2026-06-01"}],
                "excluidas": [], "template_usado": template, "reasoned": False,
            }

        monkeypatch.setattr(bp_meetings._assistant, "answer_multi", stub_answer_multi)

        r = client.post("/api/meetings/chat-multi",
                        json={"message": "¿Qué se decidió?", "meeting_ids": [1, 2]})
        assert r.status_code == 200
        data = r.get_json()
        assert data["answer"] == "[stub] respuesta"
        assert data["reuniones_incluidas"][0]["id"] == 1
        assert captured["meeting_ids"] == [1, 2]
        assert captured["fts_query"] is None

    def test_ids_take_priority_over_fts_query(self, client, monkeypatch):
        from web.blueprints import meetings as bp_meetings

        captured = {}

        def stub_answer_multi(db, message, meeting_ids=None, fts_query=None, **kw):
            captured["meeting_ids"] = meeting_ids
            captured["fts_query"] = fts_query
            return {"ok": True, "answer": "ok", "reuniones_incluidas": [], "excluidas": [],
                    "template_usado": None, "reasoned": False}

        monkeypatch.setattr(bp_meetings._assistant, "answer_multi", stub_answer_multi)

        r = client.post("/api/meetings/chat-multi",
                        json={"message": "hola", "meeting_ids": [5], "fts_query": "presupuesto"})
        assert r.status_code == 200
        assert captured["meeting_ids"] == [5]
        assert captured["fts_query"] is None  # ids ganan: fts_query se descarta en el endpoint

    def test_backend_error_maps_to_503(self, client, monkeypatch):
        from web.blueprints import meetings as bp_meetings

        def stub_fail(db, message, **kw):
            return {"ok": False, "error": "backend caído", "reasoned": False}

        monkeypatch.setattr(bp_meetings._assistant, "answer_multi", stub_fail)

        r = client.post("/api/meetings/chat-multi", json={"message": "hola", "meeting_ids": [1]})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# POST /api/meetings/deliverable-export
# ---------------------------------------------------------------------------

class TestDeliverableExport:
    def test_empty_content_400(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("PENDING_EXPORT_DIR", str(tmp_path))
        r = client.post("/api/meetings/deliverable-export", json={"content": "  "})
        assert r.status_code == 400

    def test_no_export_dir_configured_400(self, client, monkeypatch):
        monkeypatch.delenv("PENDING_EXPORT_DIR", raising=False)
        r = client.post("/api/meetings/deliverable-export", json={"content": "Hola mundo"})
        assert r.status_code == 400
        assert "Ajustes" in r.get_json()["error"]

    def test_creates_file_in_entregables_subfolder_with_correct_prefix(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("PENDING_EXPORT_DIR", str(tmp_path))
        r = client.post("/api/meetings/deliverable-export",
                        json={"content": "# Email de seguimiento\n\nHola.", "titulo": "Reunión de Ventas"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        path = data["path"]
        assert os.path.isfile(path)
        assert os.path.dirname(path) == str(tmp_path / "entregables")
        fname = os.path.basename(path)
        assert fname.startswith("vflow-entregable-")
        assert "reunion-de-ventas" in fname or "reuni" in fname  # slug ASCII del título
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == "# Email de seguimiento\n\nHola."

    def test_never_writes_pendientes_prefix(self, client, monkeypatch, tmp_path):
        """Naming del dead-drop de tareas (vflow-pendientes-*) es un contrato externo
        distinto (core/webhook.py, unidad 5.4): el entregable NUNCA debe usar ese prefijo."""
        monkeypatch.setenv("PENDING_EXPORT_DIR", str(tmp_path))
        client.post("/api/meetings/deliverable-export", json={"content": "contenido", "titulo": "t"})
        entregables_dir = tmp_path / "entregables"
        files = list(entregables_dir.iterdir()) if entregables_dir.is_dir() else []
        assert files
        for f in files:
            assert not f.name.startswith("vflow-pendientes-")
            assert f.name.startswith("vflow-entregable-")

    def test_collision_gets_unique_suffix_not_overwritten(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("PENDING_EXPORT_DIR", str(tmp_path))
        r1 = client.post("/api/meetings/deliverable-export", json={"content": "primero", "titulo": "mismo"})
        r2 = client.post("/api/meetings/deliverable-export", json={"content": "segundo", "titulo": "mismo"})
        p1 = r1.get_json()["path"]
        p2 = r2.get_json()["path"]
        assert p1 != p2
        with open(p1, "r", encoding="utf-8") as f:
            assert f.read() == "primero"
        with open(p2, "r", encoding="utf-8") as f:
            assert f.read() == "segundo"
