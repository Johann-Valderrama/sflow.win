"""Tests para la unidad 5.1 'Detecciones proactivas' (Ola 5).

Cubre:
  - update_state con detections_out: detecciones parseadas y SEPARADAS del estado
    sin romper el contrato (el retorno sigue siendo el estado de siempre).
  - Estado intacto cuando el LLM omite "detecciones"; contrato legacy (sin
    detections_out) usa el prompt original.
  - _clean_detections: fail-safe estricto ante basura del LLM.
  - core/meeting.py _handle_detections: gating por modo, flags por clase, dedup
    por key, presupuesto should_push, formateo de tarjetas por clase.
  - status()["cards"]: solo entregadas y sin feedback.
  - Round-trip de detections_json en meeting_insert/meeting_get.

No llama a ningún LLM: se mockea core.insights._chat / is_available (mismo
patrón que el resto de la suite de reuniones).
"""

import json
import time as _time

import pytest

import core.insights as insights
import core.proactive as proactive
from core.meeting import MeetingSession
from core.proactive import ProactiveGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATE_RESPONSE = {
    "temas": ["presupuesto"],
    "pendientes": [],
    "propuestas": [],
    "citas": [],
}

DETECTIONS_RESPONSE = {
    "preguntas_sin_responder": [{"pregunta": "¿Cuál es el precio final?", "time": "03:10"}],
    "compromisos": [{"texto": "te envío la propuesta mañana", "time": "05:02"}],
    "acuerdos_vagos": [{"texto": "cambiar de proveedor", "falta": "ambos", "time": None}],
}


@pytest.fixture
def fresh_gate(monkeypatch):
    """Gate proactivo limpio por test (el singleton PROACTIVE es de proceso)."""
    gate = ProactiveGate()
    monkeypatch.setattr(proactive, "PROACTIVE", gate)
    return gate


@pytest.fixture
def active_session():
    m = MeetingSession()
    m._active = True
    m._t0 = _time.monotonic() - 30.0
    return m


def mock_llm(monkeypatch, payload: dict, capture: dict = None):
    """Mockea _chat/is_available de core.insights. Si capture se pasa, guarda
    los messages de la última llamada en capture["messages"]."""

    def _fake_chat(messages, **kwargs):
        if capture is not None:
            capture["messages"] = messages
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


# ---------------------------------------------------------------------------
# update_state: segunda intención en la MISMA llamada
# ---------------------------------------------------------------------------

class TestUpdateStateDetections:
    def test_detections_parsed_and_separated_from_state(self, monkeypatch):
        payload = {**STATE_RESPONSE, "detecciones": DETECTIONS_RESPONSE}
        mock_llm(monkeypatch, payload)
        out = {}
        state = insights.update_state(insights.empty_state(), "Yo: hola", detections_out=out)
        # Contrato del estado intacto: mismas 4 claves, sin "detecciones" dentro
        assert set(state.keys()) == {"temas", "pendientes", "propuestas", "citas"}
        assert state["temas"] == ["presupuesto"]
        # Detecciones separadas, en el out-param
        assert out["preguntas_sin_responder"] == [
            {"pregunta": "¿Cuál es el precio final?", "time": "03:10"}]
        assert out["compromisos"] == [{"texto": "te envío la propuesta mañana", "time": "05:02"}]
        assert out["acuerdos_vagos"] == [
            {"texto": "cambiar de proveedor", "falta": "ambos", "time": None}]

    def test_state_intact_when_llm_omits_detecciones(self, monkeypatch):
        mock_llm(monkeypatch, STATE_RESPONSE)
        out = {"basura": "previa"}
        state = insights.update_state(insights.empty_state(), "Yo: hola", detections_out=out)
        assert state["temas"] == ["presupuesto"]
        # out-param reseteado a listas vacías (y sin la basura previa)
        assert out == insights.empty_detections()

    def test_legacy_contract_without_detections_out(self, monkeypatch):
        capture = {}
        mock_llm(monkeypatch, STATE_RESPONSE, capture)
        state = insights.update_state(insights.empty_state(), "Yo: hola")
        assert state["temas"] == ["presupuesto"]
        # Prompt original intacto: sin la segunda intención
        system = capture["messages"][0]["content"]
        assert "detecciones" not in system
        assert system == insights._INSIGHTS_SYSTEM

    def test_detection_block_and_ya_reportadas_in_prompt(self, monkeypatch):
        capture = {}
        mock_llm(monkeypatch, {**STATE_RESPONSE, "detecciones": {}}, capture)
        out = {}
        insights.update_state(insights.empty_state(), "Yo: hola",
                              detections_out=out,
                              ya_reportadas=["te envío la propuesta mañana"])
        system = capture["messages"][0]["content"]
        user = capture["messages"][1]["content"]
        assert "preguntas_sin_responder" in system
        assert "SEGUNDA TAREA" in system
        assert "YA_REPORTADAS" in user
        assert "te envío la propuesta mañana" in user

    def test_failure_leaves_detections_empty_and_state_previous(self, monkeypatch):
        def _boom(messages, **kwargs):
            raise insights.InsightsUnavailable("backend caído")
        monkeypatch.setattr(insights, "_chat", _boom)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        prev = {"temas": ["x"], "pendientes": [], "propuestas": [], "citas": []}
        out = {}
        state = insights.update_state(prev, "Yo: hola", detections_out=out)
        assert state is prev
        assert out == insights.empty_detections()


class TestCleanDetections:
    def test_garbage_is_filtered(self):
        raw = {
            "preguntas_sin_responder": [
                {"pregunta": "  "},          # vacía → fuera
                "no soy un dict",             # tipo raro → fuera
                {"pregunta": "¿y el plazo?"}  # válida, sin time → time None
            ],
            "compromisos": "no soy lista",
            "acuerdos_vagos": [{"texto": "definir alcance", "falta": "INVENTADA"}],
        }
        out = insights._clean_detections(raw)
        assert out["preguntas_sin_responder"] == [{"pregunta": "¿y el plazo?", "time": None}]
        assert out["compromisos"] == []
        # falta desconocida se normaliza a "ambos"
        assert out["acuerdos_vagos"][0]["falta"] == "ambos"

    def test_non_dict_returns_empty(self):
        assert insights._clean_detections(None) == insights.empty_detections()
        assert insights._clean_detections([1, 2]) == insights.empty_detections()


# ---------------------------------------------------------------------------
# meeting._handle_detections: gating, dedup, presupuesto, formateo
# ---------------------------------------------------------------------------

class TestHandleDetections:
    def test_silent_mode_enqueues_nothing(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        active_session._handle_detections(dict(DETECTIONS_RESPONSE))
        assert fresh_gate.queue_size() == 0
        assert active_session._detections == []

    def test_copilot_enqueues_with_class_formatting(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        # Presupuesto: 1 push no-pendiente / 5 min → solo entra la PRIMERA clase.
        # Para verificar el formateo de las 3 clases, se prueban una a una.
        casos = [
            ({"preguntas_sin_responder": [DETECTIONS_RESPONSE["preguntas_sin_responder"][0]]},
             "❓ Pregunta sin responder: ¿Cuál es el precio final?", "03:10"),
            ({"compromisos": [DETECTIONS_RESPONSE["compromisos"][0]]},
             "🤝 Compromiso: te envío la propuesta mañana", "05:02"),
            ({"acuerdos_vagos": [DETECTIONS_RESPONSE["acuerdos_vagos"][0]]},
             "⚠️ Acuerdo sin fecha/dueño: cambiar de proveedor", None),
        ]
        for det, expected_text, expected_detail in casos:
            gate = ProactiveGate()
            monkeypatch.setattr(proactive, "PROACTIVE", gate)
            m = MeetingSession()
            m._active = True
            m._t0 = _time.monotonic() - 10.0
            m._handle_detections(det)
            assert gate.queue_size() == 1
            card = gate.pop_deliverable(lull=True)
            assert card["tipo"] == "deteccion"
            assert card["texto"] == expected_text
            assert card.get("detail") == expected_detail
            assert card["key"].startswith("det-")

    def test_falta_variants_formatting(self):
        f = MeetingSession._format_detection_card
        assert f("acuerdos_vagos", {"texto": "x", "falta": "fecha"}).startswith("⚠️ Acuerdo sin fecha:")
        assert f("acuerdos_vagos", {"texto": "x", "falta": "responsable"}).startswith("⚠️ Acuerdo sin dueño:")
        assert f("acuerdos_vagos", {"texto": "x", "falta": "ambos"}).startswith("⚠️ Acuerdo sin fecha/dueño:")

    def test_class_flag_disables_class(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        monkeypatch.setenv("PROACTIVE_DETECT_PREGUNTAS", "false")
        active_session._handle_detections(
            {"preguntas_sin_responder": [{"pregunta": "¿precio?", "time": None}]})
        assert fresh_gate.queue_size() == 0
        assert active_session._detections == []

    def test_dedup_by_key_across_cycles(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        det = {"compromisos": [{"texto": "te envío la propuesta mañana", "time": None}]}
        active_session._handle_detections(det)
        # Mismo texto con distinta puntuación/mayúsculas → misma key → dedup
        det2 = {"compromisos": [{"texto": "Te envío la propuesta, mañana.", "time": None}]}
        # Liberar presupuesto para que el bloqueo (si lo hubiera) sea SOLO por dedup
        fresh_gate._last_nonpending_push_at = 0.0
        active_session._handle_detections(det2)
        assert fresh_gate.queue_size() == 1
        assert len(active_session._detections) == 1

    def test_budget_limits_to_one_push(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        active_session._handle_detections(dict(DETECTIONS_RESPONSE))
        # Las 3 clases traían detección, pero el presupuesto (~1 push no-pendiente
        # cada 5 min) solo deja pasar UNA; las otras NO se registran (pueden
        # resurgir en un ciclo posterior).
        assert fresh_gate.queue_size() == 1
        assert len(active_session._detections) == 1

    def test_inactive_session_ignores(self, monkeypatch, fresh_gate):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        m = MeetingSession()  # _active = False
        m._handle_detections(dict(DETECTIONS_RESPONSE))
        assert fresh_gate.queue_size() == 0


# ---------------------------------------------------------------------------
# status()["cards"]: entregadas y sin feedback
# ---------------------------------------------------------------------------

class TestStatusCards:
    def test_cards_only_after_delivery_and_until_feedback(self, monkeypatch, fresh_gate, active_session):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        det = {"compromisos": [{"texto": "te envío la propuesta mañana", "time": "05:02"}]}
        active_session._handle_detections(det)

        # Encolada pero NO entregada todavía → no aparece en status().cards
        assert active_session.status()["cards"] == []

        card = fresh_gate.pop_deliverable(lull=True)
        assert card is not None
        cards = active_session.status()["cards"]
        assert len(cards) == 1
        assert cards[0]["tipo"] == "deteccion"
        assert cards[0]["texto"] == "🤝 Compromiso: te envío la propuesta mañana"
        assert cards[0]["detail"] == "05:02"
        assert cards[0]["key"] == card["key"]

        # Feedback ✓ → la tarjeta sale de status().cards
        active_session.add_feedback(card["key"], "deteccion", card["texto"], 1)
        assert active_session.status()["cards"] == []


# ---------------------------------------------------------------------------
# Round-trip DB: detections_json
# ---------------------------------------------------------------------------

class TestDetectionsDbRoundTrip:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db = TranscriptionDB(db_path=str(tmp_path / "test_detections.db"))

    def test_meeting_insert_with_detections_json(self):
        detections = [
            {"key": "det-abc123", "clase": "compromisos", "base": "te envío la propuesta mañana",
             "texto": "🤝 Compromiso: te envío la propuesta mañana", "detail": "05:02",
             "t": 302.0, "time": "05:02"},
        ]
        meeting_id = self.db.meeting_insert(
            title="Reunión con detecciones",
            transcript="[05:02 Yo] te envío la propuesta mañana",
            segments_json=json.dumps([]),
            duration_seconds=400.0,
            detections_json=json.dumps(detections, ensure_ascii=False),
        )
        row = self.db.meeting_get(meeting_id)
        assert row["detections_json"] is not None
        assert json.loads(row["detections_json"]) == detections

    def test_meeting_insert_without_detections_defaults_none(self):
        meeting_id = self.db.meeting_insert(
            title="Sin detecciones", transcript="t",
            segments_json=json.dumps([]), duration_seconds=10.0,
        )
        assert self.db.meeting_get(meeting_id)["detections_json"] is None
