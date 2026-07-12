"""Tests para la unidad 5.2 'Memoria cruzada en vivo' (Ola 5).

Cubre:
  - db.meetings_search proyecta bm25 como "score" (campo aditivo: los llamadores
    viejos siguen viendo id/title/started_at/duration_seconds/snippet).
  - _cross_memory_check: tema presente en un acta pasada → tarjeta 'cruzada' con
    fecha dd/mm y texto de la decisión/pendiente; retrieval puro (cero LLM).
  - Gate de overlap de tokens: tema sin relación real NO emite.
  - Dedup por reunión pasada: la segunda consolidación con el mismo match no re-emite.
  - Exclusión de la reunión activa (por started_at) y de reuniones sin acta.
  - Kill-switch PROACTIVE_DETECT_CRUZADA=false → no corre (ni toca la DB).
  - Replay sintético: ciclo consolidación (_run_consolidation) → tarjeta en la
    cola de PROACTIVE con tipo 'cruzada', sin audio real y con el LLM mockeado.

No llama a ningún LLM: la consolidación se mockea (mismo patrón que
test_detections.py); la memoria cruzada por diseño no usa LLM.
"""

import json
import time as _time

import pytest

import core.insights as insights
import core.proactive as proactive
from core.meeting import MeetingSession
from core.proactive import ProactiveGate
from db.database import TranscriptionDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minutes(decisiones=None, pendientes=None, resumen=""):
    return json.dumps({
        "resumen": resumen,
        "temas": [],
        "decisiones": decisiones or [],
        "pendientes": pendientes or [],
        "propuestas": [],
    }, ensure_ascii=False)


@pytest.fixture
def db(tmp_path):
    """DB temporal sembrada con reuniones pasadas (2 con acta, 1 sin acta)."""
    db = TranscriptionDB(db_path=str(tmp_path / "test_cross_memory.db"))
    # Reunión 1: acta con decisión sobre presupuesto de marketing (dict {texto,t}).
    db.meeting_insert(
        title="Reunión 2026-06-12",
        transcript="[00:10 Yo] hablemos del presupuesto de marketing digital",
        segments_json=json.dumps([]),
        duration_seconds=600.0,
        started_at="2026-06-12 10:00:00",
        minutes_json=_minutes(
            decisiones=[{"texto": "aprobar el presupuesto de marketing digital del Q3", "t": "05:10"}],
            pendientes=["enviar el desglose del presupuesto de marketing al equipo"],
        ),
    )
    # Reunión 2: acta con bullets str planos (formato viejo), tema distinto.
    db.meeting_insert(
        title="Reunión 2026-06-20",
        transcript="[00:05 Ellos] revisemos la contratación del backend developer",
        segments_json=json.dumps([]),
        duration_seconds=300.0,
        started_at="2026-06-20 09:00:00",
        minutes_json=_minutes(
            decisiones=["abrir la vacante de backend developer senior"],
        ),
    )
    # Reunión 3: SIN acta (minutes_json NULL) — debe excluirse siempre.
    db.meeting_insert(
        title="Reunión sin acta",
        transcript="presupuesto de marketing digital otra vez",
        segments_json=json.dumps([]),
        duration_seconds=100.0,
        started_at="2026-06-25 15:00:00",
    )
    return db


@pytest.fixture
def fresh_gate(monkeypatch):
    gate = ProactiveGate()
    monkeypatch.setattr(proactive, "PROACTIVE", gate)
    return gate


@pytest.fixture
def session(db, monkeypatch):
    """Sesión activa sintética con la DB temporal inyectada y modo copilot."""
    monkeypatch.setenv("PROACTIVE_MODE", "copilot")
    m = MeetingSession()
    m._active = True
    m._t0 = _time.monotonic() - 30.0
    m._started_at = "2026-07-03 11:00:00"
    m._db = db
    return m


def set_temas(m, temas):
    m._insights["temas"] = [{"id": i + 1, "text": t} for i, t in enumerate(temas)]


# ---------------------------------------------------------------------------
# meetings_search: score proyectado, contrato viejo intacto
# ---------------------------------------------------------------------------

class TestMeetingsSearchScore:
    def test_score_present_and_legacy_fields_intact(self, db):
        if not db._fts_enabled:
            pytest.skip("FTS5 no disponible en este sqlite")
        results = db.meetings_search("presupuesto marketing", match="or")
        assert results
        row = results[0]
        # Campos del contrato viejo (llamadores existentes: dashboard, asistente, MCP)
        for key in ("id", "title", "started_at", "duration_seconds", "snippet"):
            assert key in row
        # Campo nuevo aditivo: bm25 (negativo para todo match real)
        assert "score" in row
        assert isinstance(row["score"], float)
        assert row["score"] < 0

    def test_empty_query_still_returns_empty(self, db):
        assert db.meetings_search("") == []


# ---------------------------------------------------------------------------
# _cross_memory_check: emisión, gate de overlap, dedup, exclusiones, flag
# ---------------------------------------------------------------------------

class TestCrossMemoryCheck:
    def test_matching_topic_emits_card_with_date_and_text(self, session, fresh_gate):
        set_temas(session, ["presupuesto de marketing digital"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 1
        card = fresh_gate.pop_deliverable(lull=True)
        assert card["tipo"] == "cruzada"
        assert card["key"].startswith("cruz-")
        assert card["texto"].startswith("El 12/06 se acordó: ")
        assert "presupuesto de marketing digital" in card["texto"]

    def test_unrelated_topic_does_not_emit(self, session, fresh_gate):
        set_temas(session, ["vacaciones del equipo en agosto"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0
        assert session._cross_emitted == set()

    def test_single_token_overlap_is_gated(self, session, fresh_gate):
        # "presupuesto" solo comparte 1 token con el acta → por debajo del
        # umbral conservador (≥2) → no emite aunque FTS sí matchee.
        set_temas(session, ["presupuesto anual consolidado"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0

    def test_dedup_second_consolidation_does_not_reemit(self, session, fresh_gate):
        set_temas(session, ["presupuesto de marketing digital"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 1
        assert len(session._cross_emitted) == 1
        # Liberar el presupuesto para aislar el dedup del gate de budget
        fresh_gate._last_nonpending_push_at = 0.0
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 1
        assert len(session._cross_emitted) == 1

    def test_active_meeting_excluded(self, session, fresh_gate, db):
        # Una fila en DB con el MISMO started_at que la sesión activa y match
        # perfecto: debe ignorarse (exclusión de la reunión activa).
        db.meeting_insert(
            title="Yo misma (activa)",
            transcript="tema urgente de licencias de software corporativo",
            segments_json=json.dumps([]),
            duration_seconds=10.0,
            started_at=session._started_at,
            minutes_json=_minutes(decisiones=["renovar licencias de software corporativo"]),
        )
        set_temas(session, ["licencias de software corporativo"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0

    def test_meeting_without_minutes_excluded(self, session, fresh_gate):
        # La reunión 3 tiene el texto en el transcript (FTS matchea) pero NO
        # tiene acta → jamás produce tarjeta por sí sola. Con temas que solo
        # matchean esa reunión vía transcript, no debe salir nada... el acta
        # de la reunión 1 SÍ matchea, así que probamos con la 2 borrada y la 1
        # sin overlap: usamos un tema que solo vive en el transcript de la 3.
        set_temas(session, ["presupuesto de marketing digital"])
        # Borrar la reunión 1 (la única con acta que matchea) → solo queda la
        # sin-acta como candidata FTS → no emite.
        session._db.meeting_delete(1)
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0

    def test_flag_off_disables_and_skips_db(self, session, fresh_gate, monkeypatch):
        monkeypatch.setenv("PROACTIVE_DETECT_CRUZADA", "false")
        # Si tocara la DB, esto explotaría: el flag corta antes.
        monkeypatch.setattr(session._db, "meetings_search",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debía buscar")))
        set_temas(session, ["presupuesto de marketing digital"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0

    def test_silent_mode_does_not_emit(self, session, fresh_gate, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        set_temas(session, ["presupuesto de marketing digital"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0

    def test_budget_exhausted_does_not_register_dedup(self, session, fresh_gate):
        # Presupuesto recién consumido → no emite NI registra: puede re-entrar
        # en la próxima consolidación (mejor tarde que perdida).
        fresh_gate.mark_pushed("deteccion")
        set_temas(session, ["presupuesto de marketing digital"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 0
        assert session._cross_emitted == set()

    def test_str_plain_minutes_format_tolerated(self, session, fresh_gate):
        # La reunión 2 tiene decisiones como str planos (formato viejo).
        set_temas(session, ["contratar backend developer senior"])
        session._cross_memory_check()
        assert fresh_gate.queue_size() == 1
        card = fresh_gate.pop_deliverable(lull=True)
        assert card["texto"] == "El 20/06 se acordó: abrir la vacante de backend developer senior"


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_fmt_ddmm(self):
        assert MeetingSession._fmt_ddmm("2026-06-12 10:00:00") == "12/06"
        assert MeetingSession._fmt_ddmm("2026-06-12") == "12/06"
        assert MeetingSession._fmt_ddmm(None) == ""
        assert MeetingSession._fmt_ddmm("basura") == ""

    def test_minutes_past_items_both_formats(self):
        items = MeetingSession._minutes_past_items({
            "decisiones": [{"texto": "a", "t": "01:00"}, "b", 42, {"otro": "x"}],
            "pendientes": ["c", {"texto": "  "}],
        })
        assert items == ["a", "b", "c"]
        assert MeetingSession._minutes_past_items(None) == []
        assert MeetingSession._minutes_past_items({"decisiones": "no-lista"}) == []


# ---------------------------------------------------------------------------
# Replay sintético: ciclo consolidación → enqueue en PROACTIVE
# ---------------------------------------------------------------------------

class TestConsolidationReplay:
    def test_consolidation_cycle_enqueues_cruzada(self, session, fresh_gate, monkeypatch):
        """Simula el ciclo real: _run_consolidation (LLM mockeado, sin audio)
        → memoria cruzada corre al final → tarjeta 'cruzada' en la cola."""
        set_temas(session, ["presupuesto de marketing digital"])
        plain = session._store_to_plain()
        monkeypatch.setattr(insights, "consolidate", lambda transcript, prev: plain)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        monkeypatch.setattr(insights, "last_error", lambda: None)

        session._insight_running = True  # como lo deja _maybe_consolidate
        session._run_consolidation(session._session_gen)

        assert session._insight_running is False  # la consolidación cerró bien
        assert fresh_gate.queue_size() == 1
        card = fresh_gate.pop_deliverable(lull=True)
        assert card["tipo"] == "cruzada"
        assert card["texto"].startswith("El 12/06 se acordó: ")

    def test_cross_memory_failure_never_breaks_consolidation(self, session, fresh_gate, monkeypatch):
        set_temas(session, ["presupuesto de marketing digital"])
        plain = session._store_to_plain()
        monkeypatch.setattr(insights, "consolidate", lambda transcript, prev: plain)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        monkeypatch.setattr(insights, "last_error", lambda: None)
        monkeypatch.setattr(MeetingSession, "_cross_memory_check",
                            lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        session._insight_running = True
        session._run_consolidation(session._session_gen)  # no debe lanzar
        assert session._insight_running is False
        assert fresh_gate.queue_size() == 0
