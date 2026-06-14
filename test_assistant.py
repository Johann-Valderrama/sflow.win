"""test_assistant.py — Pruebas unitarias para el módulo Asistente de reuniones (chat de memoria).

Uso: python test_assistant.py
Exit 0 si todo pasa, 1 si algún caso falla.
Sin red: el LLM se monkeypatchea.
DB temporal: no toca la DB real.
"""

import json
import os
import sys
import tempfile

# Asegura que el proyecto esté en el path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Helpers de test
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


def make_db(path: str):
    from db.database import TranscriptionDB
    return TranscriptionDB(db_path=path)


def insert_meeting(db, title: str, transcript: str = "", minutes: dict = None, started_at: str = None) -> int:
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


tmp_dir = tempfile.mkdtemp()
print(f"\nDB temporal: {tmp_dir}\n")

# ---------------------------------------------------------------------------
# Caso 1 — meetings_index
# ---------------------------------------------------------------------------
print("Caso 1: meetings_index")

db1 = make_db(os.path.join(tmp_dir, "test1.db"))
id_a = insert_meeting(db1, "Reunión Alpha", "transcripción alpha",
                      minutes={"resumen": "Resumen alpha", "temas": ["alpha"]},
                      started_at="2026-06-01 10:00:00")
id_b = insert_meeting(db1, "Reunión Beta", "transcripción beta",
                      minutes={"resumen": "Resumen beta", "temas": ["beta"]},
                      started_at="2026-06-02 10:00:00")

index = db1.meetings_index()
ok("devuelve lista", isinstance(index, list))
ok("contiene 2 reuniones", len(index) == 2)

ids_returned = [e["id"] for e in index]
ok("orden DESC por id (mayor primero)", ids_returned[0] > ids_returned[1])

# resumen correcto
by_id = {e["id"]: e for e in index}
ok("resumen de Alpha correcto", by_id[id_a]["resumen"] == "Resumen alpha",
   note=repr(by_id[id_a].get("resumen")))
ok("resumen de Beta correcto", by_id[id_b]["resumen"] == "Resumen beta")

# claves presentes
ok("tiene clave started_at", "started_at" in by_id[id_a])
ok("tiene clave title", by_id[id_a]["title"] == "Reunión Alpha")

# ---------------------------------------------------------------------------
# Caso 2 — _format_acta
# ---------------------------------------------------------------------------
print("\nCaso 2: _format_acta")

from core.assistant import _format_acta

minutes_full = {
    "resumen": "Reunión sobre presupuesto.",
    "decisiones": ["Aprobar 50k"],
    "temas": ["presupuesto", "personal"],
    "pendientes": [{"texto": "Enviar informe", "responsable": "Ana", "fecha": "viernes", "hora": None}],
    "propuestas": ["Contratar auditor"],
    "citas": [{"texto": "Próxima reunión", "fecha": "2026-06-20", "hora": "10:00"}],
}

texto = _format_acta(minutes_full)
ok("contiene resumen", "Reunión sobre presupuesto." in texto)
ok("contiene decisiones", "Aprobar 50k" in texto)
ok("contiene pendientes", "Enviar informe" in texto)
ok("contiene propuesta", "Contratar auditor" in texto)
ok("contiene temas", "presupuesto" in texto)

# dict vacío no lanza
try:
    texto_vacio = _format_acta({})
    ok("dict vacío no lanza", True)
    ok("dict vacío devuelve algo corto (str)", isinstance(texto_vacio, str))
except Exception as e:
    ok("dict vacío no lanza", False, note=str(e))

# ---------------------------------------------------------------------------
# Caso 3 — build_context global
# ---------------------------------------------------------------------------
print("\nCaso 3: build_context global")

db3 = make_db(os.path.join(tmp_dir, "test3.db"))
insert_meeting(db3, "Reunión presupuesto", "El presupuesto fue discutido en detalle.",
               minutes={"resumen": "Presupuesto aprobado", "temas": ["presupuesto"]},
               started_at="2026-06-03 10:00:00")
insert_meeting(db3, "Reunión entrega", "Revisamos las entregas del proyecto.",
               minutes={"resumen": "Entregas revisadas", "temas": ["entregas"]},
               started_at="2026-06-04 10:00:00")
insert_meeting(db3, "Demo sistema", "Demo del sistema de pagos y presupuesto.",
               minutes={"resumen": "Demo completada", "temas": ["demo", "pagos"]},
               started_at="2026-06-05 10:00:00")

from core.assistant import build_context

ctx, used = build_context(db3, "presupuesto")
ok("contexto es string", isinstance(ctx, str))
ok("contiene bloque índice", "ÍNDICE DE TUS REUNIONES" in ctx)
ok("used_meeting_ids no está vacío (FTS matchea)", len(used) > 0,
   note=f"used={used}")

# ---------------------------------------------------------------------------
# Caso 4 — build_context con presupuesto chico
# ---------------------------------------------------------------------------
print("\nCaso 4: build_context con presupuesto pequeño")

ctx_small, used_small = build_context(db3, "presupuesto", budget=300)
ok("no crashea con budget pequeño", True)
ok("ctx len <= budget*1.2", len(ctx_small) <= 300 * 1.2,
   note=f"len={len(ctx_small)}")
ok("ctx no está vacío", len(ctx_small) > 0)

# ---------------------------------------------------------------------------
# Caso 5 — build_context con focus y transcript
# ---------------------------------------------------------------------------
print("\nCaso 5: build_context con meeting_id (focus + transcript)")

db5 = make_db(os.path.join(tmp_dir, "test5.db"))
id_focus = insert_meeting(db5, "Reunión con transcript",
                          transcript="Este es el transcript completo de la reunión.",
                          minutes={"resumen": "Reunión con transcript"},
                          started_at="2026-06-06 10:00:00")

ctx5, used5 = build_context(db5, "transcript", meeting_id=id_focus)
ok("contiene bloque TRANSCRIPCIÓN", "TRANSCRIPCIÓN (reunión en foco" in ctx5,
   note=f"ctx5[:200]={ctx5[:200]!r}")
ok("id_focus en used_meeting_ids", id_focus in used5)

# ---------------------------------------------------------------------------
# Caso 6 — answer happy path (monkeypatch LLM)
# ---------------------------------------------------------------------------
print("\nCaso 6: answer happy path (stub LLM)")

import core.insights as _ci_mod

captured_messages = []

def stub_chat_memory(messages, **kw):
    captured_messages.clear()
    captured_messages.extend(messages)
    return "[stub] " + str(len(messages))

_ci_mod.chat_memory = stub_chat_memory

from core import assistant as _assistant

db6 = make_db(os.path.join(tmp_dir, "test6.db"))
insert_meeting(db6, "Reunión test6",
               minutes={"resumen": "Test6 resumen"},
               started_at="2026-06-07 10:00:00")

result = _assistant.answer(db6, "¿Qué se habló?")
ok("ok is True", result.get("ok") is True, note=str(result))
ok("answer empieza con [stub]", (result.get("answer") or "").startswith("[stub]"))
ok("messages[0] role == system", captured_messages[0]["role"] == "system")
ok("ASSISTANT_SYSTEM embebido en messages[0]", _assistant.ASSISTANT_SYSTEM[:30] in captured_messages[0]["content"])
ok("último message role == user", captured_messages[-1]["role"] == "user")
ok("último message content == pregunta", captured_messages[-1]["content"] == "¿Qué se habló?")

# ---------------------------------------------------------------------------
# Caso 7 — history cap: máximo 6 turnos
# ---------------------------------------------------------------------------
print("\nCaso 7: history cap (<=6 turnos history + system + user)")

captured_messages7 = []

def stub_chat_memory7(messages, **kw):
    captured_messages7.clear()
    captured_messages7.extend(messages)
    return "[stub7]"

_ci_mod.chat_memory = stub_chat_memory7

history_20 = [
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"turno {i}"}
    for i in range(20)
]

result7 = _assistant.answer(db6, "Pregunta final", history=history_20)
# messages = [system] + max 6 de history + [user actual]
# contamos todos menos system y el user final
history_in_messages = [m for m in captured_messages7 if m["role"] in ("user", "assistant")]
# el último es el user actual, los demás son del history
history_count = len(history_in_messages) - 1
ok("history enviado <= 6 turnos", history_count <= 6,
   note=f"history_count={history_count}")
ok("total messages razonable (<=9)", len(captured_messages7) <= 9,
   note=f"total={len(captured_messages7)}")

# ---------------------------------------------------------------------------
# Caso 8 — answer fail-safe (InsightsUnavailable)
# ---------------------------------------------------------------------------
print("\nCaso 8: answer fail-safe (InsightsUnavailable)")

from core.insights import InsightsUnavailable

def stub_chat_unavailable(messages, **kw):
    raise InsightsUnavailable("caído")

_ci_mod.chat_memory = stub_chat_unavailable

result8 = _assistant.answer(db6, "¿Qué se habló?")
ok("ok is False cuando InsightsUnavailable", result8.get("ok") is False,
   note=str(result8))
ok("trae key error", "error" in result8)
ok("no lanza excepción (fail-safe)", True)  # llegamos aquí = no propagó

# ---------------------------------------------------------------------------
# Caso 9 — PERF: meetings_index se llama exactamente 1 vez por build_context
# ---------------------------------------------------------------------------
print("\nCaso 9: PERF — meetings_index llamado <= 1 vez por build_context")

db9 = make_db(os.path.join(tmp_dir, "test9.db"))
insert_meeting(db9, "Reunión perf1", minutes={"resumen": "Perf alfa"}, started_at="2026-06-08 10:00:00")
insert_meeting(db9, "Reunión perf2", minutes={"resumen": "Perf beta"}, started_at="2026-06-09 10:00:00")

_index_call_count = 0
_original_meetings_index = db9.meetings_index

def _counting_meetings_index(*args, **kwargs):
    global _index_call_count
    _index_call_count += 1
    return _original_meetings_index(*args, **kwargs)

db9.meetings_index = _counting_meetings_index

_index_call_count = 0
_asst_mod = __import__("core.assistant", fromlist=["build_context"])
_asst_mod.build_context(db9, "perf test")

ok("meetings_index llamado <= 1 vez", _index_call_count <= 1,
   note=f"llamadas={_index_call_count}")
ok("meetings_index llamado exactamente 1 vez", _index_call_count == 1,
   note=f"llamadas={_index_call_count}")

# Restaurar
db9.meetings_index = _original_meetings_index

# ---------------------------------------------------------------------------
# Caso 10 — ORDEN: transcript se degrada ANTES de bajar K y de degradar índice
# ---------------------------------------------------------------------------
print("\nCaso 10: ORDEN — transcript degrada primero, índice con resumen sobrevive")

import math as _math

db10 = make_db(os.path.join(tmp_dir, "test10.db"))

TRANSCRIPT_LARGO = "X" * 2000  # transcript largo que fuerce degradado
RESUMEN_UNICO = "ResumenFTS-unico-9z7k"  # resumen que detectamos en ctx para confirmar idx_full
RESUMEN_FOCUS = "ResumenFocus-focus-abc"

# Reunión con FTS hit (la buscada), sin transcript relevante
id_fts = insert_meeting(
    db10, "Reunión FTS relevante",
    transcript="breve",
    minutes={"resumen": RESUMEN_UNICO, "decisiones": ["decision-clave-fts"]},
    started_at="2026-06-10 10:00:00",
)

# Reunión en foco, con transcript largo
id_focus = insert_meeting(
    db10, "Reunión foco con transcript largo",
    transcript=TRANSCRIPT_LARGO,
    minutes={"resumen": RESUMEN_FOCUS},
    started_at="2026-06-11 10:00:00",
)

# Calcular un budget intermedio:
# - idx_full incluirá los 2 resumenes (RESUMEN_UNICO + RESUMEN_FOCUS) -> cabe
# - t_full = 2000 chars -> demasiado con actas -> forzará degradado de transcript
# - pero idx con resumen + acta FTS SÍ debe caber

from core.assistant import _compact_index as _ci, _format_acta as _fa

entries10 = db10.meetings_index()
idx_full_10 = _ci(entries10)
fts_acta_10 = _fa({"resumen": RESUMEN_UNICO, "decisiones": ["decision-clave-fts"]})
focus_acta_10 = _fa({"resumen": RESUMEN_FOCUS})

# Tamaño sin transcript: índice + acta focus + acta FTS
base_size = (
    len(idx_full_10)
    + len(f"\n\n=== ACTA EN FOCO (reunión [{id_focus}] 2026-06-11) ===\n{focus_acta_10}")
    + len(f"\n\n=== ACTAS RELEVANTES ===\n--- Reunión [{id_fts}] 2026-06-10 ---\n{fts_acta_10}")
)
# Budget: permite idx_full + actas, pero NO el transcript largo
budget10 = base_size + 100  # pequeño margen, excluye los 2000 chars del transcript

ctx10, used10 = _asst_mod.build_context(db10, "ResumenFTS-unico-9z7k", meeting_id=id_focus, budget=budget10)

# (a) bloque TRANSCRIPCIÓN no está (fue degradado)
ok("ORDEN(a): TRANSCRIPCIÓN no incluida (degradada primero)",
   "TRANSCRIPCIÓN" not in ctx10,
   note=f"len={len(ctx10)} budget={budget10}")

# (b) resumen del acta FTS relevante sí está
ok("ORDEN(b): acta FTS relevante incluida",
   RESUMEN_UNICO in ctx10,
   note=f"RESUMEN_UNICO en ctx: {'si' if RESUMEN_UNICO in ctx10 else 'no'}")

# (c) el índice conserva resumen (no cayó a idx_no_resumen)
ok("ORDEN(c): índice conserva resumen (idx_full activo)",
   RESUMEN_UNICO in ctx10 and RESUMEN_FOCUS in ctx10,
   note=f"idx_full contiene resumenes: {RESUMEN_FOCUS in ctx10}, {RESUMEN_UNICO in ctx10}")

# ---------------------------------------------------------------------------
# Caso 11 — _budget_chars sigue al backend BATCH (no al global)
# ---------------------------------------------------------------------------
print("\nCaso 11: _budget_chars sigue al backend BATCH resuelto")

from core.assistant import _budget_chars

_BUDGET_ENV_KEYS = (
    "ASSISTANT_CONTEXT_BUDGET_CHARS",
    "INSIGHTS_BACKEND",
    "INSIGHTS_BACKEND_BATCH",
    "INSIGHTS_BACKEND_LIVE",
)


def _with_env(overrides: dict):
    """Aplica overrides de entorno (None = borrar la variable) y devuelve un saved dict."""
    saved = {k: os.environ.get(k) for k in _BUDGET_ENV_KEYS}
    for k in _BUDGET_ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in overrides.items():
        if v is not None:
            os.environ[k] = v
    return saved


def _restore_env(saved: dict):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_saved_env = _with_env({})  # limpiar todas las vars relevantes antes de empezar
try:
    # (a) Sin ninguna var -> backend "groq" -> presupuesto amplio
    _with_env({})
    ok("(a) default (sin env) -> 80000", _budget_chars() == 80000,
       note=str(_budget_chars()))

    # (b) BUG FIX: BATCH=endpoint con global SIN tocar -> presupuesto chico
    #     (antes devolvía 80000 porque solo miraba INSIGHTS_BACKEND global)
    _with_env({"INSIGHTS_BACKEND_BATCH": "endpoint"})
    ok("(b) BATCH=endpoint, global unset -> 18000", _budget_chars() == 18000,
       note=str(_budget_chars()))

    # (c) Override explícito del global a endpoint, BATCH hereda -> chico
    _with_env({"INSIGHTS_BACKEND": "endpoint"})
    ok("(c) global=endpoint, BATCH hereda -> 18000", _budget_chars() == 18000,
       note=str(_budget_chars()))

    # (d) BATCH=groq gana sobre global=endpoint -> presupuesto amplio
    #     (el per-task pisa al global; el budget debe seguir al per-task)
    _with_env({"INSIGHTS_BACKEND": "endpoint", "INSIGHTS_BACKEND_BATCH": "groq"})
    ok("(d) global=endpoint pero BATCH=groq -> 80000", _budget_chars() == 80000,
       note=str(_budget_chars()))

    # (e) BATCH=openrouter (no endpoint) -> presupuesto amplio
    _with_env({"INSIGHTS_BACKEND_BATCH": "openrouter"})
    ok("(e) BATCH=openrouter -> 80000", _budget_chars() == 80000,
       note=str(_budget_chars()))

    # (f) Override ASSISTANT_CONTEXT_BUDGET_CHARS gana sobre todo
    _with_env({"ASSISTANT_CONTEXT_BUDGET_CHARS": "5000",
               "INSIGHTS_BACKEND_BATCH": "endpoint"})
    ok("(f) ASSISTANT_CONTEXT_BUDGET_CHARS=5000 pisa al backend", _budget_chars() == 5000,
       note=str(_budget_chars()))

    # (g) Override inválido -> cae al cálculo por backend (BATCH=endpoint -> 18000)
    _with_env({"ASSISTANT_CONTEXT_BUDGET_CHARS": "no-es-int",
               "INSIGHTS_BACKEND_BATCH": "endpoint"})
    ok("(g) override inválido -> cae al backend (18000)", _budget_chars() == 18000,
       note=str(_budget_chars()))
finally:
    _restore_env(_saved_env)

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
