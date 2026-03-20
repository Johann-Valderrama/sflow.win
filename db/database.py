import sqlite3
from config import DB_PATH


class TranscriptionDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    language TEXT,
                    duration_seconds REAL,
                    model TEXT DEFAULT 'whisper-large-v3-turbo',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at
                ON transcriptions(created_at)
            """)

    def insert(self, text: str, language: str = None, duration_seconds: float = None, model: str = "whisper-large-v3-turbo") -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO transcriptions (text, language, duration_seconds, model) VALUES (?, ?, ?, ?)",
                (text, language, duration_seconds, model),
            )
            return cursor.lastrowid

    def get_recent(self, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]

    def delete_by_id(self, transcription_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE id = ?", (transcription_id,))
            return cursor.rowcount

    def delete_before_date(self, date_str: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) <= date(?)", (date_str,))
            return cursor.rowcount

    def delete_by_date(self, date_str: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) = date(?)", (date_str,))
            return cursor.rowcount

    def delete_since(self, date_str: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE created_at >= ?", (date_str,))
            return cursor.rowcount

    def delete_all(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM transcriptions")
            return cursor.rowcount

    def delete_by_ids(self, ids: list) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(f"DELETE FROM transcriptions WHERE id IN ({placeholders})", ids)
            return cursor.rowcount

    def update_text(self, transcription_id: int, new_text: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("UPDATE transcriptions SET text = ? WHERE id = ?", (new_text, transcription_id))
            return cursor.rowcount
