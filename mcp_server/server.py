"""Servidor MCP stdio de Vflow: 3 tools read-only sobre la SQLite de reuniones.

Contrato (debatido adversarialmente, ver PROGRESS.md 2026-07-03):
- search_meetings(query="", limit=10) — FTS5 (match="or", bm25) o recientes si query vacía.
- get_minutes(meeting_id) — acta completa (passthrough de minutes_json) + capítulos.
- get_transcript(meeting_id, offset=0, max_chars=15000) — transcript paginado por segmentos.

Garantías:
- Nunca escribe: TranscriptionDB(read_only=True) abre TODAS las conexiones con URI
  mode=ro (garantía a nivel SQLite, no solo de disciplina).
- Si la DB no existe aún, cada tool devuelve un error accionable (no crashea el server).
- stdout es del protocolo MCP: nada de print(); logging va a stderr.

Limitaciones documentadas (del debate):
- El índice FTS lo mantiene el proceso escritor (la app); si la app corre una versión
  vieja sin FTS, lo insertado será invisible a search_meetings hasta el backfill.
- Lector RO sobre WAL requiere directorio escribible por el mismo usuario (caso normal).
"""

import json
import logging
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from db.database import TranscriptionDB

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

mcp = FastMCP("vflow-meetings")

_db = TranscriptionDB(read_only=True)

SEARCH_LIMIT_MAX = 50
TRANSCRIPT_CHARS_MIN = 1000
TRANSCRIPT_CHARS_MAX = 50000


def _parse_json(raw):
    """json.loads tolerante: None/corrupto → None, nunca lanza."""
    try:
        return json.loads(raw or "null")
    except Exception:  # noqa: BLE001
        return None


def _get_meeting_or_fail(meeting_id: int) -> dict:
    meeting = _db.meeting_get(int(meeting_id))
    if not meeting:
        raise ValueError(f"No existe la reunión {meeting_id}. Usa search_meetings para listar las disponibles.")
    return meeting


@mcp.tool()
def search_meetings(query: str = "", limit: int = 10) -> dict:
    """Busca reuniones por texto completo (título, transcript, acta: temas, pendientes,
    decisiones, propuestas). Con query vacía devuelve las reuniones más recientes.
    Devuelve id, title, started_at, duration_seconds y snippet (coincidencias entre «»)."""
    limit = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
    query = (query or "").strip()

    if not query:
        rows = _db.meetings_recent(limit=limit)
        return {"mode": "recent", "results": rows, "count": len(rows)}

    try:
        rows = _db.meetings_search(query, limit=limit, match="or", raise_errors=True)
    except sqlite3.OperationalError as exc:
        raise ValueError(f"La consulta no es compatible con la búsqueda FTS5: {exc}") from exc

    for row in rows:
        snippet = row.get("snippet")
        if isinstance(snippet, str):
            # chr(2)/chr(3) son los delimitadores de coincidencia que emite
            # meetings_search; el '…' de truncado se conserva tal cual.
            row["snippet"] = snippet.replace("\x02", "«").replace("\x03", "»")

    return {"mode": "search", "fts": _db._fts_enabled, "results": rows, "count": len(rows)}


@mcp.tool()
def get_minutes(meeting_id: int) -> dict:
    """Devuelve el acta de una reunión: resumen, decisiones, temas, pendientes,
    propuestas, citas (todo lo que contenga el acta) más los capítulos con timestamp.
    El acta puede incluir además "momentos_destacados" (instantes que el usuario
    marcó en vivo con AltGr+H, con su timestamp y contexto breve) cuando el usuario
    marcó alguno durante la reunión. minutes=null si la reunión aún no tiene acta
    generada. Incluye además "metrics" (dict o null): métricas de conversación
    Yo/Ellos calculadas al terminar la reunión — talk_yo_s/talk_ellos_s, pct_yo/
    pct_ellos, talk_to_listen, longest_monologue_*, turns_approx (aproximado por
    alternancia de speaker en el transcript, no solapes reales), questions_*,
    wpm_* y duration_s. Passthrough de metrics_json; null en reuniones antiguas
    o sin voz detectada."""
    meeting = _get_meeting_or_fail(meeting_id)

    minutes = _parse_json(meeting.get("minutes_json"))
    if not isinstance(minutes, dict):
        minutes = None

    metrics = _parse_json(meeting.get("metrics_json"))
    if not isinstance(metrics, dict):
        metrics = None

    chapters_raw = _parse_json(meeting.get("chapters_json"))
    if isinstance(chapters_raw, dict):
        chapters = chapters_raw.get("capitulos") or None
    elif isinstance(chapters_raw, list):
        chapters = chapters_raw
    else:
        chapters = None

    return {
        "id": meeting["id"],
        "title": meeting.get("title"),
        "started_at": meeting.get("started_at"),
        "duration_seconds": meeting.get("duration_seconds"),
        "minutes": minutes,
        "chapters": chapters,
        "metrics": metrics,
        "has_transcript": bool(meeting.get("transcript")),
    }


@mcp.tool()
def get_transcript(meeting_id: int, offset: int = 0, max_chars: int = 15000) -> dict:
    """Devuelve el transcript de una reunión paginado por segmentos [{t, time, speaker,
    text}] (speaker: "Yo"|"Ellos"). Si next_offset no es null, quedan más segmentos:
    repite la llamada con offset=next_offset."""
    meeting = _get_meeting_or_fail(meeting_id)
    offset = max(0, int(offset))
    max_chars = max(TRANSCRIPT_CHARS_MIN, min(TRANSCRIPT_CHARS_MAX, int(max_chars)))

    segments = _parse_json(meeting.get("segments_json"))
    if isinstance(segments, list) and segments:
        total = len(segments)
        out = []
        used = 0
        i = offset
        while i < total:
            seg = segments[i] if isinstance(segments[i], dict) else {}
            text = str(seg.get("text") or "")
            # Garantía anti-loop: el primer segmento de la página entra siempre,
            # aunque por sí solo exceda max_chars.
            if out and used + len(text) > max_chars:
                break
            out.append({
                "t": seg.get("t"),
                "time": seg.get("time"),
                "speaker": seg.get("speaker"),
                "text": text,
            })
            used += len(text)
            i += 1
        return {
            "id": meeting["id"],
            "title": meeting.get("title"),
            "mode": "segments",
            "total_segments": total,
            "offset": offset,
            "returned": len(out),
            "next_offset": i if i < total else None,
            "segments": out,
        }

    # Fallback filas legacy sin segments_json: rebanado por caracteres.
    text = meeting.get("transcript") or ""
    chunk = text[offset:offset + max_chars]
    next_offset = offset + max_chars if offset + max_chars < len(text) else None
    return {
        "id": meeting["id"],
        "title": meeting.get("title"),
        "mode": "text",
        "total_chars": len(text),
        "offset": offset,
        "next_offset": next_offset,
        "text": chunk,
    }


def main() -> None:
    mcp.run()  # transporte stdio por defecto


if __name__ == "__main__":
    main()
