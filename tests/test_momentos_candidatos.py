"""Tests para la unidad 5.1 v2 'Marcado automático de momentos clave' (PLAN-MEJORAS).

Cubre:
  - update_state con momentos_out: candidatos parseados y SEPARADOS del estado,
    sin romper el contrato (el retorno sigue siendo el estado de siempre).
  - Kill-switch: sin momentos_out (o AUTO_HIGHLIGHTS_ENABLED=false en
    core/meeting.py), la tarea adicional NO se añade al prompt.
  - Convivencia con detections_out en la MISMA respuesta.
  - insights._clean_momentos: fail-safe estricto ante basura del LLM (tipos
    raros, "t" no parseable como mm:ss, cap a 2 por ventana).
  - core/meeting.py MeetingSession._handle_momentos_candidatos: gating por
    sesión activa, dedup vs highlights MANUALES (<2s, el manual manda) y vs
    otros autos ya registrados (<5s).
  - core/meeting.py _run_insight_update: pasa momentos_out=None con el flag
    apagado (cero tokens extra) y una lista con el flag encendido (default).
  - Invariante F12: stop() SOLO pasa highlights MANUALES a generate_minutes
    (los autos jamás entran al acta), aunque haya candidatos automáticos.
  - Persistencia: highlights_json combina manuales + autos con su "source".
  - Retrocompat: web/blueprints/meetings._normalize_highlights completa
    "source": "manual" en entradas viejas que no lo tienen.
  - Guardián de no-truncado: max_tokens=1800 y una respuesta sintética con
    3 detecciones + 2 momentos + estado completo parsea sin perder datos.

No llama a ningún LLM real: se mockea core.insights._chat / is_available
(mismo patrón que tests/test_detections.py).
"""

import json
import time as _time

import pytest

import core.insights as insights
from core.meeting import MeetingSession
from db.database import TranscriptionDB


# ---------------------------------------------------------------------------
# Helpers (mismo patrón que tests/test_detections.py)
# ---------------------------------------------------------------------------

STATE_RESPONSE = {
    "temas": ["presupuesto"],
    "pendientes": [],
    "propuestas": [],
    "citas": [],
}

MOMENTOS_RESPONSE = [{"t": "00:41", "razon": "se acordó el presupuesto final"}]


def mock_llm(monkeypatch, payload, capture: dict = None):
    """Mockea _chat/is_available de core.insights. Si capture se pasa, guarda
    los messages y kwargs de la última llamada en capture."""

    def _fake_chat(messages, **kwargs):
        if capture is not None:
            capture["messages"] = messages
            capture["kwargs"] = kwargs
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


@pytest.fixture
def active_session():
    m = MeetingSession()
    m._active = True
    m._t0 = _time.monotonic() - 30.0
    return m


class _FakeSource:
    """Reemplaza MicSource/LoopbackSource: no toca hardware real."""

    def start(self, callback):
        return None

    def stop(self):
        return None


@pytest.fixture(autouse=True)
def fake_audio_sources(monkeypatch):
    monkeypatch.setattr("core.meeting.MicSource", _FakeSource)
    monkeypatch.setattr("core.meeting.LoopbackSource", _FakeSource)


@pytest.fixture
def fast_llm(monkeypatch):
    """Acta/capítulos instantáneos y vacíos por defecto (sin red, sin LLM real)."""
    monkeypatch.setattr(insights, "generate_minutes", lambda *a, **kw: {
        "resumen": "", "temas": [], "decisiones": [], "pendientes": [], "propuestas": [],
    })
    monkeypatch.setattr(insights, "generate_chapters", lambda *a, **kw: [])


# ---------------------------------------------------------------------------
# update_state: momentos_out como segunda (o tercera) intención NO invasiva
# ---------------------------------------------------------------------------

class TestUpdateStateMomentos:
    def test_momentos_parsed_and_separated_from_state(self, monkeypatch):
        payload = {**STATE_RESPONSE, "momentos_candidatos": MOMENTOS_RESPONSE}
        mock_llm(monkeypatch, payload)
        out = []
        state = insights.update_state(insights.empty_state(), "Yo: hola", momentos_out=out)
        # Contrato del estado intacto: mismas 4 claves, sin "momentos_candidatos" dentro
        assert set(state.keys()) == {"temas", "pendientes", "propuestas", "citas"}
        assert state["temas"] == ["presupuesto"]
        assert out == [{"time": "00:41", "razon": "se acordó el presupuesto final"}]

    def test_state_intact_when_llm_omits_momentos(self, monkeypatch):
        mock_llm(monkeypatch, STATE_RESPONSE)
        out = ["basura previa"]
        state = insights.update_state(insights.empty_state(), "Yo: hola", momentos_out=out)
        assert state["temas"] == ["presupuesto"]
        assert out == []  # se vació aunque el LLM no devolviera la clave

    def test_legacy_contract_without_momentos_out(self, monkeypatch):
        capture = {}
        mock_llm(monkeypatch, STATE_RESPONSE, capture)
        state = insights.update_state(insights.empty_state(), "Yo: hola")
        assert state["temas"] == ["presupuesto"]
        system = capture["messages"][0]["content"]
        assert "momentos_candidatos" not in system
        assert system == insights._INSIGHTS_SYSTEM

    def test_momentos_block_added_to_prompt_when_requested(self, monkeypatch):
        capture = {}
        mock_llm(monkeypatch, {**STATE_RESPONSE, "momentos_candidatos": []}, capture)
        out = []
        insights.update_state(insights.empty_state(), "Yo: hola", momentos_out=out)
        system = capture["messages"][0]["content"]
        assert "momentos_candidatos" in system
        assert "TAREA ADICIONAL" in system

    def test_momentos_and_detecciones_coexist_in_same_prompt(self, monkeypatch):
        capture = {}
        mock_llm(monkeypatch, {**STATE_RESPONSE, "detecciones": {}, "momentos_candidatos": []}, capture)
        det_out, mom_out = {}, []
        insights.update_state(insights.empty_state(), "Yo: hola",
                              detections_out=det_out, momentos_out=mom_out)
        system = capture["messages"][0]["content"]
        assert "SEGUNDA TAREA" in system       # detecciones (unidad 5.1)
        assert "TAREA ADICIONAL" in system     # momentos (unidad 5.1 v2)

    def test_failure_leaves_momentos_empty_and_state_previous(self, monkeypatch):
        def _boom(messages, **kwargs):
            raise insights.InsightsUnavailable("backend caído")
        monkeypatch.setattr(insights, "_chat", _boom)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        prev = {"temas": ["x"], "pendientes": [], "propuestas": [], "citas": []}
        out = []
        state = insights.update_state(prev, "Yo: hola", momentos_out=out)
        assert state is prev
        assert out == []


# ---------------------------------------------------------------------------
# insights._clean_momentos: fail-safe estricto
# ---------------------------------------------------------------------------

class TestCleanMomentos:
    def test_valid_item_passthrough(self):
        assert insights._clean_momentos([{"t": "01:05", "razon": "cifra clave"}]) == [
            {"time": "01:05", "razon": "cifra clave"}]

    def test_non_dict_item_discarded_silently(self):
        raw = ["no soy un dict", {"t": "00:10", "razon": "x"}]
        assert insights._clean_momentos(raw) == [{"time": "00:10", "razon": "x"}]

    def test_t_no_numerico_descarta_silenciosamente(self):
        assert insights._clean_momentos([{"t": "abc", "razon": "x"}]) == []

    def test_t_tipo_raro_descarta(self):
        assert insights._clean_momentos([{"t": 42, "razon": "x"}]) == []
        assert insights._clean_momentos([{"t": None, "razon": "x"}]) == []
        assert insights._clean_momentos([{"t": ["00:10"], "razon": "x"}]) == []

    def test_razon_vacia_descarta(self):
        assert insights._clean_momentos([{"t": "00:10", "razon": "  "}]) == []
        assert insights._clean_momentos([{"t": "00:10"}]) == []

    def test_cap_dos_por_ventana(self):
        raw = [{"t": f"00:0{i}", "razon": f"r{i}"} for i in range(5)]
        out = insights._clean_momentos(raw)
        assert len(out) == 2
        assert out == [{"time": "00:00", "razon": "r0"}, {"time": "00:01", "razon": "r1"}]

    def test_invalido_no_roba_cupo_a_valido_posterior(self):
        raw = [{"t": "abc", "razon": "invalido"}, {"t": "00:01", "razon": "valido 1"},
               {"t": "00:02", "razon": "valido 2"}]
        out = insights._clean_momentos(raw)
        assert out == [{"time": "00:01", "razon": "valido 1"}, {"time": "00:02", "razon": "valido 2"}]

    def test_non_list_returns_empty(self):
        assert insights._clean_momentos(None) == []
        assert insights._clean_momentos("no soy lista") == []
        assert insights._clean_momentos({"t": "00:10", "razon": "x"}) == []


# ---------------------------------------------------------------------------
# core/meeting.py: MeetingSession._handle_momentos_candidatos
# ---------------------------------------------------------------------------

class TestHandleMomentosCandidatos:
    def test_inactive_session_ignores(self):
        m = MeetingSession()
        m._handle_momentos_candidatos([{"time": "00:10", "razon": "x"}])
        assert m._auto_highlights == []

    def test_active_session_accumulates_with_source_auto(self, active_session):
        active_session._handle_momentos_candidatos([{"time": "00:10", "razon": "cifra clave"}])
        assert len(active_session._auto_highlights) == 1
        item = active_session._auto_highlights[0]
        assert item["source"] == "auto"
        assert item["razon"] == "cifra clave"
        assert item["time"] == "00:10"
        assert item["t"] == 10.0

    def test_invalid_items_discarded_silently(self, active_session):
        active_session._handle_momentos_candidatos([
            {"time": "", "razon": "sin tiempo"},
            {"time": "00:05", "razon": ""},
            {"time": "abc", "razon": "tiempo invalido"},
            "no soy un dict",
            42,
        ])
        assert active_session._auto_highlights == []

    def test_dedup_against_manual_highlight_within_2s(self, active_session):
        active_session._highlights.append({"t": 10.0, "time": "00:10", "source": "manual"})
        active_session._handle_momentos_candidatos([{"time": "00:11", "razon": "cerca del manual"}])
        assert active_session._auto_highlights == []  # el manual manda

    def test_no_dedup_against_manual_beyond_2s(self, active_session):
        active_session._highlights.append({"t": 10.0, "time": "00:10", "source": "manual"})
        active_session._handle_momentos_candidatos([{"time": "00:20", "razon": "lejos del manual"}])
        assert len(active_session._auto_highlights) == 1

    def test_dedup_against_existing_auto_within_5s(self, active_session):
        active_session._handle_momentos_candidatos([{"time": "00:40", "razon": "primero"}])
        active_session._handle_momentos_candidatos([{"time": "00:43", "razon": "repetido"}])
        assert len(active_session._auto_highlights) == 1

    def test_no_dedup_against_existing_auto_beyond_5s(self, active_session):
        active_session._handle_momentos_candidatos([{"time": "00:40", "razon": "primero"}])
        active_session._handle_momentos_candidatos([{"time": "00:50", "razon": "distinto"}])
        assert len(active_session._auto_highlights) == 2


# ---------------------------------------------------------------------------
# core/meeting.py: kill-switch AUTO_HIGHLIGHTS_ENABLED en _run_insight_update
# ---------------------------------------------------------------------------

class TestAutoHighlightsKillSwitch:
    def test_disabled_passes_none_to_update_state(self, monkeypatch):
        monkeypatch.setenv("AUTO_HIGHLIGHTS_ENABLED", "false")
        captured = {}

        def _fake_update_state(state, delta, *, detections_out=None, ya_reportadas=None,
                               momentos_out=None):
            captured["momentos_out"] = momentos_out
            if detections_out is not None:
                detections_out.update(insights.empty_detections())
            return state

        monkeypatch.setattr(insights, "update_state", _fake_update_state)
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen = m._session_gen
        m._insight_running = True
        m._run_insight_update(m._store_to_plain(), "[00:01] Yo: hola", gen)
        assert captured["momentos_out"] is None
        assert m._auto_highlights == []

    def test_enabled_by_default_passes_list_and_merges_candidates(self, monkeypatch):
        monkeypatch.delenv("AUTO_HIGHLIGHTS_ENABLED", raising=False)  # default "true"

        def _fake_update_state(state, delta, *, detections_out=None, ya_reportadas=None,
                               momentos_out=None):
            if detections_out is not None:
                detections_out.update(insights.empty_detections())
            if momentos_out is not None:
                momentos_out.append({"time": "00:15", "razon": "cifra clave"})
            return state

        monkeypatch.setattr(insights, "update_state", _fake_update_state)
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen = m._session_gen
        m._insight_running = True
        m._run_insight_update(m._store_to_plain(), "[00:01] Yo: hola", gen)
        assert len(m._auto_highlights) == 1
        assert m._auto_highlights[0]["source"] == "auto"

    def test_stale_generation_discards_momentos_too(self, monkeypatch):
        """Mismo invariante F1 que el resto del daemon: una respuesta tardía de
        una reunión YA cerrada/reemplazada no debe colar candidatos en la B."""
        monkeypatch.delenv("AUTO_HIGHLIGHTS_ENABLED", raising=False)

        def _fake_update_state(state, delta, *, detections_out=None, ya_reportadas=None,
                               momentos_out=None):
            if detections_out is not None:
                detections_out.update(insights.empty_detections())
            if momentos_out is not None:
                momentos_out.append({"time": "00:15", "razon": "cifra colada de A"})
            return state

        monkeypatch.setattr(insights, "update_state", _fake_update_state)
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen_a = m._session_gen
        m._session_gen += 1  # reunión B arrancó mientras el daemon de A seguía en vuelo
        m._insight_running = True
        m._run_insight_update(m._store_to_plain(), "[00:01] Yo: hola", gen_a)
        assert m._auto_highlights == []
        assert m._insight_running is False


# ---------------------------------------------------------------------------
# Invariante F12: los autos JAMÁS entran al acta / gate anti-alucinación
# ---------------------------------------------------------------------------

class TestAutoHighlightsNeverEnterActaGate:
    def test_stop_passes_only_manual_highlights_to_generate_minutes(self, monkeypatch, fast_llm):
        captured = {}

        def _capture_generate_minutes(transcript, insights_arg=None, reasoning=False, *,
                                      highlights=None, notes=None, segments=None, template=None):
            captured["highlights"] = highlights
            return {"resumen": "", "temas": [], "decisiones": [], "pendientes": [], "propuestas": []}

        monkeypatch.setattr(insights, "generate_minutes", _capture_generate_minutes)
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 10.0
        m._segments = [{"t": 0.0, "speaker": "Yo", "text": "hola"}]
        # SIN highlights manuales, pero CON candidatos automáticos.
        m._auto_highlights.append({"t": 12.0, "time": "00:12", "razon": "cifra clave", "source": "auto"})

        res = m.stop()

        assert captured["highlights"] == []  # los autos NUNCA llegan aquí
        assert "momentos_destacados" not in res["minutes"]


# ---------------------------------------------------------------------------
# Persistencia: highlights_json combina manuales + autos con su "source"
# ---------------------------------------------------------------------------

class TestStopPersistsCombinedHighlights:
    def test_manual_and_auto_combined_with_source_in_highlights_json(self, monkeypatch, fast_llm, tmp_path):
        monkeypatch.setenv("SAVE_HISTORY", "true")
        db = TranscriptionDB(db_path=str(tmp_path / "test_auto_highlights.db"))
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 10.0
        m._db = db
        m._segments = [{"t": 0.0, "speaker": "Yo", "text": "hola"}]
        m.add_highlight()  # manual: source="manual"
        m._auto_highlights.append({"t": 40.0, "time": "00:40", "razon": "cifra clave", "source": "auto"})

        res = m.stop()

        assert res["saved"] is True
        row = db.meeting_get(res["meeting_id"])
        hl = json.loads(row["highlights_json"])
        assert len(hl) == 2
        assert hl[0]["source"] == "manual"
        assert hl[1]["source"] == "auto"
        assert hl[1]["razon"] == "cifra clave"

    def test_only_auto_highlights_still_persist(self, monkeypatch, fast_llm, tmp_path):
        """Sin highlights manuales, los autos solos igual se persisten (no dependen
        del gate F12, que solo aplica al acta)."""
        monkeypatch.setenv("SAVE_HISTORY", "true")
        db = TranscriptionDB(db_path=str(tmp_path / "test_auto_only.db"))
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 10.0
        m._db = db
        m._segments = [{"t": 0.0, "speaker": "Yo", "text": "hola"}]
        m._auto_highlights.append({"t": 5.0, "time": "00:05", "razon": "x", "source": "auto"})

        res = m.stop()

        row = db.meeting_get(res["meeting_id"])
        hl = json.loads(row["highlights_json"])
        assert hl == [{"t": 5.0, "time": "00:05", "razon": "x", "source": "auto"}]

    def test_no_highlights_at_all_leaves_column_none(self, monkeypatch, fast_llm, tmp_path):
        monkeypatch.setenv("SAVE_HISTORY", "true")
        db = TranscriptionDB(db_path=str(tmp_path / "test_no_highlights.db"))
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 10.0
        m._db = db
        m._segments = [{"t": 0.0, "speaker": "Yo", "text": "hola"}]

        res = m.stop()

        row = db.meeting_get(res["meeting_id"])
        assert row["highlights_json"] is None


# ---------------------------------------------------------------------------
# Retrocompat: web/blueprints/meetings._normalize_highlights
# ---------------------------------------------------------------------------

class TestNormalizeHighlightsRetrocompat:
    def test_legacy_entries_without_source_default_to_manual(self):
        from web.blueprints.meetings import _normalize_highlights
        raw = [{"t": 12.3, "time": "00:12"}]  # entrada vieja, pre-unidad 5.1 v2
        assert _normalize_highlights(raw) == [{"t": 12.3, "time": "00:12", "source": "manual"}]

    def test_explicit_source_preserved(self):
        from web.blueprints.meetings import _normalize_highlights
        raw = [{"t": 40.0, "time": "00:40", "razon": "x", "source": "auto"}]
        assert _normalize_highlights(raw) == raw

    def test_mixed_legacy_and_new_entries(self):
        from web.blueprints.meetings import _normalize_highlights
        raw = [{"t": 1.0, "time": "00:01"}, {"t": 2.0, "time": "00:02", "source": "auto", "razon": "x"}]
        out = _normalize_highlights(raw)
        assert out[0]["source"] == "manual"
        assert out[1]["source"] == "auto"

    def test_non_list_returns_empty(self):
        from web.blueprints.meetings import _normalize_highlights
        assert _normalize_highlights(None) == []
        assert _normalize_highlights("no soy lista") == []

    def test_non_dict_items_discarded(self):
        from web.blueprints.meetings import _normalize_highlights
        assert _normalize_highlights([{"t": 1.0, "time": "00:01"}, "basura", 42]) == [
            {"t": 1.0, "time": "00:01", "source": "manual"}]


# ---------------------------------------------------------------------------
# Guardián de no-truncado (protección de la llamada live, objeción A3)
# ---------------------------------------------------------------------------

class TestNoTruncado:
    def test_full_shape_response_parses_without_truncation(self, monkeypatch):
        """Respuesta sintética con estado completo + 3 detecciones + 2 momentos
        candidatos en la MISMA llamada: el parseo no debe fallar ni perder datos,
        y la llamada debe pedir max_tokens=1800 (antes 1200)."""
        full_payload = {
            "temas": [f"tema {i}" for i in range(8)],
            "pendientes": [
                {"texto": f"pendiente {i}", "responsable": "Yo", "fecha": None, "hora": None}
                for i in range(5)
            ],
            "propuestas": [{"texto": f"propuesta {i}", "confianza": "media"} for i in range(5)],
            "citas": [{"texto": "seguimiento", "fecha": "2026-08-01", "hora": "10:00"}],
            "detecciones": {
                "preguntas_sin_responder": [
                    {"pregunta": "¿cuál es el precio final del contrato anual?", "time": "05:10"}],
                "compromisos": [
                    {"texto": "te envío la propuesta actualizada mañana antes de las 10", "time": "06:02"}],
                "acuerdos_vagos": [
                    {"texto": "cambiar de proveedor logístico este trimestre", "falta": "ambos",
                     "time": "07:15"}],
            },
            "momentos_candidatos": [
                {"t": "05:10", "razon": "se preguntó el precio final del contrato"},
                {"t": "06:02", "razon": "compromiso de enviar la propuesta mañana"},
            ],
        }
        captured_kwargs = {}

        def _fake_chat(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return json.dumps(full_payload, ensure_ascii=False)

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)

        det_out, mom_out = {}, []
        state = insights.update_state(
            insights.empty_state(), "Yo: " + ("hola " * 60),
            detections_out=det_out, momentos_out=mom_out,
        )

        # Guardián real: si alguien vuelve a bajar max_tokens, este assert lo detecta.
        assert captured_kwargs.get("max_tokens") == 1800

        assert len(state["temas"]) == 8
        assert len(state["pendientes"]) == 5
        assert len(state["propuestas"]) == 5
        assert len(state["citas"]) == 1

        assert len(det_out["preguntas_sin_responder"]) == 1
        assert len(det_out["compromisos"]) == 1
        assert len(det_out["acuerdos_vagos"]) == 1

        assert len(mom_out) == 2
        assert mom_out[0] == {"time": "05:10", "razon": "se preguntó el precio final del contrato"}
        assert mom_out[1] == {"time": "06:02", "razon": "compromiso de enviar la propuesta mañana"}
