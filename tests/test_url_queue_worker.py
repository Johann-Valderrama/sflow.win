"""Tests de la unidad 0.4 (F8/F9): worker de la cola de URLs (web/server.py).

Cubre, sin red ni threads reales:

F8 — ``item_id`` no debe "recordar" la iteración anterior si la excepción ocurre
     ANTES de tomar un item nuevo (antes: ``"item_id" in dir()`` conservaba el
     binding viejo del namespace de la función).
F9 — idempotencia post-crash: ``source_queue_id`` + índice único parcial en
     ``transcriptions`` evita una fila duplicada si el proceso muere entre
     ``insert()`` y ``url_queue_set_done()``; el worker trata el
     ``IntegrityError`` resultante como éxito (marca 'done'), no como error.

El loop ``while True`` de ``_url_queue_worker`` se extrajo a
``_process_next_url_item(db) -> bool`` (una iteración, testeable) precisamente
para estos tests — ver docstring de esa función en web/server.py.
"""

import sqlite3

import pytest

from db.database import TranscriptionDB
from web.server import _process_next_url_item


# ---------------------------------------------------------------------------
# (a) source_queue_id + índice único: segunda inserción con el mismo id falla
# ---------------------------------------------------------------------------

def test_insert_duplicado_source_queue_id_lanza_integrity_error(tmp_path):
    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    db.insert(text="hola", source="url", source_queue_id=7)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(text="hola de nuevo", source="url", source_queue_id=7)


def test_insert_sin_source_queue_id_permite_multiples_null(tmp_path):
    """El índice es parcial (WHERE source_queue_id IS NOT NULL): varias filas con
    source_queue_id=None (mic/system) deben poder coexistir sin chocar."""
    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    id1 = db.insert(text="uno")
    id2 = db.insert(text="dos")
    assert id1 != id2


def test_migracion_source_queue_id_idempotente_en_db_vieja(tmp_path):
    """Una DB creada por una versión previa (sin la columna) migra sola al abrir,
    y luego insert() con y sin source_queue_id funcionan."""
    db_path = str(tmp_path / "old.db")
    # DB "vieja": crear solo la tabla base sin source_queue_id (simula pre-migración).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                language TEXT,
                duration_seconds REAL,
                model TEXT DEFAULT 'whisper-large-v3-turbo',
                source TEXT DEFAULT 'mic',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.commit()

    db = TranscriptionDB(db_path=db_path)  # dispara _init_db → migración
    tid_a = db.insert(text="con id", source_queue_id=99)
    tid_b = db.insert(text="sin id")
    row_a = db.get_by_id(tid_a)
    row_b = db.get_by_id(tid_b)
    assert row_a["source_queue_id"] == 99
    assert row_b["source_queue_id"] is None

    # Reabrir (segunda _init_db sobre la misma DB) no debe lanzar (idempotencia).
    db2 = TranscriptionDB(db_path=db_path)
    assert db2 is not None


# ---------------------------------------------------------------------------
# (b) Flujo del worker con mocks — sin red
# ---------------------------------------------------------------------------

def _fake_transcribe_url_ok(monkeypatch, *, text="texto transcrito", title="Mi video"):
    import core.url_transcribe as _ut

    def fake(url, allow_instagram=False, on_progress=None):
        if on_progress:
            on_progress("descargando")
        return {
            "ok": True,
            "title": title,
            "source": "url",
            "method": "audio",
            "language": "es",
            "text": text,
            "duration": 12.3,
            "error": None,
            "error_kind": None,
        }

    monkeypatch.setattr(_ut, "transcribe_url", fake)


def test_worker_exito_normal_marca_done(tmp_path, monkeypatch):
    monkeypatch.setenv("SAVE_HISTORY", "true")
    _fake_transcribe_url_ok(monkeypatch)

    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    item_id = db.url_queue_enqueue("https://youtube.com/watch?v=abc", platform="youtube")

    had_item = _process_next_url_item(db)
    assert had_item is True

    item = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item["status"] == "done"
    assert item["error"] is None

    rows = db.search("texto transcrito")
    assert len(rows) == 1
    assert rows[0]["source_queue_id"] == item_id


def test_worker_integrity_error_en_insert_marca_done_no_error(tmp_path, monkeypatch):
    """F9: si la fila ya existe (insert previo con el mismo source_queue_id, p. ej.
    de un run que murió justo después de insertar pero antes de marcar 'done'),
    el IntegrityError NO debe convertirse en 'error' — se marca 'done' igual."""
    monkeypatch.setenv("SAVE_HISTORY", "true")
    _fake_transcribe_url_ok(monkeypatch, text="texto ya insertado antes")

    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    item_id = db.url_queue_enqueue("https://youtube.com/watch?v=dup", platform="youtube")

    # Simula que un run anterior YA insertó la fila para este item_id antes de
    # morir (crash entre insert() y url_queue_set_done()); el repair de huérfanos
    # al arrancar ya dejó el item en 'pending' de nuevo (mismo estado que
    # url_queue_enqueue produce, así que no hace falta tocarlo más).
    db.insert(text="texto ya insertado antes", source="url", source_queue_id=item_id)

    had_item = _process_next_url_item(db)
    assert had_item is True

    item = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item["status"] == "done"      # NO 'error'
    assert item["error"] is None

    # No hay una SEGUNDA fila duplicada: sigue habiendo solo una con ese texto.
    rows = db.search("texto ya insertado antes")
    assert len(rows) == 1


def test_worker_cola_vacia_devuelve_false(tmp_path):
    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    assert _process_next_url_item(db) is False


def test_worker_error_de_transcripcion_marca_error(tmp_path, monkeypatch):
    import core.url_transcribe as _ut

    def fake(url, allow_instagram=False, on_progress=None):
        return {"ok": False, "error": "no se pudo descargar", "error_kind": "network"}

    monkeypatch.setattr(_ut, "transcribe_url", fake)

    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    item_id = db.url_queue_enqueue("https://youtube.com/watch?v=err", platform="youtube")

    had_item = _process_next_url_item(db)
    assert had_item is True
    item = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item["status"] == "error"
    assert "no se pudo descargar" in item["error"]


# ---------------------------------------------------------------------------
# F8 — excepción ANTES de tomar un item nunca reutiliza el item_id anterior
# ---------------------------------------------------------------------------

def test_f8_excepcion_antes_de_tomar_item_no_toca_item_anterior(tmp_path, monkeypatch):
    """Procesamos un item con éxito (queda 'done'). Luego forzamos que
    ``url_queue_next_pending`` lance ANTES de asignar un nuevo item_id. El item
    ya 'done' NUNCA debe pasar a 'error' (el bug F8 lo hacía vía
    ``"item_id" in dir()``, que conservaba el id de la iteración anterior)."""
    monkeypatch.setenv("SAVE_HISTORY", "true")
    _fake_transcribe_url_ok(monkeypatch)

    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
    item_id = db.url_queue_enqueue("https://youtube.com/watch?v=ok", platform="youtube")

    assert _process_next_url_item(db) is True
    item = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item["status"] == "done"

    def boom():
        raise RuntimeError("fallo simulado de DB antes de tomar el item")

    monkeypatch.setattr(db, "url_queue_next_pending", boom)

    # Esta iteración lanza dentro del try, ANTES de asignar item_id. Con el fix,
    # item_id vale None en ese momento → el except no marca error a NADIE.
    had_item = _process_next_url_item(db)
    assert had_item is True  # la excepción sí cuenta como "hubo actividad"

    # El item previamente 'done' sigue 'done', no fue degradado a 'error'.
    item_after = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item_after["status"] == "done"
    assert item_after["error"] is None


# ---------------------------------------------------------------------------
# Unidad 1.5(a): url_queue_repair_orphans() repara items huérfanos
# ---------------------------------------------------------------------------

def test_url_queue_repair_orphans_repara_items_processing(tmp_path):
    """url_queue_repair_orphans() convierte items 'processing' huérfanos a 'pending'
    con stage limpio, devolviendo el número de filas reparadas."""
    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))

    # Crear un item normal, encolarlo y marcarlo como processing manualmente
    # (simula que el worker murió justo después de marcar processing)
    item_id = db.url_queue_enqueue("https://youtube.com/watch?v=orphan", platform="youtube")
    db.url_queue_set_processing(item_id, "descargando")

    # Verificar estado antes del repair
    item_before = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item_before["status"] == "processing"
    assert item_before["stage"] == "descargando"

    # Reparar huérfanos
    repaired = db.url_queue_repair_orphans()
    assert repaired == 1

    # Verificar que se convirtió a 'pending' con stage = None
    item_after = next(i for i in db.url_queue_list() if i["id"] == item_id)
    assert item_after["status"] == "pending"
    assert item_after["stage"] is None


def test_url_queue_repair_orphans_con_cola_limpia(tmp_path):
    """url_queue_repair_orphans() devuelve 0 si no hay items 'processing'."""
    db = TranscriptionDB(db_path=str(tmp_path / "test.db"))

    # Enqueue un item pero no lo proceses (queda 'pending')
    db.url_queue_enqueue("https://youtube.com/watch?v=clean", platform="youtube")

    repaired = db.url_queue_repair_orphans()
    assert repaired == 0
