"""Tests para la unidad 2.3 'Chat "Esta reunión" EN VIVO' (Ola 2 de Vflow).

Cubre el contrato NUEVO entre core/assistant.py y core/meeting.py:
  - MeetingSession.snapshot(): copia atómica de transcript + insights bajo el lock.
  - build_context_live(): timestamps mm:ss y hablantes presentes; insights en el
    contexto; recorte al presupuesto por el PRINCIPIO (nunca el final) con aviso.
  - Snapshot inmutable: mutar MEETING después del snapshot no cambia el contexto.
  - answer_live() side cases obligatorios:
      1. transcript vacío  → respuesta fija SIN llamar al LLM.
      2. la reunión termina entre el snapshot y la respuesta → la respuesta sale
         igual, generada sobre el snapshot.
      3. presupuesto pequeño (18KB) con transcript largo → recorte correcto.
      4. insights vacíos → contexto sin bloque de análisis, flujo normal.
  - Ruteo del endpoint /api/meetings/chat: {"live": true} y el default con
    reunión activa usan answer_live; con meeting_id o sin reunión, el flujo DB.

No arranca audio real ni llama a ningún LLM: la reunión activa se simula seteando
_active/_segments/_insights sobre una MeetingSession real (mismo patrón que
tests/test_meeting_live.py) y el LLM se monkeypatchea.
"""

import copy

import pytest

from core import assistant
from core.meeting import MeetingSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_meeting(segments=None, insights=None, active=True):
    """MeetingSession simulada: estado interno seteado a mano, sin audio ni LLM."""
    m = MeetingSession()
    m._active = active
    m._started_at = "2026-07-03 10:00:00"
    m._segments = list(segments or [])
    if insights is not None:
        m._insights = copy.deepcopy(insights)  # los tests mutan in-situ: no compartir el demo
    return m


def _seg(t, speaker, text):
    return {"t": float(t), "speaker": speaker, "text": text}


_INSIGHTS_DEMO = {
    "temas": [{"id": 1, "text": "Presupuesto Q3"}],
    "pendientes": [{"id": 2, "texto": "Enviar la propuesta", "responsable": "Ana"}],
    "propuestas": [{"id": 3, "texto": "Subir el precio 10%", "confianza": "media"}],
    "citas": [{"id": 4, "texto": "Revisión de avance", "fecha": "2026-07-10", "hora": "09:00"}],
}


# ---------------------------------------------------------------------------
# snapshot() — copia atómica e inmutable
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_shape(self):
        m = _make_meeting([_seg(5, "Yo", "hola"), _seg(12, "Ellos", "buenas")], _INSIGHTS_DEMO)
        snap = m.snapshot()
        assert snap["active"] is True
        assert snap["started_at"] == "2026-07-03 10:00:00"
        assert [s["time"] for s in snap["segments"]] == ["00:05", "00:12"]
        assert [s["speaker"] for s in snap["segments"]] == ["Yo", "Ellos"]
        # Insights en formato plano (sin IDs)
        assert snap["insights"]["temas"] == ["Presupuesto Q3"]
        assert snap["insights"]["pendientes"][0]["texto"] == "Enviar la propuesta"

    def test_snapshot_immutable_after_mutation(self):
        m = _make_meeting([_seg(5, "Yo", "hola")], _INSIGHTS_DEMO)
        snap = m.snapshot()
        # Mutar la sesión DESPUÉS del snapshot (como haría el worker en vivo)
        m._segments.append(_seg(30, "Ellos", "texto nuevo"))
        m._insights["pendientes"][0]["texto"] = "CAMBIADO IN-SITU"
        m._insights["temas"].append({"id": 9, "text": "Tema nuevo"})
        assert len(snap["segments"]) == 1
        assert snap["insights"]["pendientes"][0]["texto"] == "Enviar la propuesta"
        assert snap["insights"]["temas"] == ["Presupuesto Q3"]


# ---------------------------------------------------------------------------
# build_context_live — timestamps, hablantes, insights, recorte
# ---------------------------------------------------------------------------

class TestBuildContextLive:
    def test_context_has_timestamps_speakers_and_insights(self):
        m = _make_meeting(
            [_seg(5, "Yo", "hola equipo"), _seg(65, "Ellos", "revisemos el presupuesto")],
            _INSIGHTS_DEMO,
        )
        ctx, meta = assistant.build_context_live("¿Puntos clave?", meeting=m)
        assert "[00:05 Yo] hola equipo" in ctx
        assert "[01:05 Ellos] revisemos el presupuesto" in ctx
        assert "ANÁLISIS EN VIVO" in ctx
        assert "Presupuesto Q3" in ctx
        assert "Enviar la propuesta" in ctx and "Ana" in ctx
        assert "REUNIÓN EN CURSO" in ctx
        assert meta["active"] is True
        assert meta["truncated"] is False
        assert meta["empty"] is False
        assert meta["segment_count"] == 2

    def test_small_budget_truncates_from_start_with_notice(self):
        # Transcript largo (~60KB) contra presupuesto 18KB (LM Studio/endpoint)
        segs = [_seg(i * 10, "Yo" if i % 2 == 0 else "Ellos",
                     f"intervención número {i} " + ("bla " * 25))
                for i in range(500)]
        m = _make_meeting(segs, _INSIGHTS_DEMO)
        ctx, meta = assistant.build_context_live("resumen", budget=18000, meeting=m)
        assert len(ctx) <= 18000
        assert meta["truncated"] is True
        assert "[transcript truncado: se muestran los últimos" in ctx
        # El FINAL se conserva siempre...
        assert "intervención número 499" in ctx
        # ...y el PRINCIPIO es lo que se recorta
        assert "intervención número 0 " not in ctx
        assert "intervención número 1 " not in ctx
        # El aviso va ANTES del primer segmento conservado (recorte por el principio)
        assert ctx.index("[transcript truncado") < ctx.index("intervención número 499")

    def test_truncation_keeps_chronological_tail_contiguous(self):
        segs = [_seg(i * 10, "Yo", f"seg{i:04d} " + ("x" * 100)) for i in range(400)]
        m = _make_meeting(segs, {"temas": [], "pendientes": [], "propuestas": [], "citas": []})
        ctx, meta = assistant.build_context_live("q", budget=18000, meeting=m)
        kept = [i for i in range(400) if f"seg{i:04d} " in ctx]
        assert kept, "debe conservar algo del transcript"
        assert kept[-1] == 399, "la última intervención nunca se pierde"
        assert kept == list(range(kept[0], 400)), "la cola conservada es contigua"

    def test_empty_insights_omit_analysis_block(self):
        m = _make_meeting([_seg(3, "Yo", "solo esto")],
                          {"temas": [], "pendientes": [], "propuestas": [], "citas": []})
        ctx, meta = assistant.build_context_live("q", meeting=m)
        assert "ANÁLISIS EN VIVO" not in ctx
        assert "[00:03 Yo] solo esto" in ctx
        assert meta["empty"] is False

    def test_empty_transcript_context(self):
        m = _make_meeting([], _INSIGHTS_DEMO)
        ctx, meta = assistant.build_context_live("q", meeting=m)
        assert meta["empty"] is True
        assert "Aún no hay intervenciones" in ctx

    def test_context_immutable_when_meeting_mutates_after_build(self):
        m = _make_meeting([_seg(5, "Yo", "contenido original")], _INSIGHTS_DEMO)
        ctx, _ = assistant.build_context_live("q", meeting=m)
        m._segments.append(_seg(90, "Ellos", "MUTACIÓN POSTERIOR"))
        m._insights["temas"][0]["text"] = "TEMA MUTADO"
        assert "contenido original" in ctx
        assert "MUTACIÓN POSTERIOR" not in ctx
        assert "TEMA MUTADO" not in ctx


# ---------------------------------------------------------------------------
# answer_live — side cases obligatorios
# ---------------------------------------------------------------------------

class TestAnswerLive:
    def test_empty_transcript_answers_without_llm(self, monkeypatch):
        """Side case 1: transcript vacío → respuesta fija, CERO llamadas al LLM."""
        def _boom(*a, **k):
            raise AssertionError("chat_memory NO debe llamarse con transcript vacío")
        monkeypatch.setattr(assistant.insights, "chat_memory", _boom)
        m = _make_meeting([], _INSIGHTS_DEMO)
        res = assistant.answer_live("¿Puntos clave hasta ahora?", meeting=m)
        assert res["ok"] is True
        assert res["empty"] is True
        assert res["live"] is True
        assert res["used_meeting_ids"] == []
        assert "Aún no hay contenido" in res["answer"]

    def test_meeting_ends_between_snapshot_and_response(self, monkeypatch):
        """Side case 2: la reunión termina mientras el LLM responde → la respuesta
        sale igual, generada sobre el snapshot tomado antes."""
        m = _make_meeting([_seg(5, "Yo", "dato clave alfa")], _INSIGHTS_DEMO)
        captured = {}

        def _fake_chat(messages, **kwargs):
            # Simular el fin de la reunión DURANTE la generación
            m._active = False
            m._segments = []
            m._insights = {"temas": [], "pendientes": [], "propuestas": [], "citas": []}
            captured["system"] = messages[0]["content"]
            return "respuesta sobre el snapshot"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        res = assistant.answer_live("¿qué se dijo?", meeting=m)
        assert res["ok"] is True
        assert res["answer"] == "respuesta sobre el snapshot"
        # El contexto que vio el LLM contiene el snapshot previo al fin
        assert "dato clave alfa" in captured["system"]
        assert assistant._SYSTEM_LIVE.split("\n")[0] in captured["system"]

    def test_small_budget_long_transcript_reaches_llm_truncated(self, monkeypatch):
        """Side case 3: presupuesto 18KB con transcript largo → el LLM recibe el
        contexto recortado por el principio, con la cola íntegra."""
        segs = [_seg(i * 10, "Yo", f"punto {i} " + ("relleno " * 20)) for i in range(600)]
        m = _make_meeting(segs, _INSIGHTS_DEMO)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        monkeypatch.setattr(assistant, "_budget_chars", lambda: 18000)
        res = assistant.answer_live("resumen", meeting=m)
        assert res["ok"] is True
        assert res["truncated"] is True
        sysc = captured["system"]
        assert "[transcript truncado" in sysc
        assert "punto 599 " in sysc
        assert "punto 0 " not in sysc
        assert len(sysc) <= 18000 + len(assistant._SYSTEM_LIVE) + 2

    def test_empty_insights_normal_flow(self, monkeypatch):
        """Side case 4: insights vacíos → flujo normal, contexto sin bloque de análisis."""
        m = _make_meeting([_seg(7, "Ellos", "hablamos de logística")],
                          {"temas": [], "pendientes": [], "propuestas": [], "citas": []})
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "sin insights, respondo del transcript"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        res = assistant.answer_live("¿temas?", meeting=m)
        assert res["ok"] is True
        assert "ANÁLISIS EN VIVO" not in captured["system"]
        assert "[00:07 Ellos] hablamos de logística" in captured["system"]

    def test_return_shape_matches_answer_contract(self, monkeypatch):
        monkeypatch.setattr(assistant.insights, "chat_memory", lambda *a, **k: "r")
        m = _make_meeting([_seg(1, "Yo", "x")], _INSIGHTS_DEMO)
        res = assistant.answer_live("q", meeting=m)
        # MISMO shape que answer(): ok / answer / used_meeting_ids / reasoned
        for key in ("ok", "answer", "used_meeting_ids", "reasoned"):
            assert key in res
        assert res["used_meeting_ids"] == []

    def test_empty_message_error(self):
        m = _make_meeting([_seg(1, "Yo", "x")], _INSIGHTS_DEMO)
        res = assistant.answer_live("   ", meeting=m)
        assert res["ok"] is False

    def test_history_passthrough(self, monkeypatch):
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return "r"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        m = _make_meeting([_seg(1, "Yo", "x")], _INSIGHTS_DEMO)
        hist = [{"role": "user", "content": "antes"}, {"role": "assistant", "content": "resp"}]
        res = assistant.answer_live("q", history=hist, meeting=m)
        assert res["ok"] is True
        roles = [msg["role"] for msg in captured["messages"]]
        assert roles == ["system", "user", "assistant", "user"]


# ---------------------------------------------------------------------------
# Ruteo del endpoint /api/meetings/chat (vivo vs DB)
# ---------------------------------------------------------------------------

class TestEndpointRouting:
    @pytest.fixture()
    def client(self):
        from web import server as web_server
        web_server.app.config["TESTING"] = True
        with web_server.app.test_client() as c:
            yield c

    def test_explicit_live_true_uses_answer_live(self, client, monkeypatch):
        from web import server as web_server
        calls = {}

        def _fake_live(message, **kw):
            calls["live"] = message
            return {"ok": True, "answer": "L", "used_meeting_ids": [],
                    "reasoned": False, "live": True}

        monkeypatch.setattr(web_server._assistant, "answer_live", _fake_live)
        monkeypatch.setattr(
            web_server._assistant, "answer",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no debe usar el flujo DB")))
        r = client.post("/api/meetings/chat", json={"message": "hola", "live": True})
        assert r.status_code == 200
        assert r.get_json()["answer"] == "L"
        assert calls["live"] == "hola"

    def test_default_active_meeting_without_meeting_id_uses_live(self, client, monkeypatch):
        from web import server as web_server
        monkeypatch.setattr(web_server.MEETING, "_active", True)
        monkeypatch.setattr(
            web_server._assistant, "answer_live",
            lambda message, **kw: {"ok": True, "answer": "LIVE", "used_meeting_ids": [],
                                   "reasoned": False, "live": True})
        r = client.post("/api/meetings/chat", json={"message": "hola"})
        assert r.status_code == 200
        assert r.get_json()["answer"] == "LIVE"

    def test_active_meeting_with_meeting_id_uses_db_flow(self, client, monkeypatch):
        from web import server as web_server
        monkeypatch.setattr(web_server.MEETING, "_active", True)
        monkeypatch.setattr(
            web_server._assistant, "answer",
            lambda db, message, **kw: {"ok": True, "answer": "DB", "used_meeting_ids": [7],
                                       "reasoned": False})
        monkeypatch.setattr(
            web_server._assistant, "answer_live",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no debe usar el flujo vivo")))
        r = client.post("/api/meetings/chat", json={"message": "hola", "meeting_id": 7})
        assert r.status_code == 200
        assert r.get_json()["answer"] == "DB"

    def test_inactive_meeting_defaults_to_db_flow(self, client, monkeypatch):
        from web import server as web_server
        monkeypatch.setattr(web_server.MEETING, "_active", False)
        monkeypatch.setattr(
            web_server._assistant, "answer",
            lambda db, message, **kw: {"ok": True, "answer": "DB", "used_meeting_ids": [],
                                       "reasoned": False})
        r = client.post("/api/meetings/chat", json={"message": "hola"})
        assert r.status_code == 200
        assert r.get_json()["answer"] == "DB"

    def test_explicit_live_false_forces_db_even_if_active(self, client, monkeypatch):
        from web import server as web_server
        monkeypatch.setattr(web_server.MEETING, "_active", True)
        monkeypatch.setattr(
            web_server._assistant, "answer",
            lambda db, message, **kw: {"ok": True, "answer": "DB", "used_meeting_ids": [],
                                       "reasoned": False})
        monkeypatch.setattr(
            web_server._assistant, "answer_live",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("live=false debe ir a DB")))
        r = client.post("/api/meetings/chat", json={"message": "hola", "live": False})
        assert r.status_code == 200
        assert r.get_json()["answer"] == "DB"
