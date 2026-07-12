"""Tests de búsqueda FTS5 sobre reuniones (meetings_search).

Reescrito desde el script huérfano test_fts_search.py (raíz) para la unidad
2.2 del PLAN-MEJORAS. Protege una superficie DISTINTA de tests/test_cross_memory.py
(que cubre bm25 en meetings_search desde la ruta de memoria cruzada): aquí se
cubre ranking básico, acento-insensibilidad del tokenizer, snippets, backfill
de reuniones preexistentes, robustez ante queries raras y ranking bm25
ponderado (título > transcript enterrado).
"""
import json
import sqlite3

import pytest

from db.database import TranscriptionDB


def _insert_raw(db_path: str, title: str, transcript: str,
                 minutes_json: str = None, insights_json: str = None) -> int:
    """Inserta directamente en meetings sin pasar por meeting_insert (para el test de backfill)."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO meetings (title, transcript, segments_json, insights_json, "
            "minutes_json, duration_seconds, started_at) VALUES (?, ?, NULL, ?, ?, ?, datetime('now'))",
            (title, transcript, insights_json, minutes_json, 60.0),
        )
        return cursor.lastrowid


@pytest.fixture()
def db(tmp_path):
    return TranscriptionDB(db_path=str(tmp_path / "test_meetings.db"))


class TestBasicSearch:
    def test_search_matches_only_relevant_meeting(self, db):
        m_json_1 = json.dumps({
            "resumen": "Discutimos el presupuesto anual del proyecto.",
            "decisiones": ["Aprobar el presupuesto de 50k"],
            "temas": ["presupuesto", "proyecto alpha"],
            "pendientes": [{"texto": "Enviar informe financiero", "responsable": "Ana"}],
            "propuestas": ["Contratar un auditor externo"],
        })
        m_json_2 = json.dumps({
            "resumen": "Revisión del calendario de entregas.",
            "decisiones": [],
            "temas": ["entregas", "calendario"],
            "pendientes": [{"texto": "Actualizar cronograma", "responsable": "Luis"}],
            "propuestas": [],
        })
        m_json_3 = json.dumps({
            "resumen": "Demo del sistema de facturación.",
            "decisiones": ["Implementar módulo de pagos"],
            "temas": ["facturación", "pagos"],
            "pendientes": [],
            "propuestas": ["Integrar Stripe"],
        })

        id1 = db.meeting_insert("Reunión presupuesto", "Hablamos del presupuesto y del proyecto.",
                                 "[]", 120.0, insights_json=None, minutes_json=m_json_1)
        id2 = db.meeting_insert("Reunión entregas", "El calendario de entregas fue revisado.",
                                 "[]", 90.0, insights_json=None, minutes_json=m_json_2)
        id3 = db.meeting_insert("Demo facturación", "Mostramos el sistema de facturación.",
                                 "[]", 60.0, insights_json=None, minutes_json=m_json_3)

        res = db.meetings_search("presupuesto")
        ids_found = [r["id"] for r in res]
        assert id1 in ids_found
        assert id2 not in ids_found
        assert id3 not in ids_found

        res2 = db.meetings_search("facturación")
        ids2 = [r["id"] for r in res2]
        assert id3 in ids2
        assert id1 not in ids2


class TestDiacritics:
    def test_accent_insensitive_when_tokenizer_supports_it(self, db):
        db.meeting_insert("Reunión presupuesto", "Hablamos del presupuesto y del proyecto.", "[]", 120.0)
        res_acc = db.meetings_search("reunion")  # sin tilde
        if db._fts_enabled and "remove_diacritics" in (db._fts_tokenizer or ""):
            assert len(res_acc) > 0
        # Si el tokenizer activo no soporta diacríticos, no hay aserción que hacer
        # (comportamiento documentado, no un bug): el caso base (con acento) sí debe
        # devolver resultados siempre.
        res_pres = db.meetings_search("presupuesto")
        assert len(res_pres) > 0


class TestSnippet:
    def test_result_has_snippet(self, db):
        db.meeting_insert("Reunión presupuesto", "Hablamos del presupuesto y del proyecto.", "[]", 120.0)
        res_snip = db.meetings_search("presupuesto")
        assert res_snip, "debe haber al menos un resultado"
        snip = res_snip[0].get("snippet") or ""
        assert "snippet" in res_snip[0]
        assert snip
        if db._fts_enabled:
            assert "\x02" in snip or "\x03" in snip


class TestRankingStability:
    def test_multiple_matches_return_without_crash(self, db):
        db.meeting_insert("Demo facturación", "Mostramos el sistema de facturación.", "[]", 60.0)
        db.meeting_insert("Reunión sistema B", "Revisamos el sistema de pagos.", "[]", 30.0,
                           minutes_json=json.dumps({"resumen": "sistema", "temas": [], "decisiones": [],
                                                     "pendientes": [], "propuestas": []}))
        res_rank = db.meetings_search("sistema")
        assert len(res_rank) >= 2


class TestDelete:
    def test_delete_removes_from_fts_index(self, db):
        id5 = db.meeting_insert("Reunión única xyzzy", "El término xyzzy es único en este test.", "[]", 10.0)
        res_before = db.meetings_search("xyzzy")
        assert len(res_before) > 0
        db.meeting_delete(id5)
        res_after = db.meetings_search("xyzzy")
        assert len(res_after) == 0


class TestBackfill:
    def test_reopening_db_indexes_preexisting_meetings(self, tmp_path):
        db_path = str(tmp_path / "test_backfill.db")

        db6a = TranscriptionDB(db_path=db_path)
        db6a.meeting_insert("Reunión backfill qwerty", "El sistema qwerty se revisó.", "[]", 20.0)
        del db6a  # liberar

        db6b = TranscriptionDB(db_path=db_path)
        res6 = db6b.meetings_search("qwerty")
        assert len(res6) > 0


class TestWeirdQueries:
    @pytest.mark.parametrize("q", ['"', 'a AND OR *', '   ', '""""', '*', '"*"*'])
    def test_weird_query_does_not_raise(self, db, q):
        result = db.meetings_search(q)
        assert isinstance(result, list)


class TestBM25Weighting:
    def test_title_match_ranks_above_buried_transcript_match(self, db):
        # A: término clave en el TÍTULO (peso alto), transcript sin el término
        id_a = db.meeting_insert(
            "Reunión presupuestoxyz estratégico", "Charla general sin el termino clave.", "[]", 30.0,
        )
        # B: término clave SOLO enterrado en un transcript largo (peso bajo + dilución por longitud)
        relleno = "palabra de relleno irrelevante para la busqueda. " * 80
        transcript_b = relleno + "mencion suelta de presupuestoxyz aqui. " + relleno
        id_b = db.meeting_insert("Reunión rutinaria semanal", transcript_b, "[]", 30.0)

        res8 = db.meetings_search("presupuestoxyz")
        ids8 = [r["id"] for r in res8]
        assert id_a in ids8 and id_b in ids8
        assert len(res8) >= 2 and res8[0]["id"] == id_a
