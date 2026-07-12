"""Tests del Asistente de reuniones — flujo BATCH (sobre DB persistida).

Reescrito desde los scripts huérfanos test_assistant.py y test_assistant_retrieval.py
(raíz) para la unidad 2.2 del PLAN-MEJORAS. Fusionados en un solo archivo porque
comparten fixtures (DB temporal + helper de inserción de reuniones) y protegen la
MISMA superficie: el chat BATCH de core/assistant.py (build_context, _format_acta,
_compact_index, _search_terms, _budget_chars, answer()). Distinto de
tests/test_assistant_live.py (que cubre solo el chat EN VIVO sobre una reunión
activa en RAM, sin DB) y de tests/test_cross_memory.py (retrieval de memoria
cruzada durante una reunión en curso, ruta distinta).
"""
import json
import os

import pytest

from core.assistant import (
    _budget_chars,
    _compact_index,
    _format_acta,
    _search_terms,
    build_context,
)
from core import assistant as _assistant_mod
import core.insights as _insights_mod
from db.database import TranscriptionDB


def _insert_meeting(db, title: str, transcript: str = "", minutes: dict = None,
                     started_at: str = None) -> int:
    minutes_json = json.dumps(minutes or {})
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
    return TranscriptionDB(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# meetings_index
# ---------------------------------------------------------------------------

class TestMeetingsIndex:
    def test_index_shape_and_order(self, db):
        id_a = _insert_meeting(db, "Reunión Alpha", "transcripción alpha",
                                minutes={"resumen": "Resumen alpha", "temas": ["alpha"]},
                                started_at="2026-06-01 10:00:00")
        id_b = _insert_meeting(db, "Reunión Beta", "transcripción beta",
                                minutes={"resumen": "Resumen beta", "temas": ["beta"]},
                                started_at="2026-06-02 10:00:00")

        index = db.meetings_index()
        assert isinstance(index, list)
        assert len(index) == 2

        ids_returned = [e["id"] for e in index]
        assert ids_returned[0] > ids_returned[1]  # orden DESC por id

        by_id = {e["id"]: e for e in index}
        assert by_id[id_a]["resumen"] == "Resumen alpha"
        assert by_id[id_b]["resumen"] == "Resumen beta"
        assert "started_at" in by_id[id_a]
        assert by_id[id_a]["title"] == "Reunión Alpha"


# ---------------------------------------------------------------------------
# _format_acta
# ---------------------------------------------------------------------------

class TestFormatActa:
    def test_full_minutes_render_all_sections(self):
        minutes_full = {
            "resumen": "Reunión sobre presupuesto.",
            "decisiones": ["Aprobar 50k"],
            "temas": ["presupuesto", "personal"],
            "pendientes": [{"texto": "Enviar informe", "responsable": "Ana", "fecha": "viernes", "hora": None}],
            "propuestas": ["Contratar auditor"],
            "citas": [{"texto": "Próxima reunión", "fecha": "2026-06-20", "hora": "10:00"}],
        }
        texto = _format_acta(minutes_full)
        assert "Reunión sobre presupuesto." in texto
        assert "Aprobar 50k" in texto
        assert "Enviar informe" in texto
        assert "Contratar auditor" in texto
        assert "presupuesto" in texto

    def test_empty_dict_does_not_raise(self):
        texto_vacio = _format_acta({})
        assert isinstance(texto_vacio, str)


# ---------------------------------------------------------------------------
# build_context — global, presupuesto chico, focus+transcript, orden de degradado
# ---------------------------------------------------------------------------

class TestBuildContextGlobal:
    def test_global_context_hits_fts(self, db):
        _insert_meeting(db, "Reunión presupuesto", "El presupuesto fue discutido en detalle.",
                         minutes={"resumen": "Presupuesto aprobado", "temas": ["presupuesto"]},
                         started_at="2026-06-03 10:00:00")
        _insert_meeting(db, "Reunión entrega", "Revisamos las entregas del proyecto.",
                         minutes={"resumen": "Entregas revisadas", "temas": ["entregas"]},
                         started_at="2026-06-04 10:00:00")
        _insert_meeting(db, "Demo sistema", "Demo del sistema de pagos y presupuesto.",
                         minutes={"resumen": "Demo completada", "temas": ["demo", "pagos"]},
                         started_at="2026-06-05 10:00:00")

        ctx, used = build_context(db, "presupuesto")
        assert isinstance(ctx, str)
        assert "ÍNDICE DE TUS REUNIONES" in ctx
        assert len(used) > 0

    def test_small_budget_respects_cap(self, db):
        _insert_meeting(db, "Reunión presupuesto", "El presupuesto fue discutido en detalle.",
                         minutes={"resumen": "Presupuesto aprobado", "temas": ["presupuesto"]},
                         started_at="2026-06-03 10:00:00")
        ctx_small, used_small = build_context(db, "presupuesto", budget=300)
        assert len(ctx_small) <= 300 * 1.2
        assert len(ctx_small) > 0

    def test_focus_meeting_includes_transcript_block(self, db):
        id_focus = _insert_meeting(db, "Reunión con transcript",
                                    transcript="Este es el transcript completo de la reunión.",
                                    minutes={"resumen": "Reunión con transcript"},
                                    started_at="2026-06-06 10:00:00")
        ctx5, used5 = build_context(db, "transcript", meeting_id=id_focus)
        assert "TRANSCRIPCIÓN (reunión en foco" in ctx5
        assert id_focus in used5


class TestBuildContextDegradationOrder:
    """ORDEN: el transcript se degrada ANTES de bajar K y de degradar el índice."""

    def test_transcript_degrades_first_index_summary_survives(self, db):
        TRANSCRIPT_LARGO = "X" * 2000
        RESUMEN_UNICO = "ResumenFTS-unico-9z7k"
        RESUMEN_FOCUS = "ResumenFocus-focus-abc"

        id_fts = _insert_meeting(
            db, "Reunión FTS relevante",
            transcript="breve",
            minutes={"resumen": RESUMEN_UNICO, "decisiones": ["decision-clave-fts"]},
            started_at="2026-06-10 10:00:00",
        )
        id_focus = _insert_meeting(
            db, "Reunión foco con transcript largo",
            transcript=TRANSCRIPT_LARGO,
            minutes={"resumen": RESUMEN_FOCUS},
            started_at="2026-06-11 10:00:00",
        )

        entries = db.meetings_index()
        idx_full = _compact_index(entries)
        fts_acta = _format_acta({"resumen": RESUMEN_UNICO, "decisiones": ["decision-clave-fts"]})
        focus_acta = _format_acta({"resumen": RESUMEN_FOCUS})

        base_size = (
            len(idx_full)
            + len(f"\n\n=== ACTA EN FOCO (reunión [{id_focus}] 2026-06-11) ===\n{focus_acta}")
            + len(f"\n\n=== ACTAS RELEVANTES ===\n--- Reunión [{id_fts}] 2026-06-10 ---\n{fts_acta}")
        )
        budget = base_size + 100  # excluye el transcript largo de 2000 chars

        ctx, used = build_context(db, RESUMEN_UNICO, meeting_id=id_focus, budget=budget)

        # (a) el bloque TRANSCRIPCIÓN fue degradado
        assert "TRANSCRIPCIÓN" not in ctx
        # (b) el acta FTS relevante sí está
        assert RESUMEN_UNICO in ctx
        # (c) el índice conserva el resumen (no cayó a idx_no_resumen)
        assert RESUMEN_FOCUS in ctx


# ---------------------------------------------------------------------------
# answer() — happy path, history cap, fail-safe, perf
# ---------------------------------------------------------------------------

class TestAnswerHappyPath:
    def test_answer_happy_path_stub_llm(self, db, monkeypatch):
        _insert_meeting(db, "Reunión test6", minutes={"resumen": "Test6 resumen"},
                         started_at="2026-06-07 10:00:00")

        captured_messages = []

        def stub_chat_memory(messages, **kw):
            captured_messages.clear()
            captured_messages.extend(messages)
            return "[stub] " + str(len(messages))

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_memory)

        result = _assistant_mod.answer(db, "¿Qué se habló?")
        assert result.get("ok") is True
        assert (result.get("answer") or "").startswith("[stub]")
        assert captured_messages[0]["role"] == "system"
        assert _assistant_mod.ASSISTANT_SYSTEM[:30] in captured_messages[0]["content"]
        assert captured_messages[-1]["role"] == "user"
        assert captured_messages[-1]["content"] == "¿Qué se habló?"

    def test_history_capped_at_six_turns(self, db, monkeypatch):
        _insert_meeting(db, "Reunión test6", minutes={"resumen": "Test6 resumen"},
                         started_at="2026-06-07 10:00:00")

        captured_messages = []

        def stub_chat_memory(messages, **kw):
            captured_messages.clear()
            captured_messages.extend(messages)
            return "[stub7]"

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_memory)

        history_20 = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turno {i}"}
            for i in range(20)
        ]
        _assistant_mod.answer(db, "Pregunta final", history=history_20)

        history_in_messages = [m for m in captured_messages if m["role"] in ("user", "assistant")]
        history_count = len(history_in_messages) - 1  # el último es el user actual
        assert history_count <= 6
        assert len(captured_messages) <= 9

    def test_answer_failsafe_insights_unavailable(self, db, monkeypatch):
        _insert_meeting(db, "Reunión test6", minutes={"resumen": "Test6 resumen"},
                         started_at="2026-06-07 10:00:00")

        def stub_chat_unavailable(messages, **kw):
            raise _insights_mod.InsightsUnavailable("caído")

        monkeypatch.setattr(_insights_mod, "chat_memory", stub_chat_unavailable)

        result = _assistant_mod.answer(db, "¿Qué se habló?")
        assert result.get("ok") is False
        assert "error" in result  # no propagó la excepción (fail-safe)


class TestAnswerPerf:
    def test_meetings_index_called_at_most_once_per_build_context(self, db):
        _insert_meeting(db, "Reunión perf1", minutes={"resumen": "Perf alfa"}, started_at="2026-06-08 10:00:00")
        _insert_meeting(db, "Reunión perf2", minutes={"resumen": "Perf beta"}, started_at="2026-06-09 10:00:00")

        call_count = {"n": 0}
        original_meetings_index = db.meetings_index

        def counting_meetings_index(*args, **kwargs):
            call_count["n"] += 1
            return original_meetings_index(*args, **kwargs)

        db.meetings_index = counting_meetings_index
        try:
            build_context(db, "perf test")
        finally:
            db.meetings_index = original_meetings_index

        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# _budget_chars — sigue al backend BATCH resuelto (no al global)
# ---------------------------------------------------------------------------

class TestBudgetChars:
    _BUDGET_ENV_KEYS = (
        "ASSISTANT_CONTEXT_BUDGET_CHARS",
        "INSIGHTS_BACKEND",
        "INSIGHTS_BACKEND_BATCH",
        "INSIGHTS_BACKEND_LIVE",
    )

    @pytest.fixture(autouse=True)
    def _clean_budget_env(self, monkeypatch):
        for k in self._BUDGET_ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        yield

    def test_default_no_env(self, monkeypatch):
        assert _budget_chars() == 80000

    def test_batch_endpoint_global_unset(self, monkeypatch):
        """BUG FIX histórico: BATCH=endpoint con global sin tocar -> presupuesto chico
        (antes devolvía 80000 porque solo miraba INSIGHTS_BACKEND global)."""
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        assert _budget_chars() == 18000

    def test_global_endpoint_batch_inherits(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND", "endpoint")
        assert _budget_chars() == 18000

    def test_batch_override_wins_over_global(self, monkeypatch):
        """El override per-task pisa al global; el budget debe seguir al per-task."""
        monkeypatch.setenv("INSIGHTS_BACKEND", "endpoint")
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
        assert _budget_chars() == 80000

    def test_batch_openrouter_wide_budget(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "openrouter")
        assert _budget_chars() == 80000

    def test_explicit_override_wins_over_backend(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_CONTEXT_BUDGET_CHARS", "5000")
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        assert _budget_chars() == 5000

    def test_invalid_override_falls_back_to_backend(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_CONTEXT_BUDGET_CHARS", "no-es-int")
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        assert _budget_chars() == 18000


# ---------------------------------------------------------------------------
# Retrieval OR — fix de recuperación con lenguaje natural (match="or")
# ---------------------------------------------------------------------------

@pytest.fixture()
def retrieval_db(tmp_path):
    db = TranscriptionDB(db_path=str(tmp_path / "test_retrieval.db"))

    minutes_a = {
        "resumen": "Discutimos la amenaza de las USS en el sector este.",
        "temas": ["Aparición de un miembro de las USS", "fuerzas especiales Umbrella"],
        "decisiones": ["Evacuar el sector"],
        "pendientes": [],
        "propuestas": [],
    }
    id_a = db.meeting_insert(
        title="Reunión de seguridad USS",
        transcript="Es un miembro de las USS, fuerzas especiales de Umbrella. "
                   "Se discutió el protocolo de respuesta ante la amenaza.",
        segments_json="[]",
        duration_seconds=90.0,
        started_at="2026-06-10 10:00:00",
        insights_json=None,
        minutes_json=json.dumps(minutes_a),
    )

    minutes_b = {
        "resumen": "Revisión de entregas del proyecto Alpha.",
        "temas": ["entregas", "cronograma"],
        "decisiones": [],
        "pendientes": [],
        "propuestas": [],
    }
    id_b = db.meeting_insert(
        title="Revisión del proyecto Alpha",
        transcript="Actualizamos el cronograma de entregas del proyecto Alpha.",
        segments_json="[]",
        duration_seconds=45.0,
        started_at="2026-06-11 10:00:00",
        insights_json=None,
        minutes_json=json.dumps(minutes_b),
    )
    return db, id_a, id_b


class TestRetrievalOrFix:
    def test_and_exact_token_finds_a(self, retrieval_db):
        db, id_a, id_b = retrieval_db
        res1 = db.meetings_search("uss", match="and")
        ids1 = [r["id"] for r in res1]
        assert id_a in ids1
        assert id_b not in ids1

    def test_and_natural_question_does_not_find_a(self, retrieval_db):
        """Demuestra el bug original: AND estricto con lenguaje natural falla."""
        db, id_a, id_b = retrieval_db
        res2 = db.meetings_search("que dijeron de uss", match="and")
        ids2 = [r["id"] for r in res2]
        assert id_a not in ids2

    def test_or_natural_question_finds_a(self, retrieval_db):
        """Fix verificado: OR con lenguaje natural sí encuentra A."""
        db, id_a, id_b = retrieval_db
        res3 = db.meetings_search("que dijeron de uss", match="or")
        ids3 = [r["id"] for r in res3]
        assert id_a in ids3

    def test_search_terms_filters_stopwords(self):
        terms = _search_terms("que dijeron de uss")
        terms_list = terms.split()
        assert "uss" in terms_list
        assert "que" not in terms_list
        assert "de" not in terms_list
        assert "dijeron" not in terms_list

    def test_search_terms_only_stopwords_falls_back_to_original(self):
        terms_fallback = _search_terms("que de la")
        assert terms_fallback == "que de la"

    def test_search_terms_short_token_discarded(self):
        terms_short = _search_terms("a uss")
        assert "uss" in terms_short.split()
        assert "a" not in terms_short.split()

    def test_build_context_natural_question_finds_uss_meeting(self, retrieval_db):
        db, id_a, id_b = retrieval_db
        ctx, used_ids = build_context(db, "que dijeron de uss")
        assert id_a in used_ids
        assert "USS" in ctx or "uss" in ctx.lower()
        assert isinstance(ctx, str) and len(ctx) > 0
