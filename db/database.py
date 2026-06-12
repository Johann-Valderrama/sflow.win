import os
import logging
import sqlite3
from datetime import datetime, timedelta
from config import DB_PATH

logger = logging.getLogger(__name__)


class TranscriptionDB:
    """Gestiona el almacenamiento SQLite de transcripciones."""

    def __init__(self, db_path: str = DB_PATH):
        """Inicializa la conexión a la base de datos y crea las tablas si no existen."""
        self.db_path = db_path
        self._init_db()

    _DDL = [
        """CREATE TABLE IF NOT EXISTS transcriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            language TEXT,
            duration_seconds REAL,
            model TEXT DEFAULT 'whisper-large-v3-turbo',
            source TEXT DEFAULT 'mic',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at
           ON transcriptions(created_at)""",
        """CREATE TABLE IF NOT EXISTS dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replace_from TEXT,
            replace_to TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            pinned INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            hit_count INTEGER DEFAULT 0
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_dict_from
           ON dictionary(replace_from) WHERE replace_from IS NOT NULL""",
    ]

    # Migraciones para columnas añadidas en versiones posteriores (ALTER TABLE idempotente)
    _MIGRATIONS = [
        "ALTER TABLE dictionary ADD COLUMN pinned INTEGER DEFAULT 0",
        "ALTER TABLE dictionary ADD COLUMN source TEXT DEFAULT 'manual'",
        "ALTER TABLE dictionary ADD COLUMN hit_count INTEGER DEFAULT 0",
        # v1.2: columna source en transcripciones para registrar fuente de audio
        "ALTER TABLE transcriptions ADD COLUMN source TEXT DEFAULT 'mic'",
    ]

    # DDL adicional para la cola de URLs (Fase 3, paso 2)
    _URL_QUEUE_DDL = """CREATE TABLE IF NOT EXISTS url_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        platform TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        stage TEXT,
        title TEXT,
        error TEXT,
        allow_instagram INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )"""

    def _init_db(self):
        """Crea la tabla de transcripciones y el índice por fecha si no existen."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                for ddl in self._DDL:
                    conn.execute(ddl)
                conn.execute(self._URL_QUEUE_DDL)
                conn.commit()
                # Migraciones seguras: ignorar "duplicate column" si ya existen
                for migration in self._MIGRATIONS:
                    try:
                        conn.execute(migration)
                        conn.commit()
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            raise
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            logger.error("SQLite database corrupt or unreadable: %s", e)
            corrupt_path = self.db_path + ".corrupt"
            try:
                os.rename(self.db_path, corrupt_path)
                logger.warning("Renamed corrupt DB to %s, creating fresh database", corrupt_path)
            except OSError:
                # If rename fails, try removing the corrupt file
                try:
                    os.remove(self.db_path)
                except OSError:
                    pass
            with sqlite3.connect(self.db_path) as conn:
                for ddl in self._DDL:
                    conn.execute(ddl)
                conn.execute(self._URL_QUEUE_DDL)
    def insert(self, text: str, language: str = None, duration_seconds: float = None, model: str = "whisper-large-v3-turbo", source: str = "mic") -> int:
        """Inserta una transcripción y retorna su ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO transcriptions (text, language, duration_seconds, model, source) VALUES (?, ?, ?, ?, ?)",
                (text, language, duration_seconds, model, source),
            )
            return cursor.lastrowid

    def get_recent(self, limit: int = 20) -> list:
        """Retorna las transcripciones más recientes, ordenadas por fecha descendente."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list:
        """Busca transcripciones cuyo texto contenga la consulta (LIKE %query%)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count(self) -> int:
        """Retorna el número total de transcripciones almacenadas."""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]

    def delete_by_id(self, transcription_id: int) -> int:
        """Elimina una transcripción por su ID. Retorna el número de filas eliminadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE id = ?", (transcription_id,))
            return cursor.rowcount

    def delete_before_date(self, date_str: str) -> int:
        """Elimina transcripciones creadas en o antes de la fecha indicada."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) <= date(?)", (date_str,))
            return cursor.rowcount

    def delete_by_date(self, date_str: str) -> int:
        """Elimina transcripciones de una fecha específica (YYYY-MM-DD)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) = date(?)", (date_str,))
            return cursor.rowcount

    def delete_since(self, date_str: str) -> int:
        """Elimina transcripciones creadas desde la fecha indicada en adelante."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE created_at >= ?", (date_str,))
            return cursor.rowcount

    def delete_all(self) -> int:
        """Elimina todas las transcripciones. Retorna el número de filas eliminadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions")
            return cursor.rowcount

    def delete_by_ids(self, ids: list) -> int:
        """Elimina transcripciones por una lista de IDs."""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(f"DELETE FROM transcriptions WHERE id IN ({placeholders})", ids)
            return cursor.rowcount

    def update_text(self, transcription_id: int, new_text: str) -> int:
        """Actualiza el texto de una transcripción existente."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("UPDATE transcriptions SET text = ? WHERE id = ?", (new_text, transcription_id))
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Dictionary CRUD
    # ------------------------------------------------------------------

    def list_dictionary(self) -> list:
        """Retorna todas las entradas del diccionario, ordenadas por pinned desc, hit_count desc, created_at desc."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM dictionary ORDER BY pinned DESC, hit_count DESC, created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def add_dictionary_entry(self, replace_to: str, replace_from: str = None) -> int:
        """Inserta o actualiza una entrada del diccionario (UPSERT por replace_from).

        Si replace_from es None, inserta una nueva entrada de vocabulario.
        Si replace_from ya existe, actualiza replace_to y enabled=1.
        """
        with sqlite3.connect(self.db_path) as conn:
            if replace_from is not None:
                # Intentar UPDATE primero; si no afecta filas, INSERT
                cursor = conn.execute(
                    "UPDATE dictionary SET replace_to=?, enabled=1 WHERE replace_from=?",
                    (replace_to, replace_from),
                )
                if cursor.rowcount > 0:
                    row = conn.execute(
                        "SELECT id FROM dictionary WHERE replace_from=?", (replace_from,)
                    ).fetchone()
                    return row[0] if row else -1
                cursor = conn.execute(
                    "INSERT INTO dictionary (replace_from, replace_to, enabled) VALUES (?, ?, 1)",
                    (replace_from, replace_to),
                )
                return cursor.lastrowid
            else:
                cursor = conn.execute(
                    "INSERT INTO dictionary (replace_from, replace_to, enabled) VALUES (NULL, ?, 1)",
                    (replace_to,),
                )
                return cursor.lastrowid

    def delete_dictionary_entry(self, entry_id: int) -> int:
        """Elimina una entrada del diccionario por ID. Retorna filas eliminadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM dictionary WHERE id = ?", (entry_id,))
            return cursor.rowcount

    def set_dictionary_pinned(self, entry_id: int, pinned: bool) -> int:
        """Fija o desfija una entrada del diccionario. Retorna filas actualizadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE dictionary SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, entry_id),
            )
            return cursor.rowcount

    def increment_dictionary_hits(self, ids: list) -> int:
        """Incrementa hit_count en 1 para cada id de la lista. Retorna filas actualizadas."""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"UPDATE dictionary SET hit_count = hit_count + 1 WHERE id IN ({placeholders})",
                ids,
            )
            return cursor.rowcount

    def set_dictionary_enabled(self, entry_id: int, enabled: bool) -> int:
        """Activa o desactiva una entrada del diccionario. Retorna filas actualizadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE dictionary SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, entry_id),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # URL Queue CRUD (Fase 3, paso 2)
    # ------------------------------------------------------------------

    def url_queue_enqueue(self, url: str, platform: str = None, allow_instagram: bool = False) -> int:
        """Añade una URL a la cola con status 'pending'. Devuelve el id insertado."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO url_queue (url, platform, status, allow_instagram) VALUES (?, ?, 'pending', ?)",
                (url, platform, 1 if allow_instagram else 0),
            )
            return cursor.lastrowid

    def url_queue_next_pending(self) -> dict | None:
        """Devuelve el item 'pending' más antiguo (FIFO) o None si no hay ninguno."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM url_queue WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def url_queue_set_processing(self, item_id: int, stage: str = "iniciando") -> None:
        """Marca un item como 'processing' con una etapa inicial."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'processing', stage = ? WHERE id = ?",
                (stage, item_id),
            )

    def url_queue_update_stage(self, item_id: int, stage: str) -> None:
        """Actualiza la etapa descriptiva de un item 'processing'."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET stage = ? WHERE id = ?",
                (stage, item_id),
            )

    def url_queue_set_done(self, item_id: int, title: str = None) -> None:
        """Marca un item como 'done'."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'done', stage = 'listo', title = ? WHERE id = ?",
                (title, item_id),
            )

    def url_queue_set_error(self, item_id: int, error: str) -> None:
        """Marca un item como 'error' con mensaje."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'error', stage = 'error', error = ? WHERE id = ?",
                (error, item_id),
            )

    def url_queue_list(self) -> list:
        """Devuelve todos los items de la cola ordenados por created_at DESC."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM url_queue ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def url_queue_clear_finished(self) -> int:
        """Elimina filas con status 'done' o 'error'. Devuelve filas eliminadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM url_queue WHERE status IN ('done', 'error')"
            )
            return cursor.rowcount

    def url_queue_cancel_pending(self) -> int:
        """Elimina filas con status 'pending' (no toca 'processing'). Devuelve filas eliminadas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM url_queue WHERE status = 'pending'"
            )
            return cursor.rowcount

    def url_queue_summary(self) -> dict:
        """Devuelve un resumen de conteos por status."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM url_queue GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "processing": 0, "done": 0, "error": 0}
        for status, cnt in rows:
            if status in counts:
                counts[status] = cnt
        return counts

    def prune_older_than(self, days: int) -> int:
        """Elimina transcripciones más antiguas que *days* días.

        Si days <= 0 no hace nada (semántica: conservar siempre).
        Devuelve el número de filas eliminadas.
        """
        if days <= 0:
            return 0
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM transcriptions WHERE date(created_at) < date(?)",
                (cutoff,),
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info("Poda de historial: %d transcripciones eliminadas (anteriores a %s)", deleted, cutoff)
        return deleted
