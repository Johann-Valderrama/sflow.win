"""Test: línea de tiempo de momentos clave (capítulos).

Ejecutar: python test_chapters.py
Exit 0 = todo PASS, exit 1 = algún FAIL.
"""
import io
import json
import os
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

# Forzar UTF-8 en la salida para evitar UnicodeEncodeError en consolas cp1252
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        msg = f"  {FAIL}  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        _failures.append(name)


# ---------------------------------------------------------------------------
# Fixture de segmentos
# ---------------------------------------------------------------------------

SEGMENTS = [
    {"t": 0.0,  "time": "00:00", "speaker": "Yo",    "text": "Hola, vamos a empezar."},
    {"t": 7.0,  "time": "00:07", "speaker": "Ellos", "text": "De acuerdo."},
    {"t": 68.0, "time": "01:08", "speaker": "Yo",    "text": "Ahora el presupuesto."},
    {"t": 130.0,"time": "02:10", "speaker": "Ellos", "text": "Hay que revisarlo."},
]

TRANSCRIPT = "\n".join(
    f"[{s['time']} {s['speaker']}] {s['text']}" for s in SEGMENTS
)

# ---------------------------------------------------------------------------
# Caso 1: happy path — snapping correcto
# ---------------------------------------------------------------------------

def test_generate_chapters_happy():
    print("\n[1] generate_chapters happy + snapping")
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

    check("devuelve 2 capítulos", len(result) == 2, f"got {len(result)}")
    check("títulos presentes", all(c["titulo"] for c in result))
    check("orden cronológico", result[0]["t"] <= result[1]["t"])

    # "00:06" → secs=6; segmento más cercano es t=7 (|7-6|=1 < |0-6|=6)
    cap0 = result[0]
    check("snap 00:06 → segmento t=7", abs(cap0["t"] - 7.0) < 0.01,
          f"t={cap0['t']}, inicio={cap0['inicio']}")
    check("snap 00:06 → inicio '00:07'", cap0["inicio"] == "00:07",
          f"got '{cap0['inicio']}'")

    # "01:10" → secs=70; segmento más cercano t=68 (|68-70|=2 < |130-70|=60)
    cap1 = result[1]
    check("snap 01:10 → segmento t=68", abs(cap1["t"] - 68.0) < 0.01,
          f"t={cap1['t']}, inicio={cap1['inicio']}")
    check("snap 01:10 → inicio '01:08'", cap1["inicio"] == "01:08",
          f"got '{cap1['inicio']}'")


# ---------------------------------------------------------------------------
# Caso 2: fail-safe (InsightsUnavailable, JSON inválido, transcript vacío)
# ---------------------------------------------------------------------------

def test_failsafe():
    print("\n[2] fail-safe")
    from core import insights

    # a) InsightsUnavailable
    with patch.object(insights, "_chat", side_effect=insights.InsightsUnavailable("sin clave")):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    check("InsightsUnavailable → []", result == [], f"got {result}")

    # b) respuesta no-JSON
    with patch.object(insights, "_chat", return_value="esto no es json"):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    check("JSON inválido → []", result == [], f"got {result}")

    # c) transcript vacío → sin llamar al LLM
    called = []
    def stub_chat(*a, **kw):
        called.append(1)
        return "{}"
    with patch.object(insights, "_chat", side_effect=stub_chat):
        with patch.object(insights, "is_available", return_value=True):
            result = insights.generate_chapters("   ", SEGMENTS)
    check("transcript vacío → []", result == [], f"got {result}")
    check("transcript vacío → LLM no llamado", len(called) == 0, f"calls={called}")

    # d) is_available=False → sin llamar al LLM
    called2 = []
    def stub_chat2(*a, **kw):
        called2.append(1)
        return "{}"
    with patch.object(insights, "_chat", side_effect=stub_chat2):
        with patch.object(insights, "is_available", return_value=False):
            result = insights.generate_chapters(TRANSCRIPT, SEGMENTS)
    check("is_available=False → []", result == [], f"got {result}")
    check("is_available=False → LLM no llamado", len(called2) == 0)


# ---------------------------------------------------------------------------
# Caso 3: DB — meeting_insert con chapters_json y meeting_set_chapters
# ---------------------------------------------------------------------------

def test_db_chapters():
    print("\n[3] DB chapters")
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmpdir, "test.db")
        with patch("config.DB_PATH", db_path):
            from db.database import TranscriptionDB
            db = TranscriptionDB(db_path)

            # insert con chapters_json
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
            check("meeting_insert devuelve id entero", isinstance(mid, int) and mid > 0, f"id={mid}")

            row = db.meeting_get(mid)
            check("meeting_get devuelve fila", row is not None)
            check("chapters_json persiste", row.get("chapters_json") == chs_json,
                  f"got {row.get('chapters_json')!r}")

            # meeting_set_chapters
            new_chs = [{"t": 68.0, "inicio": "01:08", "titulo": "Presupuesto", "resumen": "x"}]
            new_json = json.dumps(new_chs)
            rc = db.meeting_set_chapters(mid, new_json)
            check("meeting_set_chapters rowcount=1", rc == 1, f"rc={rc}")

            row2 = db.meeting_get(mid)
            check("chapters_json actualizado", row2.get("chapters_json") == new_json,
                  f"got {row2.get('chapters_json')!r}")
    finally:
        # Limpiar tmpdir ignorando errores de lock en Windows
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Caso 4: _mmss_to_seconds
# ---------------------------------------------------------------------------

def test_mmss_to_seconds():
    print("\n[4] _mmss_to_seconds")
    from core.insights import _mmss_to_seconds

    check("'01:10' → 70", abs(_mmss_to_seconds("01:10") - 70.0) < 0.01,
          f"got {_mmss_to_seconds('01:10')}")
    check("'00:06' → 6", abs(_mmss_to_seconds("00:06") - 6.0) < 0.01,
          f"got {_mmss_to_seconds('00:06')}")
    check("'1:02:03' → 3723", abs(_mmss_to_seconds("1:02:03") - 3723.0) < 0.01,
          f"got {_mmss_to_seconds('1:02:03')}")
    check("entrada basura → 0", _mmss_to_seconds("abc") == 0.0,
          f"got {_mmss_to_seconds('abc')}")
    check("cadena vacía → 0", _mmss_to_seconds("") == 0.0,
          f"got {_mmss_to_seconds('')}")
    check("None → 0", _mmss_to_seconds(None) == 0.0,
          f"got {_mmss_to_seconds(None)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("test_chapters.py")
    print("=" * 60)

    tests = [
        test_mmss_to_seconds,
        test_generate_chapters_happy,
        test_failsafe,
        test_db_chapters,
    ]

    for t in tests:
        try:
            t()
        except Exception:  # noqa: BLE001
            print(f"  FAIL  {t.__name__} (excepción inesperada)")
            traceback.print_exc()
            _failures.append(t.__name__)

    print("\n" + "=" * 60)
    if _failures:
        print(f"RESULTADO: {len(_failures)} FAIL(s): {', '.join(_failures)}")
        sys.exit(1)
    else:
        print("RESULTADO: todos los tests PASS")
        sys.exit(0)
