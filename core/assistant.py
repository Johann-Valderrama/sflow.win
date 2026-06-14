"""Asistente de reuniones — orquestador de retrieval + contexto + LLM para chat sobre reuniones.

Construye el contexto (índice + actas + transcripción) y delega la generación
en insights.chat_memory. Sin importar db: recibe la instancia como parámetro.
"""
import json
import logging
import os

from core import insights

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ASSISTANT_SYSTEM = (
    "Eres un asistente de memoria de las reuniones del usuario. "
    "Tu función es ayudar a recordar, buscar y sintetizar lo que ocurrió en sus reuniones.\n\n"
    "REGLAS ESTRICTAS:\n"
    "- Responde SOLO con base en el CONTEXTO provisto (índice de reuniones, actas y transcripción). "
    "Prohibido usar conocimiento externo o inventar datos, nombres, fechas o compromisos que no estén en el contexto.\n"
    "- Cita siempre la reunión de la que proviene cada dato, indicando su fecha y/o título "
    "(ej.: \"(reunión del 2026-06-10)\").\n"
    "- Si la información solicitada NO está en el contexto, dilo con claridad: "
    "\"No encuentro eso en tus reuniones.\" Nunca inventes ni extrapoles.\n"
    "- SÍ puedes redactar entregables (email de seguimiento, informe, FAQ, resumen ejecutivo) "
    "y traducir, PERO los hechos deben derivar del material real: puedes dar forma y estilo, "
    "nunca inventar contenido factual.\n"
    "- No conviertas contenido descriptivo en pendientes. No inventes responsables ni fechas.\n"
    "- Responde en español salvo que el usuario pida explícitamente otro idioma."
)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def _budget_chars() -> int:
    """Presupuesto de caracteres para el bloque de contexto."""
    env_val = os.getenv("ASSISTANT_CONTEXT_BUDGET_CHARS", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    # El asistente corre sobre el backend "batch" (acta/asistente), no el global: el presupuesto
    # debe seguir a ese backend (p.ej. LM Studio local necesita una ventana más chica).
    backend = insights._resolve_backend("batch")
    return 18000 if backend == "endpoint" else 80000


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_acta(minutes: dict) -> str:
    """Convierte minutes_json (dict) a texto legible y compacto."""
    try:
        parts = []
        resumen = minutes.get("resumen") or ""
        if resumen:
            parts.append(f"Resumen: {resumen}")

        decisiones = minutes.get("decisiones") or []
        if decisiones:
            lines = "\n".join(f"- {d}" for d in decisiones if d)
            parts.append(f"Decisiones:\n{lines}")

        pendientes = minutes.get("pendientes") or []
        if pendientes:
            plines = []
            for p in pendientes:
                if isinstance(p, dict):
                    txt = p.get("texto") or p.get("text") or ""
                    meta = []
                    if p.get("responsable"):
                        meta.append(p["responsable"])
                    fecha_hora = " ".join(filter(None, [p.get("fecha"), p.get("hora")]))
                    if fecha_hora:
                        meta.append(fecha_hora)
                    suffix = f" ({', '.join(meta)})" if meta else ""
                    plines.append(f"- {txt}{suffix}")
                elif isinstance(p, str):
                    plines.append(f"- {p}")
            if plines:
                parts.append("Pendientes:\n" + "\n".join(plines))

        propuestas = minutes.get("propuestas") or []
        if propuestas:
            prolines = []
            for p in propuestas:
                if isinstance(p, dict):
                    prolines.append(f"- {p.get('texto') or p.get('text') or str(p)}")
                elif isinstance(p, str):
                    prolines.append(f"- {p}")
            if prolines:
                parts.append("Propuestas:\n" + "\n".join(prolines))

        temas = minutes.get("temas") or []
        if temas:
            temas_strs = [t if isinstance(t, str) else str(t) for t in temas]
            parts.append("Temas: " + ", ".join(temas_strs))

        citas = minutes.get("citas") or []
        if citas:
            clines = []
            for c in citas:
                if isinstance(c, dict):
                    txt = c.get("texto") or c.get("text") or ""
                    fh = " ".join(filter(None, [c.get("fecha"), c.get("hora")]))
                    suffix = f" ({fh})" if fh else ""
                    clines.append(f"- {txt}{suffix}")
                elif isinstance(c, str):
                    clines.append(f"- {c}")
            if clines:
                parts.append("Citas:\n" + "\n".join(clines))

        return "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.debug("_format_acta error (ignorado): %s", exc)
        return ""


def _compact_index(entries: list, drop_resumen: bool = False, max_rows: int = None) -> str:
    """Genera el índice compacto de reuniones como bloque de texto.

    Recibe la lista ya cargada (no re-consulta la DB).
    """
    if max_rows is not None:
        entries = entries[:max_rows]
    lines = ["=== ÍNDICE DE TUS REUNIONES ==="]
    for e in entries:
        started = (e.get("started_at") or "")[:10]
        title = e.get("title") or ""
        if drop_resumen:
            lines.append(f"[{e['id']}] {started} · {title}")
        else:
            resumen = (e.get("resumen") or "")[:160]
            if resumen:
                lines.append(f"[{e['id']}] {started} · {title} — {resumen}")
            else:
                lines.append(f"[{e['id']}] {started} · {title}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_context(db, message: str, meeting_id=None, budget: int = None) -> tuple:
    """Construye el bloque de contexto para el asistente con truncado en cascada.

    Devuelve (context_str, used_meeting_ids).

    La carga de datos (meetings_index, meeting_get, meetings_search) se realiza
    UNA SOLA VEZ antes del loop de degradado. El orden de degradado es:
    transcript → K de actas FTS → índice.
    """
    budget = budget if budget is not None else _budget_chars()

    # -----------------------------------------------------------------------
    # 1. Carga de datos — UNA sola vez, sin re-consultas en el loop
    # -----------------------------------------------------------------------
    try:
        entries = db.meetings_index()
    except Exception:  # noqa: BLE001
        entries = []

    # Focus meeting
    focus = None
    focus_id = None
    focus_acta_str = ""
    focus_transcript = ""
    if meeting_id is not None:
        try:
            focus = db.meeting_get(meeting_id)
        except Exception:  # noqa: BLE001
            focus = None
        if focus:
            focus_id = focus["id"]
            try:
                focus_minutes = json.loads(focus.get("minutes_json") or "null") or {}
            except Exception:  # noqa: BLE001
                focus_minutes = {}
            focus_acta_str = _format_acta(focus_minutes)
            focus_transcript = focus.get("transcript") or ""

    # FTS search — excluye el focus si coincide
    try:
        fts_results = db.meetings_search(message, limit=3)
    except Exception:  # noqa: BLE001
        fts_results = []

    fts_actas = []  # list of (id, acta_str, meeting_row)
    for r in fts_results:
        if r["id"] == focus_id:
            continue
        try:
            full = db.meeting_get(r["id"])
        except Exception:  # noqa: BLE001
            continue
        if full:
            try:
                fm_minutes = json.loads(full.get("minutes_json") or "null") or {}
            except Exception:  # noqa: BLE001
                fm_minutes = {}
            fm_acta = _format_acta(fm_minutes)
            fts_actas.append((full["id"], fm_acta, full))
        if len(fts_actas) >= 3:
            break

    # -----------------------------------------------------------------------
    # 2. Pre-renderizar variantes de índice EN MEMORIA
    # -----------------------------------------------------------------------
    idx_full = _compact_index(entries)
    idx_no_resumen = _compact_index(entries, drop_resumen=True)
    idx_min = _compact_index(entries, drop_resumen=True, max_rows=40)

    # -----------------------------------------------------------------------
    # 3. Pre-renderizar variantes de transcript (solo si hay focus)
    # -----------------------------------------------------------------------
    if focus and focus_transcript:
        t_full = focus_transcript
        max_tc = max(budget // 3, 500)
        if len(focus_transcript) > max_tc:
            t_trunc = focus_transcript[:max_tc] + "\n…[truncado]"
        else:
            t_trunc = focus_transcript
        t_none = ""
    else:
        t_full = t_trunc = t_none = ""

    # -----------------------------------------------------------------------
    # 4. Función auxiliar para ensamblar un estado
    # -----------------------------------------------------------------------
    def _assemble(idx_str: str, k: int, transcript_str: str):
        parts = [idx_str]
        used_ids = []

        # Acta en foco (SIEMPRE, no se degrada por K)
        if focus and focus_acta_str:
            parts.append(
                f"=== ACTA EN FOCO (reunión [{focus_id}] {(focus.get('started_at') or '')[:10]}) ===\n"
                + focus_acta_str
            )
            used_ids.append(focus_id)

        # Actas relevantes FTS (primeras k)
        if k > 0 and fts_actas:
            fts_block_parts = []
            for fid, facta, frow in fts_actas[:k]:
                if facta:
                    fts_block_parts.append(
                        f"--- Reunión [{fid}] {(frow.get('started_at') or '')[:10]} ---\n"
                        + facta
                    )
                    used_ids.append(fid)
            if fts_block_parts:
                parts.append("=== ACTAS RELEVANTES ===\n" + "\n\n".join(fts_block_parts))

        # Transcripción (si transcript_str no vacío)
        if focus and transcript_str:
            parts.append(
                f"=== TRANSCRIPCIÓN (reunión en foco [{focus_id}]) ===\n" + transcript_str
            )

        ctx = "\n\n".join(parts)
        return ctx, list(dict.fromkeys(used_ids))

    # -----------------------------------------------------------------------
    # 5. Cascada lineal: MENOS degradado → MÁS degradado
    #    Orden: transcript primero → luego bajar K → índice al final
    # -----------------------------------------------------------------------
    estados = [
        # (idx_str, k, transcript_str)
        (idx_full,       3, t_full),   # 1) nada degradado
        (idx_full,       3, t_trunc),  # 2) transcript truncado
        (idx_full,       3, t_none),   # 3) sin transcript
        (idx_full,       2, t_none),   # 4) bajar K=2
        (idx_full,       1, t_none),   # 5) bajar K=1
        (idx_full,       0, t_none),   # 6) bajar K=0
        (idx_no_resumen, 0, t_none),   # 7) índice sin resumen
        (idx_min,        0, t_none),   # 8) índice mínimo
    ]

    for idx_str, k, transcript_str in estados:
        ctx, used_ids = _assemble(idx_str, k, transcript_str)
        if len(ctx) <= budget:
            return ctx, used_ids

    # -----------------------------------------------------------------------
    # 6. Fallback final: idx_min truncado duro a budget
    # -----------------------------------------------------------------------
    ctx_min, _ = _assemble(idx_min, 0, "")
    return ctx_min[:budget], []


# ---------------------------------------------------------------------------
# Reasoning heuristic
# ---------------------------------------------------------------------------

def _needs_reasoning(message: str) -> bool:
    """Heurística nivel-2: ¿la pregunta requiere razonamiento analítico?

    Normaliza el mensaje (minúsculas + quita acentos) y busca disparadores
    analíticos. Devuelve True para preguntas complejas/analíticas, False para
    lookups simples (lista/resume/cuáles/pendientes/quién/cuándo/citas).
    """
    import unicodedata  # noqa: PLC0415
    normalized = unicodedata.normalize("NFD", (message or "").lower())
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")

    _ANALYTICAL_TRIGGERS = (
        "compar", "analiz", "recomien", "recomend", "estrateg",
        "plan de accion", "redacta un plan", "evalu", "pros y contra",
        "ventajas y desventaj", "por que", "deberia", "que harias",
        "sugier", "propon", "prioriz", "implicacion", "consecuencia",
        "que conviene", "ayudame a decidir", "razona",
        "explica por que", "analisis", "diferencia entre",
    )

    return any(trigger in normalized for trigger in _ANALYTICAL_TRIGGERS)


# ---------------------------------------------------------------------------
# Public answer function
# ---------------------------------------------------------------------------

def answer(db, message: str, history=None, meeting_id=None, max_tokens: int = 1024,
           reasoning="auto") -> dict:
    """Responde una pregunta sobre el historial de reuniones usando retrieval + LLM.

    Devuelve {ok: True, answer: str, used_meeting_ids: list, reasoned: bool}
    o {ok: False, error: str, reasoned: bool}.

    El parámetro ``reasoning`` controla el razonamiento extendido:
      - True  → siempre razona
      - False → nunca razona
      - "auto" o None → heurística (_needs_reasoning) decide
    """
    message = (message or "").strip()

    # Resolver reasoning antes de cualquier return (se incluye en todos los paths)
    if reasoning is True:
        resolved = True
    elif reasoning is False:
        resolved = False
    else:
        resolved = _needs_reasoning(message)

    if not message:
        return {"ok": False, "error": "Mensaje vacío", "reasoned": resolved}

    try:
        context, used = build_context(db, message, meeting_id=meeting_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Asistente: error construyendo contexto: %s", exc)
        context = ""
        used = []

    system_content = ASSISTANT_SYSTEM + "\n\n" + context

    messages = [{"role": "system", "content": system_content}]

    # History: últimos 6 turnos, roles válidos, content truncado a 2000 chars
    if history:
        valid_history = [
            {"role": h["role"], "content": str(h.get("content") or "")[:2000]}
            for h in history
            if isinstance(h, dict) and h.get("role") in ("user", "assistant")
        ]
        messages.extend(valid_history[-6:])

    messages.append({"role": "user", "content": message})

    try:
        text = insights.chat_memory(messages, max_tokens=max_tokens, reasoning=resolved)
        return {"ok": True, "answer": text, "used_meeting_ids": used, "reasoned": resolved}
    except insights.InsightsUnavailable as exc:
        return {"ok": False, "error": str(exc) or "Backend de insights no disponible",
                "reasoned": resolved}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Error al consultar el asistente: " + str(exc),
                "reasoned": resolved}
