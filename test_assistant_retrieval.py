"""test_assistant_retrieval.py — Verifica el fix de recuperación OR en el Asistente.

Casos cubiertos:
  1. meetings_search("uss", match="and")       → encuentra A
  2. meetings_search("que dijeron de uss", match="and")  → NO encuentra A (bug viejo demostrado)
  3. meetings_search("que dijeron de uss", match="or")   → SÍ encuentra A (fix verificado)
  4. _search_terms("que dijeron de uss")       → contiene "uss", NO contiene "que"/"de"/"dijeron"
  5. build_context(db, "que dijeron de uss")   → contexto contiene "USS" y used_ids incluye id de A

Uso: python test_assistant_retrieval.py
Exit 0 si todo pasa, 1 si algún caso falla.
Sin red. DB temporal.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Salida UTF-8: la consola de Windows (cp1252) no codifica caracteres como → en los prints.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"
_failures = []


def ok(name: str, condition: bool, note: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f"  ({note})" if note else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _failures.append(name)


# ---------------------------------------------------------------------------
# Preparar DB sintética
# ---------------------------------------------------------------------------
tmp_dir = tempfile.mkdtemp()
db_path = os.path.join(tmp_dir, "test_retrieval.db")

print(f"\nDB temporal: {db_path}\n")

from db.database import TranscriptionDB

db = TranscriptionDB(db_path=db_path)

# Reunión A: contiene "USS" en transcript y en temas
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

# Reunión B: no contiene "uss"
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

print(f"Reunión A (USS): id={id_a}")
print(f"Reunión B (Alpha): id={id_b}\n")

# ---------------------------------------------------------------------------
# Caso 1 — AND exacto con token "uss": debe encontrar A
# ---------------------------------------------------------------------------
print("Caso 1: meetings_search('uss', match='and') → encuentra A")
res1 = db.meetings_search("uss", match="and")
ids1 = [r["id"] for r in res1]
ok("encuentra reunión A con 'uss' AND", id_a in ids1, note=f"ids={ids1}")
ok("no encuentra reunión B con 'uss' AND", id_b not in ids1, note=f"ids={ids1}")

# ---------------------------------------------------------------------------
# Caso 2 — AND con pregunta natural: NO debe encontrar A (demuestra el bug original)
# ---------------------------------------------------------------------------
print("\nCaso 2: meetings_search('que dijeron de uss', match='and') → NO encuentra A (bug viejo)")
res2 = db.meetings_search("que dijeron de uss", match="and")
ids2 = [r["id"] for r in res2]
ok("AND: pregunta natural NO encuentra A (confirma bug original)", id_a not in ids2,
   note=f"ids={ids2} — si A aparece aquí el bug ya no existe (token 'dijeron' presente en transcript)")

# ---------------------------------------------------------------------------
# Caso 3 — OR con pregunta natural: SÍ debe encontrar A (fix)
# ---------------------------------------------------------------------------
print("\nCaso 3: meetings_search('que dijeron de uss', match='or') → SÍ encuentra A (fix)")
res3 = db.meetings_search("que dijeron de uss", match="or")
ids3 = [r["id"] for r in res3]
ok("OR: pregunta natural SÍ encuentra A", id_a in ids3, note=f"ids={ids3}")

# ---------------------------------------------------------------------------
# Caso 4 — _search_terms filtra stopwords correctamente
# ---------------------------------------------------------------------------
print("\nCaso 4: _search_terms('que dijeron de uss')")
from core.assistant import _search_terms

terms = _search_terms("que dijeron de uss")
terms_list = terms.split()
print(f"  _search_terms → {terms!r}  tokens={terms_list}")
ok("'uss' presente en terms", "uss" in terms_list, note=f"terms={terms!r}")
ok("'que' NO presente en terms (stopword)", "que" not in terms_list, note=f"terms={terms!r}")
ok("'de' NO presente en terms (stopword)", "de" not in terms_list, note=f"terms={terms!r}")
ok("'dijeron' NO presente en terms (stopword)", "dijeron" not in terms_list, note=f"terms={terms!r}")

# Caso borde: si SOLO quedan stopwords → fallback al mensaje original
terms_fallback = _search_terms("que de la")
ok("fallback cuando solo stopwords → devuelve original", terms_fallback == "que de la",
   note=f"resultado={terms_fallback!r}")

# Token corto (<2 chars): "a" descartado
terms_short = _search_terms("a uss")
ok("token <2 chars descartado, 'uss' permanece", "uss" in terms_short.split(), note=f"{terms_short!r}")

# ---------------------------------------------------------------------------
# Caso 5 — build_context con pregunta natural encuentra contexto de reunión USS
# ---------------------------------------------------------------------------
print("\nCaso 5: build_context(db, 'que dijeron de uss') → contexto contiene 'USS'")
from core.assistant import build_context

ctx, used_ids = build_context(db, "que dijeron de uss")

print(f"  used_meeting_ids={used_ids}")
ok("used_meeting_ids incluye id de reunión A", id_a in used_ids, note=f"used={used_ids}")
ok("contexto contiene 'USS' (mayúsculas o mezcla)", "USS" in ctx or "uss" in ctx.lower(),
   note=f"ctx[:300]={ctx[:300]!r}")
ok("contexto es string no vacío", isinstance(ctx, str) and len(ctx) > 0)

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"RESULTADO: {len(_failures)} caso(s) FALLARON: {_failures}")
    sys.exit(1)
else:
    print("RESULTADO: Todos los casos pasaron.")
    sys.exit(0)
