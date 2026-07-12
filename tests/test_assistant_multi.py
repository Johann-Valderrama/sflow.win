"""Tests de la unidad 5.3 'chat de memoria CROSS-reunión + entregables' (core/assistant.py).

Cubre la resolución de candidatos (ids > query, cap de 12 por recencia, sin acta
excluida), el ensamblado del contexto con presupuesto (las actas viejas quedan
fuera cuando no caben y se declaran excluidas; el contexto nunca supera el
budget), y el contrato de ``answer_multi`` (system prompt con fechas + regla de
citar, plantillas de entregable, validación de errores). Sin red ni LLM real:
``insights.chat_memory`` se monkeypatchea (mismo patrón que test_assistant_batch.py).
"""
import json

import pytest

from core import assistant as _assistant_mod
from core.assistant import (
    DELIVERABLE_TEMPLATES,
    MULTI_SYSTEM,
    _assemble_multi_context,
    _resolve_multi_candidates,
    answer_multi,
)
import core.insights as _insights_mod
from db.database import TranscriptionDB


def _insert_meeting(db, title: str, transcript: str = "", minutes: dict = None,
                     started_at: str = None) -> int:
    minutes_json = json.dumps(minutes) if minutes is not None else None
    return db.meeting_insert(
        title=title,
        transcript=transcript,
        segments_json="[]",
        duration_seconds=60.0,
        started_at=started_at or "2026-06-10 10:00:00",
        insights_json=None,
        minutes_json=minutes_json,
    )


@pytest.fixture()
def db(tmp_path):
    return TranscriptionDB(db_path=str(tmp_path / "test_multi.db"))


# ---------------------------------------------------------------------------
# _resolve_multi_candidates
# ---------------------------------------------------------------------------

class TestResolveCandidatesPriority:
    def test_ids_win_over_fts_query(self, db, monkeypatch):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "resumen a"},
                                started_at="2026-06-01 10:00:00")
        search_calls = []
        original = db.meetings_search

        def counting_search(*args, **kwargs):
            search_calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "meetings_search", counting_search)

        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[id_a], fts_query="algo")
        assert len(candidates) == 1
        assert candidates[0]["id"] == id_a
        assert search_calls == []  # FTS nunca se llamó: ids ganan siempre

    def test_no_ids_no_query_returns_empty(self, db):
        candidates, excluded = _resolve_multi_candidates(db)
        assert candidates == []
        assert excluded == []


class TestResolveCandidatesExclusions:
    def test_missing_id_excluded_as_not_found(self, db):
        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[999999])
        assert candidates == []
        assert excluded == [{"id": 999999, "titulo": "", "motivo": "no encontrada"}]

    def test_meeting_without_minutes_excluded_as_sin_acta(self, db):
        id_no_acta = _insert_meeting(db, "Sin acta", minutes=None, started_at="2026-06-01 10:00:00")
        id_con_acta = _insert_meeting(db, "Con acta", minutes={"resumen": "hay resumen"},
                                       started_at="2026-06-02 10:00:00")
        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[id_no_acta, id_con_acta])
        candidate_ids = [c["id"] for c in candidates]
        assert id_con_acta in candidate_ids
        assert id_no_acta not in candidate_ids
        motivos = {e["id"]: e["motivo"] for e in excluded}
        assert motivos[id_no_acta] == "sin acta"

    def test_empty_minutes_dict_treated_as_sin_acta(self, db):
        """minutes_json == '{}' (presente pero vacío) tampoco cuenta como acta real."""
        id_empty = _insert_meeting(db, "Acta vacía", minutes={}, started_at="2026-06-01 10:00:00")
        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[id_empty])
        assert candidates == []
        assert excluded[0]["motivo"] == "sin acta"

    def test_duplicate_ids_deduplicated(self, db):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "a"}, started_at="2026-06-01 10:00:00")
        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[id_a, id_a, id_a])
        assert len(candidates) == 1

    def test_non_integer_ids_ignored(self, db):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "a"}, started_at="2026-06-01 10:00:00")
        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=[id_a, "no-es-int", None])
        assert len(candidates) == 1
        assert excluded == []


class TestResolveCandidatesCap:
    def test_more_than_12_capped_by_recency_and_excluded_declared(self, db):
        ids = []
        for i in range(14):
            mid = _insert_meeting(
                db, f"Reunión {i}", minutes={"resumen": f"resumen {i}"},
                started_at=f"2026-06-{i + 1:02d} 10:00:00",
            )
            ids.append(mid)

        candidates, excluded = _resolve_multi_candidates(db, meeting_ids=ids)
        assert len(candidates) == 12

        # Recencia: los 12 más RECIENTES (últimos insertados, fechas más altas) se
        # quedan; los 2 más viejos (índices 0 y 1, fechas más bajas) se excluyen.
        candidate_ids = {c["id"] for c in candidates}
        assert ids[0] not in candidate_ids
        assert ids[1] not in candidate_ids
        assert ids[13] in candidate_ids
        assert ids[12] in candidate_ids

        cap_excluded = [e for e in excluded if e["motivo"] == "cap de recencia (máx 12)"]
        assert len(cap_excluded) == 2
        cap_excluded_ids = {e["id"] for e in cap_excluded}
        assert cap_excluded_ids == {ids[0], ids[1]}

    def test_candidates_ordered_most_recent_first(self, db):
        id_old = _insert_meeting(db, "Vieja", minutes={"resumen": "vieja"}, started_at="2026-01-01 10:00:00")
        id_new = _insert_meeting(db, "Nueva", minutes={"resumen": "nueva"}, started_at="2026-06-01 10:00:00")
        candidates, _ = _resolve_multi_candidates(db, meeting_ids=[id_old, id_new])
        assert [c["id"] for c in candidates] == [id_new, id_old]


class TestResolveCandidatesFts:
    def test_fts_query_resolves_via_search(self, db):
        id_a = _insert_meeting(db, "Reunión presupuesto",
                                transcript="Hablamos del presupuesto anual en detalle.",
                                minutes={"resumen": "Presupuesto aprobado"},
                                started_at="2026-06-01 10:00:00")
        _insert_meeting(db, "Reunión entregas", transcript="Revisamos entregas.",
                         minutes={"resumen": "Entregas revisadas"}, started_at="2026-06-02 10:00:00")
        candidates, _ = _resolve_multi_candidates(db, fts_query="presupuesto")
        assert any(c["id"] == id_a for c in candidates)


# ---------------------------------------------------------------------------
# _assemble_multi_context — presupuesto
# ---------------------------------------------------------------------------

class TestAssembleContextBudget:
    def test_context_never_exceeds_budget(self):
        candidates = [
            {"id": 1, "title": "Reciente", "started_at": "2026-06-10",
             "minutes": {"resumen": "R" * 3000}},
            {"id": 2, "title": "Vieja", "started_at": "2026-06-01",
             "minutes": {"resumen": "V" * 3000}},
        ]
        ctx, included, excluded = _assemble_multi_context(candidates, budget=3500)
        assert len(ctx) <= 3500
        assert len(included) >= 1
        # La reunión que no cupo se declara excluida por presupuesto.
        assert any(e["motivo"] == "presupuesto" for e in excluded)

    def test_budget_too_small_for_any_meeting_excludes_all(self):
        """Si ni la más reciente cabe, el contexto queda solo con el header (sin
        forzar inclusión parcial) y TODAS las candidatas se declaran excluidas."""
        candidates = [
            {"id": 1, "title": "Reciente", "started_at": "2026-06-10",
             "minutes": {"resumen": "R" * 3000}},
        ]
        ctx, included, excluded = _assemble_multi_context(candidates, budget=200)
        assert len(ctx) <= 200
        assert included == []
        assert excluded[0]["motivo"] == "presupuesto"

    def test_recent_meeting_wins_over_old_when_budget_tight(self):
        candidates = [
            {"id": 10, "title": "Reciente", "started_at": "2026-06-10",
             "minutes": {"resumen": "R" * 3000}},
            {"id": 20, "title": "Vieja", "started_at": "2026-01-01",
             "minutes": {"resumen": "V" * 3000}},
        ]
        # Presupuesto justo para la primera (más reciente) y nada más.
        header = "=== ACTAS DE REUNIONES SELECCIONADAS ==="
        first_only, included, excluded = _assemble_multi_context(candidates, budget=len(header) + 3200)
        included_ids = [c["id"] for c in included]
        assert included_ids == [10]
        assert excluded[0]["id"] == 20
        assert excluded[0]["motivo"] == "presupuesto"

    def test_each_included_block_shows_date_title_and_id(self):
        candidates = [{"id": 42, "title": "Reunión de kickoff", "started_at": "2026-06-15",
                       "minutes": {"resumen": "Se lanzó el proyecto."}}]
        ctx, included, excluded = _assemble_multi_context(candidates, budget=80000)
        assert "[42]" in ctx
        assert "2026-06-15" in ctx
        assert "Reunión de kickoff" in ctx
        assert excluded == []

    def test_empty_candidates_returns_header_only(self):
        ctx, included, excluded = _assemble_multi_context([], budget=1000)
        assert included == []
        assert excluded == []
        assert "ACTAS DE REUNIONES" in ctx


# ---------------------------------------------------------------------------
# answer_multi — contrato, validación, system prompt
# ---------------------------------------------------------------------------

class TestAnswerMultiValidation:
    def test_empty_message_fails(self, db):
        result = answer_multi(db, "", meeting_ids=[1])
        assert result["ok"] is False

    def test_no_ids_no_query_fails(self, db):
        result = answer_multi(db, "hola")
        assert result["ok"] is False
        assert "meeting_ids" in result["error"] or "fts_query" in result["error"]

    def test_unknown_template_fails(self, db):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "a"}, started_at="2026-06-01 10:00:00")
        result = answer_multi(db, "hola", meeting_ids=[id_a], template="no-existe")
        assert result["ok"] is False


class TestAnswerMultiHappyPath:
    def test_happy_path_stub_llm(self, db, monkeypatch):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "resumen a"},
                                started_at="2026-06-01 10:00:00")
        id_b = _insert_meeting(db, "Reunión B", minutes={"resumen": "resumen b"},
                                started_at="2026-06-05 10:00:00")

        captured = []

        def stub_chat_memory(messages, **kw):
            captured.clear()
            captured.extend(messages)
            return "[stub-multi]"

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_memory)

        result = answer_multi(db, "¿Qué se decidió?", meeting_ids=[id_a, id_b])
        assert result["ok"] is True
        assert result["answer"] == "[stub-multi]"
        ids_incluidos = {r["id"] for r in result["reuniones_incluidas"]}
        assert ids_incluidos == {id_a, id_b}
        assert result["excluidas"] == []
        assert result["template_usado"] is None

        system_msg = captured[0]["content"]
        assert system_msg.startswith(MULTI_SYSTEM[:30])
        # Regla de citar presente
        assert "Cita SIEMPRE" in system_msg
        # Fechas de las actas presentes en el contexto inyectado
        assert "2026-06-01" in system_msg
        assert "2026-06-05" in system_msg

    def test_template_extends_system_prompt(self, db, monkeypatch):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "resumen a"},
                                started_at="2026-06-01 10:00:00")
        captured = []

        def stub_chat_memory(messages, **kw):
            captured.clear()
            captured.extend(messages)
            return "[stub-email]"

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_memory)

        result = answer_multi(db, "Redacta el email", meeting_ids=[id_a],
                              template="email_seguimiento")
        assert result["ok"] is True
        assert result["template_usado"] == "email_seguimiento"
        assert "EMAIL DE SEGUIMIENTO" in captured[0]["content"]

    def test_fts_query_path_happy(self, db, monkeypatch):
        id_a = _insert_meeting(db, "Reunión presupuesto",
                                transcript="Hablamos largo del presupuesto anual.",
                                minutes={"resumen": "Presupuesto aprobado"},
                                started_at="2026-06-01 10:00:00")

        def stub_chat_memory(messages, **kw):
            return "[stub-fts]"

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_memory)

        result = answer_multi(db, "¿Qué se dijo del presupuesto?", fts_query="presupuesto")
        assert result["ok"] is True
        assert any(r["id"] == id_a for r in result["reuniones_incluidas"])

    def test_failsafe_insights_unavailable(self, db, monkeypatch):
        id_a = _insert_meeting(db, "Reunión A", minutes={"resumen": "a"}, started_at="2026-06-01 10:00:00")

        def stub_unavailable(messages, **kw):
            raise _insights_mod.InsightsUnavailable("caído")

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_unavailable)

        result = answer_multi(db, "hola", meeting_ids=[id_a])
        assert result["ok"] is False
        assert "error" in result


class TestDeliverableTemplatesShape:
    def test_three_templates_exist(self):
        assert set(DELIVERABLE_TEMPLATES.keys()) == {
            "email_seguimiento", "informe", "resumen_acuerdos",
        }

    def test_each_template_has_system_extra(self):
        for name, tpl in DELIVERABLE_TEMPLATES.items():
            assert tpl.get("system_extra")
            assert tpl.get("label")
