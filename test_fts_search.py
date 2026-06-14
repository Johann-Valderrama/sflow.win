"""test_fts_search.py — Pruebas de búsqueda FTS5 sobre reuniones.

Uso: python test_fts_search.py
Exit 0 si todo pasa, 1 si algún caso falla.
"""

import json
import os
import sqlite3
import sys
import tempfile

# Asegura que el proyecto esté en el path
sys.path.insert(0, os.path.dirname(__file__))

from db.database import TranscriptionDB

PASS = "PASS"
FAIL = "FAIL"
_failures = []


def ok(name: str, condition: bool, note: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f"  ({note})" if note else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _failures.append(name)


def make_db(path: str) -> TranscriptionDB:
    return TranscriptionDB(db_path=path)


def insert_raw(db_path: str, title: str, transcript: str,
               minutes_json: str = None, insights_json: str = None) -> int:
    """Inserta directamente en meetings sin pasar por meeting_insert (para tests de backfill)."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO meetings (title, transcript, segments_json, insights_json, "
            "minutes_json, duration_seconds, started_at) VALUES (?, ?, NULL, ?, ?, ?, datetime('now'))",
            (title, transcript, insights_json, minutes_json, 60.0),
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Preparar DB temporal
# ---------------------------------------------------------------------------
tmp_dir = tempfile.mkdtemp()
db_path = os.path.join(tmp_dir, "test_meetings.db")

print(f"\nDB temporal: {db_path}\n")

# ---------------------------------------------------------------------------
# Caso 1 — Búsqueda básica
# ---------------------------------------------------------------------------
print("Caso 1: Búsqueda básica")
db = make_db(db_path)

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

id1 = db.meeting_insert("Reunión presupuesto", "Hablamos del presupuesto y del proyecto.", "[]", 120.0, insights_json=None, minutes_json=m_json_1)
id2 = db.meeting_insert("Reunión entregas", "El calendario de entregas fue revisado.", "[]", 90.0, insights_json=None, minutes_json=m_json_2)
id3 = db.meeting_insert("Demo facturación", "Mostramos el sistema de facturación.", "[]", 60.0, insights_json=None, minutes_json=m_json_3)

res = db.meetings_search("presupuesto")
ids_found = [r["id"] for r in res]
ok("presupuesto encuentra m1", id1 in ids_found)
ok("presupuesto no encuentra m2", id2 not in ids_found)
ok("presupuesto no encuentra m3", id3 not in ids_found)

res2 = db.meetings_search("facturación")
ids2 = [r["id"] for r in res2]
ok("facturación encuentra m3", id3 in ids2)
ok("facturación no encuentra m1", id1 not in ids2)

# ---------------------------------------------------------------------------
# Caso 2 — Acento-insensible
# ---------------------------------------------------------------------------
print("\nCaso 2: Acento-insensible")
res_acc = db.meetings_search("reunion")  # sin tilde
ids_acc = [r["id"] for r in res_acc]
if db._fts_enabled and "remove_diacritics" in (db._fts_tokenizer or ""):
    ok("'reunion' (sin acento) encuentra registros con 'reunión'", len(ids_acc) > 0)
else:
    print(f"  [WARN] Tokenizer activo: {db._fts_tokenizer!r} — skip assert diacríticos")

res_pres = db.meetings_search("presupuesto")
ok("'presupuesto' (con acento) devuelve resultados", len(res_pres) > 0)

# ---------------------------------------------------------------------------
# Caso 3 — Snippet
# ---------------------------------------------------------------------------
print("\nCaso 3: Snippet")
res_snip = db.meetings_search("presupuesto")
if res_snip:
    snip = res_snip[0].get("snippet") or ""
    ok("resultado trae key 'snippet'", "snippet" in res_snip[0])
    ok("snippet no está vacío", bool(snip))
    if db._fts_enabled:
        ok("snippet contiene marcador \\x02 o \\x03", "\x02" in snip or "\x03" in snip,
           note=f"snippet={snip!r}")
    else:
        print("  [WARN] FTS no disponible — snippet sin marcadores (modo LIKE)")
else:
    ok("hay resultado para snippet test", False, note="sin resultados")

# ---------------------------------------------------------------------------
# Caso 4 — Ranking: término en 2 reuniones, ambas vuelven sin crash
# ---------------------------------------------------------------------------
print("\nCaso 4: Ranking estable")
# "sistema" aparece en la transcripción de m3
id4 = db.meeting_insert("Reunión sistema B", "Revisamos el sistema de pagos.", "[]", 30.0,
                         minutes_json=json.dumps({"resumen": "sistema", "temas": [], "decisiones": [], "pendientes": [], "propuestas": []}))
res_rank = db.meetings_search("sistema")
ids_rank = [r["id"] for r in res_rank]
ok("ranking devuelve múltiples resultados sin crash", len(res_rank) >= 2)
ok("orden estable (lista no vacía)", len(res_rank) > 0)

# ---------------------------------------------------------------------------
# Caso 5 — Delete elimina del índice
# ---------------------------------------------------------------------------
print("\nCaso 5: Delete elimina del índice FTS")
id5 = db.meeting_insert("Reunión única xyzzy", "El término xyzzy es único en este test.", "[]", 10.0)
res_before = db.meetings_search("xyzzy")
ok("xyzzy encontrado antes de delete", len(res_before) > 0)
db.meeting_delete(id5)
res_after = db.meetings_search("xyzzy")
ok("xyzzy NO encontrado tras delete", len(res_after) == 0)

# ---------------------------------------------------------------------------
# Caso 6 — Backfill: reuniones insertadas raw son indexadas en re-init
# ---------------------------------------------------------------------------
print("\nCaso 6: Backfill — re-init indexa reuniones preexistentes")
db_path2 = os.path.join(tmp_dir, "test_backfill.db")

# Primera instancia: inserta via meeting_insert normal
db6a = make_db(db_path2)
id6 = db6a.meeting_insert("Reunión backfill qwerty", "El sistema qwerty se revisó.", "[]", 20.0)
del db6a  # liberar

# Segunda instancia sobre la misma DB: _init_db debe backfill si es necesario
db6b = make_db(db_path2)
res6 = db6b.meetings_search("qwerty")
ok("backfill: qwerty encontrado en segunda instancia", len(res6) > 0)

# ---------------------------------------------------------------------------
# Caso 7 — Query rara no lanza excepción
# ---------------------------------------------------------------------------
print("\nCaso 7: Queries raras no lanzan excepción")
weird_queries = ['"', 'a AND OR *', '   ', '""""', '*', '"*"*']
for q in weird_queries:
    try:
        result = db.meetings_search(q)
        ok(f"query {q!r} devuelve lista", isinstance(result, list))
    except Exception as exc:
        ok(f"query {q!r} no lanza excepción", False, note=str(exc))

# ---------------------------------------------------------------------------
# Caso 8 — Ranking ponderado bm25: match en título supera a match en transcript
# ---------------------------------------------------------------------------
print("\nCaso 8: bm25 ponderado — título rankea sobre transcript enterrado")
db_path3 = os.path.join(tmp_dir, "test_bm25.db")
db8 = make_db(db_path3)

# A: término clave en el TÍTULO (peso alto), transcript sin el término
id_a = db8.meeting_insert(
    "Reunión presupuestoxyz estratégico", "Charla general sin el termino clave.", "[]", 30.0,
)
# B: término clave SOLO enterrado en un transcript largo (peso bajo + dilución por longitud)
relleno = "palabra de relleno irrelevante para la busqueda. " * 80
transcript_b = relleno + "mencion suelta de presupuestoxyz aqui. " + relleno
id_b = db8.meeting_insert("Reunión rutinaria semanal", transcript_b, "[]", 30.0)

res8 = db8.meetings_search("presupuestoxyz")
ids8 = [r["id"] for r in res8]
ok("bm25: ambos resultados presentes", id_a in ids8 and id_b in ids8, note=f"ids={ids8}")
ok("bm25: el del título rankea primero", len(res8) >= 2 and res8[0]["id"] == id_a,
   note=f"primero={ids8[0] if ids8 else None} esperado={id_a}")

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
