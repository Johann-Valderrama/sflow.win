"""Asistente de reuniones — orquestador de retrieval + contexto + LLM para chat sobre reuniones.

Construye el contexto (índice + actas + transcripción) y delega la generación
en insights.chat_memory. Sin importar db: recibe la instancia como parámetro.
"""
import json
import logging
import os
import re
import unicodedata

from core import insights

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stopwords (español) — palabras vacías o de marco de pregunta que no aportan
# señal de búsqueda en FTS.  Se compara en minúsculas + sin acentos.
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset = frozenset({
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "y", "o", "u", "a", "ante", "con", "en", "para", "por", "sin", "sobre",
    "que", "que", "cual", "cual", "cuales", "cuales",
    "se", "su", "sus", "lo", "le", "les", "mi", "mis", "tu", "tus",
    "al", "es", "son", "fue", "era", "hay", "me", "te", "nos",
    "algo", "alguna", "algun", "esto", "eso", "esta", "este", "esa", "ese",
    "dijeron", "dijo", "dice", "decir",
    "menciona", "menciono", "mencionaron",
    "hablar", "hablaron", "habla", "hablo",
    "sabe", "saben",
    "cuando", "donde", "como",
})


def _normalize(token: str) -> str:
    """Normaliza un token a minúsculas sin acentos para comparar con _STOPWORDS."""
    nfd = unicodedata.normalize("NFD", token.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _search_terms(message: str) -> str:
    """Extrae tokens de búsqueda del mensaje, descartando stopwords y tokens <2 chars.

    Si no queda ningún token relevante devuelve el mensaje original (fallback seguro).
    """
    raw_tokens = re.split(r"[^\w]+", message, flags=re.UNICODE)
    kept = [t for t in raw_tokens if t and len(t) >= 2 and _normalize(t) not in _STOPWORDS]
    return " ".join(kept) if kept else message


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ASSISTANT_SYSTEM = (
    "Eres un asistente de memoria de las reuniones del usuario. "
    "Tu función es ayudar a recordar, buscar y sintetizar lo que ocurrió en sus reuniones.\n\n"
    "REGLAS ESTRICTAS:\n"
    "- Responde SOLO con base en el CONTEXTO provisto (índice de reuniones, actas y transcripción). "
    "Prohibido usar conocimiento externo o inventar datos, nombres, fechas o compromisos que no estén en el contexto.\n"
    "- Cita siempre la reunión de la que proviene cada dato usando su identificador entre "
    "corchetes EXACTAMENTE como aparece en el contexto (solo el número), p.ej. [10]. Puedes "
    "añadir la fecha para legibilidad (ej.: \"reunión del 2026-06-10 [10]\"), pero NUNCA omitas "
    "los corchetes con el número: son obligatorios en cada cita.\n"
    "- Si la información solicitada NO está en el contexto, dilo con claridad: "
    "\"No encuentro eso en tus reuniones.\" Nunca inventes ni extrapoles.\n"
    "- SÍ puedes redactar entregables (email de seguimiento, informe, FAQ, resumen ejecutivo) "
    "y traducir, PERO los hechos deben derivar del material real: puedes dar forma y estilo, "
    "nunca inventar contenido factual.\n"
    "- No conviertas contenido descriptivo en pendientes. No inventes responsables ni fechas.\n"
    "- Responde en español salvo que el usuario pida explícitamente otro idioma."
)


_SYSTEM_LIVE = (
    "Eres el copiloto del usuario en una reunión EN CURSO. Recibes la transcripción en vivo "
    "hasta este instante (con timestamps mm:ss y hablantes 'Yo'/'Ellos') y el análisis en vivo "
    "parcial (temas/pendientes/propuestas detectados hasta ahora).\n\n"
    "REGLAS ESTRICTAS:\n"
    "- Responde SOLO con base en el CONTEXTO provisto (transcripción en vivo + análisis). "
    "Prohibido usar conocimiento externo o inventar datos, nombres, fechas o compromisos "
    "que no estén en el contexto.\n"
    "- Cita el momento del que proviene cada dato usando su timestamp mm:ss tal como aparece "
    "en la transcripción (p.ej. \"a los 12:30 acordaron…\"). NO cites identificadores de "
    "reunión entre corchetes: esta reunión aún no está guardada y no tiene acta.\n"
    "- La reunión sigue en curso: el contexto está incompleto por definición. Si algo no se "
    "ha dicho todavía, dilo con claridad (\"eso no se ha mencionado hasta ahora\"). "
    "Prefiere callar antes que inventar.\n"
    "- SÍ puedes redactar entregables derivados de lo dicho (resumen parcial, lista de puntos, "
    "borrador de email o mensaje), PERO los hechos deben salir de la transcripción real: "
    "puedes dar forma y estilo, nunca inventar contenido factual.\n"
    "- No conviertas contenido descriptivo en pendientes. No inventes responsables ni fechas.\n"
    "- Responde en español salvo que el usuario pida explícitamente otro idioma."
)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def _budget_chars() -> int:
    """Presupuesto de caracteres para el bloque de contexto.

    Delega en ``insights.budget_chars`` (helper compartido con generate_minutes/
    generate_chapters) para que un mismo backend tenga el mismo presupuesto en
    toda la app. El asistente corre sobre el backend "batch" (acta/asistente).
    """
    return insights.budget_chars(task="batch")


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_time_suffix(item: dict) -> str:
    """Sufijo ' (mm:ss)' si el item trae un 't' snapeado (segundos); '' si no."""
    t = item.get("t")
    if t is None:
        return ""
    try:
        secs = int(float(t))
    except (TypeError, ValueError):
        return ""
    return f" ({secs // 60}:{secs % 60:02d})"


def _format_acta(minutes: dict) -> str:
    """Convierte minutes_json (dict) a texto legible y compacto."""
    try:
        parts = []
        resumen = minutes.get("resumen") or ""
        if resumen:
            parts.append(f"Resumen: {resumen}")

        bant = minutes.get("bant")
        if isinstance(bant, dict):
            labels = {"budget": "Presupuesto", "authority": "Autoridad",
                      "need": "Necesidad", "timeline": "Plazo"}
            blines = [f"- {labels.get(k, k)}: {v}" for k, v in bant.items() if v]
            if blines:
                parts.append("BANT:\n" + "\n".join(blines))

        notas = minutes.get("notas_usuario") or []
        if notas:
            nlines = []
            for n in notas:
                if isinstance(n, dict):
                    line = f"- {n.get('time', '')} {n.get('nota', '')}".strip()
                    ctx = (n.get("contexto") or "").strip()
                    if ctx:
                        line += f" (IA: {ctx})"
                    nlines.append(line)
                elif isinstance(n, str):
                    nlines.append(f"- {n}")
            if nlines:
                parts.append("Notas del usuario:\n" + "\n".join(nlines))

        momentos = minutes.get("momentos_destacados") or []
        if momentos:
            mlines = []
            for m in momentos:
                if isinstance(m, dict):
                    mlines.append(f"- {m.get('time', '')} {m.get('texto', '')}".strip())
                elif isinstance(m, str):
                    mlines.append(f"- {m}")
            if mlines:
                parts.append("Momentos destacados:\n" + "\n".join(mlines))

        decisiones = minutes.get("decisiones") or []
        if decisiones:
            dlines = []
            for d in decisiones:
                if isinstance(d, dict):
                    txt = d.get("texto") or d.get("text") or ""
                    if txt:
                        dlines.append(f"- {txt}{_format_time_suffix(d)}")
                elif isinstance(d, str) and d:
                    dlines.append(f"- {d}")
            if dlines:
                parts.append("Decisiones:\n" + "\n".join(dlines))

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
                    plines.append(f"- {txt}{suffix}{_format_time_suffix(p)}")
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

    # FTS search — excluye el focus si coincide.
    # Usa _search_terms para filtrar stopwords y match="or" para tolerar lenguaje natural.
    try:
        fts_results = db.meetings_search(_search_terms(message), limit=3, match="or")
    except Exception:  # noqa: BLE001
        fts_results = []

    # Mapa id → snippet limpio (sin marcadores \x02/\x03) para incluir en contexto
    _fts_snippets: dict = {}
    for r in fts_results:
        raw_snip = r.get("snippet") or ""
        _fts_snippets[r["id"]] = raw_snip.replace("\x02", "").replace("\x03", "")

    fts_actas = []  # list of (id, acta_str, meeting_row, snippet_str)
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
            fts_actas.append((full["id"], fm_acta, full, _fts_snippets.get(full["id"], "")))
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
            for fid, facta, frow, fsnippet in fts_actas[:k]:
                if facta:
                    block = (
                        f"--- Reunión [{fid}] {(frow.get('started_at') or '')[:10]} ---\n"
                        + facta
                    )
                    if fsnippet:
                        block += f"\nFragmento relevante: {fsnippet}"
                    fts_block_parts.append(block)
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
# Live context builder (unidad 2.3 — chat "Esta reunión" EN VIVO)
# ---------------------------------------------------------------------------

def _format_live_insights(ins: dict) -> str:
    """Formatea el Insight Stream plano (temas/pendientes/propuestas/citas) como texto compacto.

    Devuelve "" si no hay nada detectado aún (insights vacíos → el bloque se omite).
    """
    ins = ins or {}
    parts = []

    temas = [str(t) for t in (ins.get("temas") or []) if t]
    if temas:
        parts.append("Temas detectados: " + ", ".join(temas))

    plines = []
    for p in ins.get("pendientes") or []:
        if isinstance(p, dict):
            txt = (p.get("texto") or "").strip()
            if not txt:
                continue
            meta = []
            if p.get("responsable"):
                meta.append(str(p["responsable"]))
            fecha_hora = " ".join(filter(None, [p.get("fecha"), p.get("hora")]))
            if fecha_hora:
                meta.append(fecha_hora)
            plines.append(f"- {txt}" + (f" ({', '.join(meta)})" if meta else ""))
        elif isinstance(p, str) and p.strip():
            plines.append(f"- {p.strip()}")
    if plines:
        parts.append("Pendientes detectados:\n" + "\n".join(plines))

    prolines = []
    for p in ins.get("propuestas") or []:
        txt = ((p.get("texto") or "") if isinstance(p, dict) else str(p or "")).strip()
        if txt:
            prolines.append(f"- {txt}")
    if prolines:
        parts.append("Propuestas detectadas:\n" + "\n".join(prolines))

    clines = []
    for c in ins.get("citas") or []:
        if isinstance(c, dict):
            txt = (c.get("texto") or "").strip()
            if not txt:
                continue
            fh = " ".join(filter(None, [c.get("fecha"), c.get("hora")]))
            clines.append(f"- {txt}" + (f" ({fh})" if fh else ""))
        elif isinstance(c, str) and c.strip():
            clines.append(f"- {c.strip()}")
    if clines:
        parts.append("Próximas citas detectadas:\n" + "\n".join(clines))

    return "\n".join(parts)


def _briefing_block_for_live() -> str:
    """Bloque del briefing OPS (unidad 7.1) para el chat en vivo, o "" si no aplica.

    Gate de privacidad: en modo proactivo "silent" (pantalla compartida) NO se
    inyecta, aunque el archivo exista. Fail-safe: cualquier error al resolver el
    modo o leer el briefing devuelve "" (nunca rompe el chat en vivo).
    """
    try:
        from core import proactive as _proactive  # noqa: PLC0415 — evita ciclo de import
        if _proactive.get_mode() == "silent":
            return ""
        from core import ops_briefing as _ops_briefing  # noqa: PLC0415
        return _ops_briefing.get_briefing()
    except Exception as exc:  # noqa: BLE001
        logger.debug("build_context_live: briefing OPS omitido (%s)", type(exc).__name__)
        return ""


def build_context_live(message: str, budget: int = None, meeting=None) -> tuple:
    """Construye el contexto del chat sobre la reunión EN CURSO. Contrato NUEVO (unidad 2.3).

    A diferencia de build_context (que lee la DB), aquí la fuente es el singleton
    MEETING en RAM: se toma UN snapshot atómico (bajo su lock) de transcript +
    insights y todo se arma sobre esa foto — el estado puede seguir mutando (o la
    reunión terminar) mientras se genera la respuesta, sin releer.

    Recorte al presupuesto: se PRIORIZA lo más reciente del transcript — se corta
    por el PRINCIPIO, nunca el final — y se antepone el aviso
    "[transcript truncado: se muestran los últimos X minutos]".

    ``message`` forma parte del contrato (paridad con build_context) aunque hoy
    no se usa para retrieval: el transcript vivo entra completo o recortado.
    ``meeting`` permite inyectar una sesión en tests; por defecto usa MEETING.

    Devuelve (context_str, meta) con meta = {active, segment_count, truncated,
    empty, shown_seconds, briefing_included}.

    Briefing OPS (unidad 7.1): si OPS_BRIEFING_PATH está configurado y el modo
    proactivo no es "silent", su bloque entra al prefix fijo (mismo trato que
    ins_block) con la MISMA válvula de sacrificio: si el presupuesto es tan chico
    que meterlo se comería el transcript reciente, se descarta (prioridad:
    transcript > briefing). ``meta["briefing_included"]`` refleja si entró de
    verdad — lo usa answer_live para decidir si añade la instrucción condicional.
    """
    budget = budget if budget is not None else _budget_chars()
    if meeting is None:
        from core.meeting import MEETING as meeting  # noqa: PLC0415 — import perezoso: evita ciclo y carga de audio en import

    snap = meeting.snapshot()
    segments = snap.get("segments") or []
    insights_snap = snap.get("insights") or {}

    header = "=== REUNIÓN EN CURSO (transcripción en vivo, aún sin acta) ==="
    if snap.get("started_at"):
        header += f"\nIniciada: {snap['started_at']}"

    ins_str = _format_live_insights(insights_snap)
    ins_block = ("=== ANÁLISIS EN VIVO (parcial) ===\n" + ins_str) if ins_str else ""
    briefing_block = _briefing_block_for_live()

    meta = {
        "active": bool(snap.get("active")),
        "segment_count": len(segments),
        "truncated": False,
        "empty": not segments,
        "shown_seconds": 0,
        "briefing_included": False,
    }

    if not segments:
        parts = [header, "(Aún no hay intervenciones transcritas.)"]
        if ins_block:
            parts.append(ins_block)
        if briefing_block:
            parts.append(briefing_block)
            meta["briefing_included"] = True
        return "\n\n".join(parts)[:budget], meta

    lines = [f"[{s['time']} {s['speaker']}] {s['text']}" for s in segments]

    def _fixed_prefix(with_insights: bool, with_briefing: bool) -> str:
        parts = [header]
        if with_insights and ins_block:
            parts.append(ins_block)
        if with_briefing and briefing_block:
            parts.append(briefing_block)
        return "\n\n".join(parts) + "\n\n=== TRANSCRIPCIÓN EN VIVO ===\n"

    prefix = _fixed_prefix(True, True)
    avail = budget - len(prefix)
    if avail < 500 and briefing_block:
        # Presupuesto minúsculo: el transcript vivo manda; se sacrifica PRIMERO el
        # briefing (prioridad transcript > briefing > insights, mismo espíritu que
        # el sacrificio de ins_block: nunca dejar avail negativo).
        prefix = _fixed_prefix(True, False)
        avail = budget - len(prefix)
    else:
        meta["briefing_included"] = bool(briefing_block)
    if avail < 500 and ins_block:
        # Aún minúsculo tras soltar el briefing: se sacrifica también el análisis en vivo.
        prefix = _fixed_prefix(False, False)
        avail = budget - len(prefix)
        meta["briefing_included"] = False

    total = sum(len(ln) + 1 for ln in lines)
    kept_start = 0
    if total > avail:
        # Recorte por el PRINCIPIO: acumular desde el final hasta agotar el presupuesto,
        # reservando espacio para el aviso de truncado.
        notice_reserve = 80
        acc = notice_reserve
        kept_start = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            if acc + len(lines[i]) + 1 > avail:
                break
            acc += len(lines[i]) + 1
            kept_start = i
        if kept_start >= len(lines):
            # Ni la última línea cabe entera: conservarla igual (el final NUNCA se
            # pierde); el clamp final la recorta por su principio si hace falta.
            kept_start = len(lines) - 1
        meta["truncated"] = True

    kept_lines = lines[kept_start:]
    if meta["truncated"]:
        shown_s = max(segments[-1]["t"] - segments[kept_start]["t"], 0.0)
        meta["shown_seconds"] = int(shown_s)
        shown_min = max(int(round(shown_s / 60.0)), 1)
        kept_lines = [f"[transcript truncado: se muestran los últimos {shown_min} minutos]"] + kept_lines

    ctx = prefix + "\n".join(kept_lines)
    if len(ctx) > budget:
        # Última defensa (presupuesto absurdo < 1 línea): truncar duro por el PRINCIPIO
        # del transcript, preservando el final.
        head_len = len(prefix)
        tail = ctx[head_len:][-(max(budget - head_len, 0)):]
        ctx = (prefix + tail)[:budget] if budget > head_len else ctx[-budget:]
        meta["truncated"] = True
    return ctx, meta


def _template_live_extra(meeting=None) -> str:
    """1 línea de plumbing: si la reunión activa tiene una plantilla con
    ``live_extra``, la devuelve para añadirla al system del chat en vivo.
    Fail-safe: "" ante cualquier problema (nunca rompe el chat en vivo)."""
    try:
        if meeting is None:
            from core.meeting import MEETING as meeting  # noqa: PLC0415
        from core import meeting_templates as _templates  # noqa: PLC0415
        return _templates.get(meeting.get_template()).get("live_extra") or ""
    except Exception:  # noqa: BLE001
        return ""


_EMPTY_LIVE_ANSWER = (
    "Aún no hay contenido transcrito de la reunión en curso. "
    "En cuanto haya intervenciones podré responder sobre lo hablado."
)


def answer_live(message: str, history=None, max_tokens: int = 1024,
                reasoning="auto", meeting=None) -> dict:
    """Responde una pregunta sobre la reunión EN CURSO (snapshot en RAM, no DB).

    MISMO shape de retorno que answer(): {ok, answer, used_meeting_ids, reasoned}
    (más "live": True y "truncated"). used_meeting_ids siempre [] — la reunión
    viva no tiene id todavía.

    Side case documentado — transcript vacío: NO se llama al LLM; se devuelve una
    respuesta fija (determinista, gratis: sin contenido no hay nada que el LLM
    pueda aportar sin inventar).
    """
    message = (message or "").strip()

    if reasoning is True:
        resolved = True
    elif reasoning is False:
        resolved = False
    else:
        resolved = _needs_reasoning(message)

    if not message:
        return {"ok": False, "error": "Mensaje vacío", "reasoned": resolved, "live": True}

    try:
        context, meta = build_context_live(message, meeting=meeting)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Asistente (vivo): error construyendo contexto: %s", exc)
        return {"ok": False, "error": "No se pudo leer el estado de la reunión en curso.",
                "reasoned": resolved, "live": True}

    if meta.get("empty"):
        return {"ok": True, "answer": _EMPTY_LIVE_ANSWER, "used_meeting_ids": [],
                "reasoned": False, "live": True, "empty": True, "truncated": False}

    system_content = _SYSTEM_LIVE
    live_extra = _template_live_extra(meeting)
    if live_extra:
        system_content += "\n\n" + live_extra
    identity_line = insights.user_identity_line()
    if identity_line:
        system_content += "\n\n" + identity_line
    if meta.get("briefing_included"):
        # Instrucción condicional (unidad 7.1): solo se añade cuando el briefing
        # se incluyó DE VERDAD en el contexto (no sacrificado por la válvula, no
        # apagado, no gateado por modo silent).
        system_content += (
            "\n\nUsa el CONTEXTO DEL USUARIO solo para CONECTAR lo hablado con "
            "proyectos/compromisos del usuario; nunca inventes hechos del "
            "briefing que no vengan al caso."
        )
    system_content += "\n\n" + context
    messages = [{"role": "system", "content": system_content}]

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
        return {"ok": True, "answer": text, "used_meeting_ids": [], "reasoned": resolved,
                "live": True, "truncated": bool(meta.get("truncated"))}
    except insights.InsightsUnavailable as exc:
        return {"ok": False, "error": str(exc) or "Backend de insights no disponible",
                "reasoned": resolved, "live": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Error al consultar el asistente: " + str(exc),
                "reasoned": resolved, "live": True}


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

    system_content = ASSISTANT_SYSTEM
    identity_line = insights.user_identity_line()
    if identity_line:
        system_content += "\n\n" + identity_line
    system_content += "\n\n" + context

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


# ---------------------------------------------------------------------------
# Chat de memoria CROSS-reunión + entregables (unidad 5.3)
# ---------------------------------------------------------------------------
# Chatea sobre un CONJUNTO de reuniones (seleccionadas a mano o por tema vía FTS)
# y produce entregables (email de seguimiento, informe, resumen de acuerdos).
# Diseño cerrado en debate adversarial — puntos NO renegociables:
#   1. Contexto = ACTAS (minutes_json), NUNCA transcripts completos.
#   2. Cap duro de 12 reuniones candidatas por RECENCIA, resuelto ANTES de armar
#      el contexto; lo que no entra por cap o por presupuesto se DECLARA excluido
#      (nunca se descarta en silencio).
#   3. ids explícitos tienen prioridad sobre fts_query (nunca ambos a la vez).
#   4. Cada acta en el contexto lleva SIEMPRE fecha + título + meeting_id visibles.

_MULTI_MAX_CANDIDATES = 12

MULTI_SYSTEM = (
    "Eres un asistente de memoria que trabaja sobre un CONJUNTO de reuniones seleccionado "
    "por el usuario (no todo el historial). Recibes las ACTAS (nunca transcripciones "
    "completas) de esas reuniones, cada una con fecha, título e identificador [N].\n\n"
    "REGLAS ESTRICTAS:\n"
    "- Responde SOLO con base en las actas provistas. Prohibido usar conocimiento externo o "
    "inventar datos, nombres, fechas o compromisos que no estén en el contexto.\n"
    "- Cita SIEMPRE la fecha y la reunión de origen de cada afirmación (p. ej. \"el "
    "2026-06-10 [10] se acordó...\").\n"
    "- Si la respuesta no está en las actas provistas, dilo con claridad: \"No encuentro eso "
    "en las reuniones seleccionadas.\" Nunca inventes ni extrapoles.\n"
    "- No conviertas contenido descriptivo en pendientes. No inventes responsables ni fechas.\n"
    "- Responde en español salvo que el usuario pida explícitamente otro idioma."
)


# Entregables (unidad 5.3): cada uno es una instrucción EXTRA al system prompt, sobre
# la misma base MULTI_SYSTEM (los hechos siguen debiendo salir de las actas). Sin
# template ("None") el chat queda libre, sin instrucción de formato.
DELIVERABLE_TEMPLATES: dict = {
    "email_seguimiento": {
        "label": "Email de seguimiento",
        "system_extra": (
            "\n\nENTREGABLE SOLICITADO: redacta un EMAIL DE SEGUIMIENTO listo para copiar y "
            "enviar. Primera línea 'Asunto: ...', luego el cuerpo. Incluye los acuerdos/"
            "decisiones y los próximos pasos relevantes, citando la fecha/reunión de origen "
            "de cada uno. Tono profesional y conciso."
        ),
    },
    "informe": {
        "label": "Informe ejecutivo",
        "system_extra": (
            "\n\nENTREGABLE SOLICITADO: redacta un INFORME EJECUTIVO con estas secciones, "
            "cada una con su encabezado: Contexto, Decisiones, Pendientes por responsable, "
            "Riesgos. Cita la fecha/reunión de origen en cada afirmación relevante. Si una "
            "sección no tiene contenido real en las actas, dilo explícitamente en vez de "
            "inventar."
        ),
    },
    "resumen_acuerdos": {
        "label": "Resumen de acuerdos",
        "system_extra": (
            "\n\nENTREGABLE SOLICITADO: redacta una LISTA COMPACTA de acuerdos/decisiones, "
            "un ítem por línea, cada uno con su fecha y la reunión de origen. Sin relleno ni "
            "introducción."
        ),
    },
}


def _resolve_multi_candidates(db, meeting_ids=None, fts_query=None) -> tuple:
    """Resuelve las reuniones candidatas para ``answer_multi``.

    Prioridad: ``meeting_ids`` explícitos SIEMPRE ganan sobre ``fts_query`` (nunca se
    combinan). Devuelve ``(candidates, excluded)``:

      - ``candidates``: lista de hasta ``_MULTI_MAX_CANDIDATES`` dicts
        ``{id, title, started_at, minutes}``, ordenados por RECENCIA (más reciente
        primero). El cap se aplica ANTES de que ``_assemble_multi_context`` arme el
        bloque de texto — nunca se cargan al prompt más de 12 actas.
      - ``excluded``: lista de dicts ``{id, titulo, motivo}`` para cada reunión pedida
        que quedó fuera — id no encontrado ("no encontrada"), sin acta real
        ("sin acta") o recortada por el cap de recencia ("cap de recencia (máx 12)").
        El presupuesto de caracteres se aplica DESPUÉS, en ``_assemble_multi_context``.
    """
    excluded: list = []
    raw_ids: list = []

    if meeting_ids:
        seen = set()
        for mid in meeting_ids:
            try:
                mid_int = int(mid)
            except (TypeError, ValueError):
                continue
            if mid_int in seen:
                continue
            seen.add(mid_int)
            raw_ids.append(mid_int)
    elif fts_query:
        try:
            results = db.meetings_search(_search_terms(fts_query), limit=30, match="or")
        except Exception:  # noqa: BLE001
            results = []
        raw_ids = [r["id"] for r in results]

    if not raw_ids:
        return [], []

    resolved: list = []
    for mid in raw_ids:
        try:
            row = db.meeting_get(mid)
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            excluded.append({"id": mid, "titulo": "", "motivo": "no encontrada"})
            continue
        title = row.get("title") or ""
        try:
            minutes = json.loads(row.get("minutes_json") or "null")
        except Exception:  # noqa: BLE001
            minutes = None
        if not minutes:
            excluded.append({"id": mid, "titulo": title, "motivo": "sin acta"})
            continue
        resolved.append({
            "id": mid,
            "title": title,
            "started_at": row.get("started_at") or row.get("created_at") or "",
            "minutes": minutes,
        })

    resolved.sort(key=lambda c: c["started_at"] or "", reverse=True)

    candidates = resolved[:_MULTI_MAX_CANDIDATES]
    for extra in resolved[_MULTI_MAX_CANDIDATES:]:
        excluded.append({
            "id": extra["id"], "titulo": extra["title"],
            "motivo": "cap de recencia (máx 12)",
        })

    return candidates, excluded


def _assemble_multi_context(candidates: list, budget: int) -> tuple:
    """Ensambla el bloque de texto de actas para el chat multi-reunión.

    ``candidates`` debe venir YA ordenado por recencia (más reciente primero, ver
    ``_resolve_multi_candidates``). Se agregan actas en ese orden hasta agotar
    ``budget``; las que no caben se declaran excluidas (motivo "presupuesto") en
    vez de truncarse a la mitad. Cada bloque incluido lleva SIEMPRE visibles
    fecha + título + ``[meeting_id]``.

    Devuelve ``(context_str, included_meta, excluded_meta)`` con
    ``included_meta``/``excluded_meta`` = listas de ``{id, titulo, fecha}`` /
    ``{id, titulo, motivo}``.
    """
    header = "=== ACTAS DE REUNIONES SELECCIONADAS ==="
    used = len(header)
    parts = [header]
    included_meta: list = []
    excluded_meta: list = []

    for c in candidates:
        fecha = (c.get("started_at") or "")[:10] or "sin fecha"
        acta_str = _format_acta(c.get("minutes") or {}) or "(acta sin contenido)"
        title = c.get("title") or "sin título"
        block = f"\n\n--- Reunión [{c['id']}] {fecha} · {title} ---\n{acta_str}"
        if used + len(block) > budget:
            excluded_meta.append({"id": c["id"], "titulo": c.get("title") or "", "motivo": "presupuesto"})
            continue
        parts.append(block)
        used += len(block)
        included_meta.append({"id": c["id"], "titulo": c.get("title") or "", "fecha": fecha})

    return "".join(parts), included_meta, excluded_meta


def answer_multi(db, message: str, meeting_ids=None, fts_query=None, history=None,
                 template: "str | None" = None, max_tokens: int = 1536,
                 reasoning="auto") -> dict:
    """Chatea sobre un CONJUNTO de reuniones (selección manual o por tema) y, si se pide
    un ``template``, produce un entregable (ver ``DELIVERABLE_TEMPLATES``).

    ``meeting_ids`` (lista de ids) tiene prioridad SIEMPRE sobre ``fts_query`` (texto de
    búsqueda FTS) — nunca se combinan. Si no se pasa ninguno de los dos, devuelve error.

    Devuelve ``{ok: True, answer, reuniones_incluidas, excluidas, template_usado,
    reasoned}`` o ``{ok: False, error, reasoned}``. ``reuniones_incluidas``/``excluidas``
    documentan qué reunión entró de verdad al contexto y cuál quedó fuera (y por qué:
    "no encontrada", "sin acta", "cap de recencia (máx 12)" o "presupuesto").
    """
    message = (message or "").strip()

    if reasoning is True:
        resolved_reasoning = True
    elif reasoning is False:
        resolved_reasoning = False
    else:
        resolved_reasoning = _needs_reasoning(message)

    if not message:
        return {"ok": False, "error": "Mensaje vacío", "reasoned": resolved_reasoning}

    if template is not None and template not in DELIVERABLE_TEMPLATES:
        return {"ok": False, "error": f"Plantilla desconocida: {template}",
                "reasoned": resolved_reasoning}

    if not meeting_ids and not fts_query:
        return {"ok": False, "error": "Indica meeting_ids o fts_query",
                "reasoned": resolved_reasoning}

    try:
        candidates, excluded_resolve = _resolve_multi_candidates(
            db, meeting_ids=meeting_ids, fts_query=fts_query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Asistente multi: error resolviendo candidatos: %s", exc)
        candidates, excluded_resolve = [], []

    budget = _budget_chars()
    context, included_meta, excluded_budget = _assemble_multi_context(candidates, budget)
    excluded_meta = excluded_resolve + excluded_budget

    system_content = MULTI_SYSTEM
    if template:
        system_content += DELIVERABLE_TEMPLATES[template]["system_extra"]
    identity_line = insights.user_identity_line()
    if identity_line:
        system_content += "\n\n" + identity_line
    system_content += "\n\n" + context

    messages = [{"role": "system", "content": system_content}]

    if history:
        valid_history = [
            {"role": h["role"], "content": str(h.get("content") or "")[:2000]}
            for h in history
            if isinstance(h, dict) and h.get("role") in ("user", "assistant")
        ]
        messages.extend(valid_history[-6:])

    messages.append({"role": "user", "content": message})

    try:
        text = insights.chat_memory(messages, max_tokens=max_tokens, reasoning=resolved_reasoning)
        return {
            "ok": True, "answer": text,
            "reuniones_incluidas": included_meta, "excluidas": excluded_meta,
            "template_usado": template, "reasoned": resolved_reasoning,
        }
    except insights.InsightsUnavailable as exc:
        return {"ok": False, "error": str(exc) or "Backend de insights no disponible",
                "reasoned": resolved_reasoning}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Error al consultar el asistente: " + str(exc),
                "reasoned": resolved_reasoning}
