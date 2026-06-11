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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at
           ON transcriptions(created_at)""",
    ]

    def _init_db(self):
        """Crea la tabla de transcripciones y el índice por fecha si no existen."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                for ddl in self._DDL:
                    conn.execute(ddl)
                conn.commit()
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
    def insert(self, text: str, language: str = None, duration_seconds: float = None, model: str = "whisper-large-v3-turbo") -> int:
        """Inserta una transcripción y retorna su ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO transcriptions (text, language, duration_seconds, model) VALUES (?, ?, ?, ?)",
                (text, language, duration_seconds, model),
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
