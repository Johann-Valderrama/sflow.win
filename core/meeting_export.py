"""Export de reuniones a Markdown con frontmatter (contrato Vflow ↔ OPS).

Cada reunión se vuelca a un archivo `.md` legible por humanos Y por agentes (tu OPS
puede indexar la carpeta). El frontmatter es el "contrato": metadatos estables que un
indexador puede leer sin parsear el cuerpo. Los pendientes van como checkboxes markdown
estándar para que sean estado consultable, no texto libre.

Se exporta automáticamente al terminar cada reunión (best-effort) y hay un backfill
para volcar las reuniones ya guardadas en la DB.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def _slug(text: str, maxlen: int = 40) -> str:
    """Convierte un texto en un slug seguro para nombre de archivo."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9áéíóúñ ]+", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    return text[:maxlen] or "reunion"


def _fmt_pendiente(p: dict) -> str:
    """Pendiente como checkbox markdown con responsable/fecha/hora si existen."""
    if isinstance(p, str):
        return f"- [ ] {p}"
    meta = []
    if p.get("responsable"):
        meta.append(f"@{p['responsable']}")
    fh = " ".join(x for x in (p.get("fecha"), p.get("hora")) if x)
    if fh:
        meta.append(f"📅 {fh}")
    suffix = f" ({' · '.join(meta)})" if meta else ""
    return f"- [ ] {p.get('texto', '')}{suffix}"


def _fmt_cita(c: dict) -> str:
    if isinstance(c, str):
        return f"- {c}"
    fh = " ".join(x for x in (c.get("fecha"), c.get("hora")) if x)
    return f"- {c.get('texto', '')}" + (f" — 📅 {fh}" if fh else "")


def _fmt_momento(m: dict) -> str:
    if isinstance(m, str):
        return f"- {m}"
    time = m.get("time", "")
    texto = m.get("texto", "")
    return f"- **{time}** — {texto}" if time else f"- {texto}"


def meeting_markdown(meeting: dict) -> str:
    """Construye el markdown completo de una reunión a partir de su fila de DB."""
    started = meeting.get("started_at") or meeting.get("created_at") or ""
    date = (started or "")[:10]
    dur = meeting.get("duration_seconds") or 0
    dur_min = round(dur / 60, 1)
    minutes = {}
    insights = {}
    try:
        minutes = json.loads(meeting.get("minutes_json") or "{}")
    except Exception:  # noqa: BLE001
        pass
    try:
        insights = json.loads(meeting.get("insights_json") or "{}")
    except Exception:  # noqa: BLE001
        pass

    # --- Frontmatter (contrato OPS) ---
    fm = [
        "---",
        f"id: {meeting.get('id', '')}",
        f"date: {date}",
        f"started_at: {started}",
        f"duration_min: {dur_min}",
        "source: vflow",
        "participants: [Yo, Ellos]",
        "schema_version: 1",
        "---",
        "",
    ]

    body = [f"# Reunión {date}".rstrip(), ""]

    resumen = minutes.get("resumen", "")
    if resumen:
        body += ["## Resumen", resumen, ""]

    momentos = minutes.get("momentos_destacados") or []
    if momentos:
        body += ["## ⭐ Momentos destacados"] + [_fmt_momento(m) for m in momentos] + [""]

    decisiones = minutes.get("decisiones") or []
    if decisiones:
        body += ["## Decisiones"] + [f"- {d}" for d in decisiones] + [""]

    # Pendientes/compromisos: del acta si los hay, si no del análisis en vivo
    pendientes = minutes.get("pendientes") or insights.get("pendientes") or []
    if pendientes:
        body += ["## Pendientes"] + [_fmt_pendiente(p) for p in pendientes] + [""]

    propuestas = minutes.get("propuestas") or insights.get("propuestas") or []
    if propuestas:
        body += ["## Propuestas"] + [
            f"- {p if isinstance(p, str) else p.get('texto', '')}" for p in propuestas
        ] + [""]

    citas = minutes.get("citas") or insights.get("citas") or []
    if citas:
        body += ["## Próximas reuniones"] + [_fmt_cita(c) for c in citas] + [""]

    temas = minutes.get("temas") or []
    if temas:
        body += ["## Temas tratados"] + [
            f"- {t if isinstance(t, str) else t.get('text', '')}" for t in temas
        ] + [""]

    transcript = meeting.get("transcript") or ""
    if transcript:
        body += ["## Transcripción", "", transcript, ""]

    return "\n".join(fm + body)


def export_meeting(meeting: dict, meetings_dir: str) -> "str | None":
    """Escribe el .md de una reunión en meetings_dir. Devuelve la ruta o None si falla."""
    try:
        os.makedirs(meetings_dir, exist_ok=True)
        started = meeting.get("started_at") or meeting.get("created_at") or ""
        date_part = (started or "").replace(":", "").replace(" ", "_")[:15] or "reunion"
        fname = f"{date_part}_reunion-{meeting.get('id', 'x')}.md"
        path = os.path.join(meetings_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(meeting_markdown(meeting))
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Export de reunión a markdown falló: %s", exc)
        return None


def delete_meeting_files(meetings_dir: str, meeting_id) -> int:
    """Elimina el/los .md de una reunión (mantiene la carpeta en sync con la DB)."""
    import glob
    removed = 0
    try:
        for p in glob.glob(os.path.join(meetings_dir, f"*reunion-{meeting_id}.md")):
            os.remove(p)
            removed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo borrar el .md de la reunión %s: %s", meeting_id, exc)
    return removed


def clear_all_files(meetings_dir: str) -> int:
    """Elimina todos los .md de reuniones exportados."""
    import glob
    removed = 0
    try:
        for p in glob.glob(os.path.join(meetings_dir, "*reunion-*.md")):
            os.remove(p)
            removed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron borrar los .md: %s", exc)
    return removed


def export_all(db, meetings_dir: str) -> int:
    """Backfill: exporta todas las reuniones de la DB. Devuelve cuántas escribió."""
    n = 0
    for row in db.meetings_recent(limit=100000):
        full = db.meeting_get(row["id"])
        if full and export_meeting(full, meetings_dir):
            n += 1
    return n
