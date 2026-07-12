"""Tests para la línea de tiempo de momentos clave (capítulos).

Reescrito desde el script huérfano test_chapters.py (raíz) para la unidad
2.2 del PLAN-MEJORAS: el original tenía ``def test_*`` pero ERRORABA bajo
pytest porque envolvía ``sys.stdout`` con un ``io.TextIOWrapper`` a nivel de
módulo, lo que pytest's capture rompe con "ValueError: I/O operation on
closed file". Aquí se preservan los mismos invariantes sin tocar stdout:
generate_chapters happy/failsafe, persistencia en DB y _mmss_to_seconds.
"""
import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixture de segmentos
# ---------------------------------------------------------------------------

SEGMENTS = [
    {"t": 0.0, "time": "00:00", "speaker": "Yo", "text": "Hola, vamos a empezar."},
    {"t": 7.0, "time": "00:07", "speaker": "Ellos", "text": "De acuerdo."},
    {"t": 68.0, "time": "01:08", "speaker": "Yo", "text": "Ahora el presupuesto."},
    {"t": 130.0, "time": "02:10", "speaker": "Ellos", "text": "Hay que revisarlo."},
]

TRANSCRIPT = "\n".join(
    f"[{s['time']} {s['speaker']}] {s['text']}" for s in SEGMENTS
)


# ---------------------------------------------------------------------------
# Caso 1: happy path — snapping correcto
# ---------------------------------------------------------------------------

def test_generate_chapters_happy():
    from core import insights

    stub_response = json.dumps({
        "capitulos": [
            {"inicio": "00:06", "titulo": "Apertura", "resumen": ""},
            {"inicio": "01:10", "titulo": "Presupuesto", "resumen": "x"},
        ]
    })

    with patch.object(insights, "_chat", return_value=stub_response):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)

    assert len(result) == 2
    assert all(c["titulo"] for c in result)
    assert result[0]["t"] <= result[1]["t"]

    # "00:06" -> secs=6; segmento más cercano es t=7 (|7-6|=1 < |0-6|=6)
    cap0 = result[0]
    assert abs(cap0["t"] - 7.0) < 0.01
    assert cap0["inicio"] == "00:07"

    # "01:10" -> secs=70; segmento más cercano t=68 (|68-70|=2 < |130-70|=60)
    cap1 = result[1]
    assert abs(cap1["t"] - 68.0) < 0.01
    assert cap1["inicio"] == "01:08"


# ---------------------------------------------------------------------------
# Caso 2: fail-safe (InsightsUnavailable, JSON inválido, transcript vacío)
# ---------------------------------------------------------------------------

def test_failsafe_insights_unavailable():
    from core import insights

    with patch.object(insights, "_chat", side_effect=insights.InsightsUnavailable("sin clave")):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    assert result == []


def test_failsafe_invalid_json():
    from core import insights

    with patch.object(insights, "_chat", return_value="esto no es json"):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    assert result == []


def test_failsafe_empty_transcript_no_llm_call():
    from core import insights

    called = []

    def stub_chat(*a, **kw):
        called.append(1)
        return "{}"

    with patch.object(insights, "_chat", side_effect=stub_chat):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters("   ", SEGMENTS)
    assert result == []
    assert len(called) == 0


def test_failsafe_not_available_no_llm_call():
    from core import insights

    called = []

    def stub_chat(*a, **kw):
        called.append(1)
        return "{}"

    with patch.object(insights, "_chat", side_effect=stub_chat):
        with patch.object(insights, "is_available", return_value=False):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    assert result == []
    assert len(called) == 0


# ---------------------------------------------------------------------------
# Caso 3: DB — meeting_insert con chapters_json y meeting_set_chapters
# ---------------------------------------------------------------------------

def test_db_chapters(tmp_path):
    from db.database import TranscriptionDB

    db_path = str(tmp_path / "test.db")
    with patch("config.DB_PATH", db_path):
        db = TranscriptionDB(db_path)

        chs = [{"t": 7.0, "inicio": "00:07", "titulo": "Apertura", "resumen": ""}]
        chs_json = json.dumps(chs)
        mid = db.meeting_insert(
            title="Test",
            transcript=TRANSCRIPT,
            segments_json=json.dumps(SEGMENTS),
            duration_seconds=130.0,
            started_at="2026-01-01 10:00:00",
            chapters_json=chs_json,
        )
        assert isinstance(mid, int) and mid > 0

        row = db.meeting_get(mid)
        assert row is not None
        assert row.get("chapters_json") == chs_json

        new_chs = [{"t": 68.0, "inicio": "01:08", "titulo": "Presupuesto", "resumen": "x"}]
        new_json = json.dumps(new_chs)
        rc = db.meeting_set_chapters(mid, new_json)
        assert rc == 1

        row2 = db.meeting_get(mid)
        assert row2.get("chapters_json") == new_json


# ---------------------------------------------------------------------------
# Caso 4: _mmss_to_seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mmss,expected", [
    ("01:10", 70.0),
    ("00:06", 6.0),
    ("1:02:03", 3723.0),
])
def test_mmss_to_seconds_valid(mmss, expected):
    from core.insights import _mmss_to_seconds
    assert abs(_mmss_to_seconds(mmss) - expected) < 0.01


@pytest.mark.parametrize("mmss", ["abc", "", None])
def test_mmss_to_seconds_invalid(mmss):
    from core.insights import _mmss_to_seconds
    assert _mmss_to_seconds(mmss) == 0.0
