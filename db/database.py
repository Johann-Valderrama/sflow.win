import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from config import DB_PATH

logger = logging.getLogger(__name__)


class TranscriptionDB:
    """Gestiona el almacenamiento SQLite de transcripciones."""

    def __init__(self, db_path: str = DB_PATH, read_only: bool = False):
        """Inicializa la conexión a la base de datos y crea las tablas si no existen.

        read_only=True: para procesos lectores externos (p. ej. el servidor MCP).
        No ejecuta DDL, migraciones ni backfill FTS (la DB la administra la app);
        todas las conexiones se abren con URI mode=ro, así que este objeto no puede
        escribir aunque un bug lo intente. Si el archivo no existe, cada conexión
        falla con FileNotFoundError (SQLite en mode=ro no crea el archivo).
        """
        self.db_path = db_path
        self.read_only = read_only
        self._fts_enabled = False
        self._fts_tokenizer = None
        self._fts_probed = False
        # Unidad 1.2: bandera pública para que el caller (main.py, que sí conoce
        # el tray de Qt) pueda avisar al usuario cuando _init_db tuvo que
        # recuperarse de una DB corrupta. Este módulo no conoce Qt, así que solo
        # deja constancia en el objeto; nunca muestra UI por sí mismo.
        self.recovered_from_corruption = False
        self.corrupt_backup_path = None
        if not read_only:
            self._init_db()

    def _connect(self, check_same_thread: bool = True) -> sqlite3.Connection:
        """Abre una conexión con busy_timeout (5s). En read_only usa URI mode=ro.

        En modo lector, la disponibilidad de FTS se detecta en la primera conexión
        (el backfill/creación del índice solo corre en el proceso escritor, por lo
        que el índice puede estar desincronizado hasta que la app lo reconstruya).
        """
        if self.read_only:
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(
                    f"No existe la base de datos: {self.db_path} "
                    "(abre Vflow al menos una vez para crearla)"
                )
            uri = "file:" + self.db_path.replace("\\", "/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0,
                                   check_same_thread=check_same_thread)
            if not self._fts_probed:
                try:
                    self._fts_enabled = bool(conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name='meetings_fts'"
                    ).fetchone()[0])
                    self._fts_probed = True
                except sqlite3.Error:
                    pass
            return conn
        return sqlite3.connect(self.db_path, timeout=5.0,
                               check_same_thread=check_same_thread)

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
        # Modo reunión: insights (Insight Stream) y acta (minutes) por reunión
        "ALTER TABLE meetings ADD COLUMN insights_json TEXT",
        "ALTER TABLE meetings ADD COLUMN minutes_json TEXT",
        # Línea de tiempo de momentos clave (capítulos etiquetados por LLM)
        "ALTER TABLE meetings ADD COLUMN chapters_json TEXT",
        # Momentos destacados marcados por el usuario en vivo (AltGr+H)
        "ALTER TABLE meetings ADD COLUMN highlights_json TEXT",
        # Notas rápidas capturadas en vivo durante la reunión (unidad 2.1)
        "ALTER TABLE meetings ADD COLUMN notes_json TEXT",
        # Feedback ✓/✗ del único push en vivo (pendientes), para el bucle de mejora de prompts (unidad 2.2)
        "ALTER TABLE meetings ADD COLUMN feedback_json TEXT",
        # Métricas de conversación Yo/Ellos: talk-time, pct, monólogo, WPM, preguntas (unidad 3.1)
        "ALTER TABLE meetings ADD COLUMN metrics_json TEXT",
        # Plantilla por tipo de reunión (unidad 4.3): general/ventas/one_on_one/clase
        "ALTER TABLE meetings ADD COLUMN template TEXT",
        # Detecciones proactivas entregadas en vivo (unidad 5.1): rastro para el
        # bucle de mejora de prompts junto a feedback_json
        "ALTER TABLE meetings ADD COLUMN detections_json TEXT",
        # Texto crudo pre-diccionario (unidad 6.2): NULL si coincide con el texto
        # final (nada que mostrar en el toggle "ver crudo").
        "ALTER TABLE transcriptions ADD COLUMN raw_text TEXT",
        # Idempotencia del worker de la cola de URLs (F9, unidad 0.4): referencia al
        # item de url_queue que originó esta fila. NULL para transcripciones que no
        # vienen de la cola (mic/system/individual). El índice único parcial (creado
        # más abajo, DESPUÉS de esta migración porque depende de la columna) impide
        # una segunda inserción para el mismo item si el proceso muere entre el
        # insert() y el url_queue_set_done() y el item se re-encola y re-procesa.
        "ALTER TABLE transcriptions ADD COLUMN source_queue_id INTEGER",
    ]

    # Índice que depende de una columna creada por migración (no puede vivir en
    # _DDL: se ejecuta DESPUÉS del loop de _MIGRATIONS). CREATE INDEX IF NOT EXISTS
    # ya es idempotente por sí solo.
    _POST_MIGRATION_INDEXES = [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transcriptions_source_queue "
        "ON transcriptions(source_queue_id) WHERE source_queue_id IS NOT NULL",
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

    # DDL para reuniones (modo reunión: captura dual mic+loopback)
    _MEETINGS_DDL = """CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        transcript TEXT,
        segments_json TEXT,
        insights_json TEXT,
        minutes_json TEXT,
        duration_seconds REAL,
        started_at TEXT,
        template TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )"""

    def _init_db(self):
        """Crea la tabla de transcripciones y el índice por fecha si no existen."""
        try:
            conn = self._connect()
            try:
                # WAL incondicional (sticky en el archivo): permite lectores
                # concurrentes (servidor MCP) sin "database is locked" mientras
                # la app escribe. Corre en cada arranque para cubrir DBs creadas
                # por versiones previas en journal_mode=delete.
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as e:
                    logger.warning("No se pudo activar WAL: %s", e)
                for ddl in self._DDL:
                    conn.execute(ddl)
                conn.execute(self._URL_QUEUE_DDL)
                conn.execute(self._MEETINGS_DDL)
                conn.commit()
                # Migraciones seguras: ignorar "duplicate column" si ya existen
                for migration in self._MIGRATIONS:
                    try:
                        conn.execute(migration)
                        conn.commit()
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            raise
                # Índices que dependen de columnas recién migradas (deben correr
                # después de que la columna exista).
                for index_ddl in self._POST_MIGRATION_INDEXES:
                    conn.execute(index_ddl)
                conn.commit()
                # FTS5: creación con cascada de tokenizer
                self._fts_enabled = False
                self._fts_tokenizer = None
                for tokenize_opt in [
                    "tokenize='unicode61 remove_diacritics 2'",
                    "tokenize='unicode61 remove_diacritics 1'",
                    "tokenize='unicode61'",
                ]:
                    try:
                        conn.execute(
                            f"CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5("
                            f"title, transcript, resumen, temas, pendientes, decisiones, propuestas, "
                            f"{tokenize_opt})"
                        )
                        conn.commit()
                        self._fts_enabled = True
                        self._fts_tokenizer = tokenize_opt
                        break
                    except sqlite3.OperationalError:
                        continue
                if not self._fts_enabled:
                    logger.warning("FTS5 no disponible en este SQLite — la búsqueda de reuniones usará LIKE")
                else:
                    self._fts_backfill(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            logger.error("SQLite database corrupt or unreadable: %s", e)
            corrupt_path = self.db_path + ".corrupt"
            self.recovered_from_corruption = True
            try:
                os.rename(self.db_path, corrupt_path)
                logger.warning("Renamed corrupt DB to %s, creating fresh database", corrupt_path)
                self.corrupt_backup_path = corrupt_path
            except OSError:
                # If rename fails, try removing the corrupt file
                try:
                    os.remove(self.db_path)
                except OSError:
                    pass
            with self._connect() as conn:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
                for ddl in self._DDL:
                    conn.execute(ddl)
                conn.execute(self._URL_QUEUE_DDL)
                conn.execute(self._MEETINGS_DDL)
                conn.commit()
                # Migraciones también aquí: la DB fresca recién creada por _DDL no
                # tiene las columnas ALTER TABLE de _MIGRATIONS (bug preexistente,
                # expuesto por la migración raw_text de la unidad 6.2 — sin este
                # bloque, insert() con raw_text fallaba tras una recuperación por
                # corrupción con "no such column: raw_text").
                for migration in self._MIGRATIONS:
                    try:
                        conn.execute(migration)
                        conn.commit()
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            raise
                for index_ddl in self._POST_MIGRATION_INDEXES:
                    conn.execute(index_ddl)
                conn.commit()

    def insert(self, text: str, language: str = None, duration_seconds: float = None, model: str = "whisper-large-v3-turbo", source: str = "mic", raw_text: str = None, source_queue_id: int = None) -> int:
        """Inserta una transcripción y retorna su ID.

        raw_text: texto crudo pre-diccionario (unidad 6.2). None cuando no hay
        crudo disponible o coincide con `text` (el caller ya hace esa
        comparación antes de llamar; aquí se guarda tal cual llega).
        source_queue_id: id del item de url_queue que originó esta fila (F9,
        unidad 0.4). None para transcripciones que no vienen de la cola. El
        índice único parcial ``idx_transcriptions_source_queue`` impide una
        segunda fila para el mismo item (idempotencia post-crash del worker).
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO transcriptions (text, language, duration_seconds, model, source, raw_text, source_queue_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text, language, duration_seconds, model, source, raw_text, source_queue_id),
            )
            return cursor.lastrowid

    def get_recent(self, limit: int = 20) -> list:
        """Retorna las transcripciones más recientes, ordenadas por fecha descendente."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list:
        """Busca transcripciones cuyo texto contenga la consulta (LIKE %query%)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count(self) -> int:
        """Retorna el número total de transcripciones almacenadas."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]

    def stats(self) -> dict:
        """Agregados baratos para las metric cards del dashboard.

        Solo consultas indexadas por created_at (nada de conteo de palabras: sería
        full-scan). Convención de tiempo: created_at es UTC naive (igual que asume
        el front con `new Date(t.created_at + 'Z')`), por eso date('now') sin
        modificador de zona.
        """
        with self._connect() as conn:
            today = conn.execute(
                "SELECT COUNT(*) FROM transcriptions WHERE date(created_at) = date('now')"
            ).fetchone()[0]
            week_seconds = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) FROM transcriptions "
                "WHERE created_at >= datetime('now', '-7 days')"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]
            dict_active = conn.execute(
                "SELECT COUNT(*) FROM dictionary WHERE enabled = 1"
            ).fetchone()[0]
            return {
                "today_count": today,
                "week_seconds": round(week_seconds or 0, 1),
                "total_count": total,
                "dict_active": dict_active,
            }

    def delete_by_id(self, transcription_id: int) -> int:
        """Elimina una transcripción por su ID. Retorna el número de filas eliminadas."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE id = ?", (transcription_id,))
            return cursor.rowcount

    def delete_before_date(self, date_str: str) -> int:
        """Elimina transcripciones creadas en o antes de la fecha indicada."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) <= date(?)", (date_str,))
            return cursor.rowcount

    def delete_by_date(self, date_str: str) -> int:
        """Elimina transcripciones de una fecha específica (YYYY-MM-DD)."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE date(created_at) = date(?)", (date_str,))
            return cursor.rowcount

    def delete_since(self, date_str: str) -> int:
        """Elimina transcripciones creadas desde la fecha indicada en adelante."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transcriptions WHERE created_at >= ?", (date_str,))
            return cursor.rowcount

    def delete_all(self) -> int:
        """Elimina todas las transcripciones. Retorna el número de filas eliminadas."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transcriptions")
            return cursor.rowcount

    def delete_by_ids(self, ids: list) -> int:
        """Elimina transcripciones por una lista de IDs."""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM transcriptions WHERE id IN ({placeholders})", ids)
            return cursor.rowcount

    def update_text(self, transcription_id: int, new_text: str) -> int:
        """Actualiza el texto de una transcripción existente."""
        with self._connect() as conn:
            cursor = conn.execute("UPDATE transcriptions SET text = ? WHERE id = ?", (new_text, transcription_id))
            return cursor.rowcount

    def get_by_id(self, transcription_id: int) -> dict | None:
        """Devuelve una transcripción por ID (incluye raw_text) o None si no existe."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?", (transcription_id,)
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Dictionary CRUD
    # ------------------------------------------------------------------

    def list_dictionary(self) -> list:
        """Retorna todas las entradas del diccionario, ordenadas por pinned desc, hit_count desc, created_at desc."""
        with self._connect() as conn:
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
        with self._connect() as conn:
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

    def list_suggested_dictionary(self) -> list:
        """Retorna las entradas del diccionario con source='suggested' (bandeja de revisión)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM dictionary WHERE source = 'suggested' ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def dictionary_from_exists(self, replace_from: str) -> bool:
        """True si ya existe una entrada con ese replace_from (case-insensitive)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM dictionary WHERE replace_from IS NOT NULL AND lower(replace_from) = lower(?) LIMIT 1",
                (replace_from,),
            ).fetchone()
            return row is not None

    def add_suggested_entry(self, replace_from: str, replace_to: str) -> int | None:
        """Inserta una sugerencia de par (deshabilitada, source='suggested').

        Devuelve el id insertado, o None si ya existe una entrada con ese
        replace_from (dedup case-insensitive vía UNIQUE INDEX + comprobación previa).
        """
        if self.dictionary_from_exists(replace_from):
            return None
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO dictionary (replace_from, replace_to, enabled, source) "
                    "VALUES (?, ?, 0, 'suggested')",
                    (replace_from, replace_to),
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Carrera con otra inserción concurrente del mismo replace_from
                return None

    def accept_suggested_entry(self, entry_id: int) -> int:
        """Acepta una sugerencia: enabled=1, source pasa a 'manual' (ya no es sugerencia).

        Devuelve filas actualizadas (0 si no existe o no era 'suggested').
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE dictionary SET enabled = 1, source = 'manual' "
                "WHERE id = ? AND source = 'suggested'",
                (entry_id,),
            )
            return cursor.rowcount

    def count_suggested_dictionary(self) -> int:
        """Cuenta las sugerencias pendientes (para el badge del panel)."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM dictionary WHERE source = 'suggested'"
            ).fetchone()[0]

    def delete_dictionary_entry(self, entry_id: int) -> int:
        """Elimina una entrada del diccionario por ID. Retorna filas eliminadas."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM dictionary WHERE id = ?", (entry_id,))
            return cursor.rowcount

    def set_dictionary_pinned(self, entry_id: int, pinned: bool) -> int:
        """Fija o desfija una entrada del diccionario. Retorna filas actualizadas."""
        with self._connect() as conn:
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
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE dictionary SET hit_count = hit_count + 1 WHERE id IN ({placeholders})",
                ids,
            )
            return cursor.rowcount

    def set_dictionary_enabled(self, entry_id: int, enabled: bool) -> int:
        """Activa o desactiva una entrada del diccionario. Retorna filas actualizadas."""
        with self._connect() as conn:
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
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO url_queue (url, platform, status, allow_instagram) VALUES (?, ?, 'pending', ?)",
                (url, platform, 1 if allow_instagram else 0),
            )
            return cursor.lastrowid

    def url_queue_next_pending(self) -> dict | None:
        """Devuelve el item 'pending' más antiguo (FIFO) o None si no hay ninguno."""
        with self._connect(check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM url_queue WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def url_queue_set_processing(self, item_id: int, stage: str = "iniciando") -> None:
        """Marca un item como 'processing' con una etapa inicial."""
        with self._connect(check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'processing', stage = ? WHERE id = ?",
                (stage, item_id),
            )

    def url_queue_update_stage(self, item_id: int, stage: str) -> None:
        """Actualiza la etapa descriptiva de un item 'processing'."""
        with self._connect(check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET stage = ? WHERE id = ?",
                (stage, item_id),
            )

    def url_queue_set_done(self, item_id: int, title: str = None) -> None:
        """Marca un item como 'done'."""
        with self._connect(check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'done', stage = 'listo', title = ? WHERE id = ?",
                (title, item_id),
            )

    def url_queue_set_error(self, item_id: int, error: str) -> None:
        """Marca un item como 'error' con mensaje."""
        with self._connect(check_same_thread=False) as conn:
            conn.execute(
                "UPDATE url_queue SET status = 'error', stage = 'error', error = ? WHERE id = ?",
                (error, item_id),
            )

    def url_queue_list(self) -> list:
        """Devuelve todos los items de la cola ordenados por created_at DESC."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM url_queue ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def url_queue_clear_finished(self) -> int:
        """Elimina filas con status 'done' o 'error'. Devuelve filas eliminadas."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM url_queue WHERE status IN ('done', 'error')"
            )
            return cursor.rowcount

    def url_queue_cancel_pending(self) -> int:
        """Elimina filas con status 'pending' (no toca 'processing'). Devuelve filas eliminadas."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM url_queue WHERE status = 'pending'"
            )
            return cursor.rowcount

    def url_queue_repair_orphans(self) -> int:
        """Repara items 'processing' huérfanos de un crash anterior.

        Items con status='processing' sin proceso activo (crash con el worker a
        mitad de un item) se reencolan como 'pending'. Devuelve filas reparadas.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE url_queue SET status = 'pending', stage = NULL WHERE status = 'processing'"
            )
            return cursor.rowcount

    def url_queue_summary(self) -> dict:
        """Devuelve un resumen de conteos por status."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM url_queue GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "processing": 0, "done": 0, "error": 0}
        for status, cnt in rows:
            if status in counts:
                counts[status] = cnt
        return counts

    # ------------------------------------------------------------------
    # Reuniones (modo reunión)
    # ------------------------------------------------------------------

    def meeting_insert(self, title: str, transcript: str, segments_json: str,
                       duration_seconds: float, started_at: str = None,
                       insights_json: str = None, minutes_json: str = None,
                       chapters_json: str = None, highlights_json: str = None,
                       notes_json: str = None, feedback_json: str = None,
                       metrics_json: str = None, template: str = None,
                       detections_json: str = None) -> int:
        """Inserta una reunión finalizada y devuelve su id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO meetings (title, transcript, segments_json, insights_json, "
                "minutes_json, chapters_json, highlights_json, notes_json, feedback_json, "
                "metrics_json, duration_seconds, started_at, template, detections_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, transcript, segments_json, insights_json, minutes_json,
                 chapters_json, highlights_json, notes_json, feedback_json,
                 metrics_json, duration_seconds, started_at, template, detections_json),
            )
            meeting_id = cursor.lastrowid
            self._fts_index_meeting(conn, meeting_id, {
                "title": title,
                "transcript": transcript,
                "minutes_json": minutes_json,
                "insights_json": insights_json,
            })
            return meeting_id

    def meetings_recent(self, limit: int = 20) -> list:
        """Devuelve las reuniones más recientes (sin el transcript completo, para listar)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, duration_seconds, started_at, created_at, metrics_json "
                "FROM meetings ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def meeting_get(self, meeting_id: int) -> dict | None:
        """Devuelve una reunión completa (con transcript y segmentos) por id."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            return dict(row) if row else None

    def meeting_set_chapters(self, meeting_id: int, chapters_json: str) -> int:
        """Actualiza los capítulos de una reunión. Devuelve rowcount."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE meetings SET chapters_json=? WHERE id=?",
                (chapters_json, meeting_id),
            )
            conn.commit()
            return cursor.rowcount

    def meeting_delete(self, meeting_id: int) -> int:
        """Elimina una reunión por id. Devuelve filas eliminadas."""
        with self._connect() as conn:
            rowcount = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,)).rowcount
            if self._fts_enabled:
                try:
                    conn.execute("DELETE FROM meetings_fts WHERE rowid=?", (meeting_id,))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("FTS delete error (meeting %s): %s", meeting_id, exc)
            return rowcount

    def meetings_delete_all(self) -> int:
        """Elimina todas las reuniones. Devuelve filas eliminadas."""
        with self._connect() as conn:
            rowcount = conn.execute("DELETE FROM meetings").rowcount
            if self._fts_enabled:
                try:
                    conn.execute("DELETE FROM meetings_fts")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("FTS clear error: %s", exc)
            return rowcount

    def meetings_prune_older_than(self, days: int) -> int:
        """Elimina reuniones (actas y transcripts) más antiguas que *days* días.

        Si days <= 0 no hace nada (semántica: conservar siempre — misma
        convención que ``prune_older_than`` para transcripciones).

        A diferencia de las transcripciones, ``meetings_fts`` se mantiene A
        MANO (sin triggers SQLite): un DELETE monolítico sobre `meetings`
        dejaría entradas huérfanas en el índice FTS y ``meetings_search``
        seguiría devolviendo hits de reuniones ya borradas. Por eso se sigue
        el mismo patrón que ``meeting_delete``/``meetings_delete_all``: primero
        se listan los ids a borrar, se limpia su entrada FTS una por una
        (orden FTS-antes-que-filas), y solo al final se borran las filas de
        `meetings`. Devuelve el número de filas eliminadas.
        """
        if days <= 0:
            return 0
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM meetings WHERE date(created_at) < date(?)",
                    (cutoff,),
                ).fetchall()
            ]
            if not ids:
                return 0
            if self._fts_enabled:
                for meeting_id in ids:
                    try:
                        conn.execute("DELETE FROM meetings_fts WHERE rowid=?", (meeting_id,))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("FTS delete error (meeting %s): %s", meeting_id, exc)
            cursor = conn.execute(
                "DELETE FROM meetings WHERE date(created_at) < date(?)",
                (cutoff,),
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info("Poda de reuniones: %d reuniones eliminadas (anteriores a %s)", deleted, cutoff)
        return deleted

    # ------------------------------------------------------------------
    # FTS5 — búsqueda full-text en reuniones
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_str_or_dict_list(lst, key="texto") -> str:
        """Aplana una lista de (str | dict con 'texto'/'text') a string concatenado.

        Nunca lanza excepción: ante cualquier dato raro devuelve "".
        """
        if not isinstance(lst, list):
            return ""
        parts = []
        for item in lst:
            try:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get(key) or item.get("text") or ""))
            except Exception:  # noqa: BLE001
                pass
        return " ".join(p for p in parts if p)

    def _meeting_fts_fields(self, row: dict) -> tuple:
        """Extrae los campos de texto plano para el índice FTS de una reunión.

        row: dict con keys title, transcript, minutes_json, insights_json (str|None).
        Devuelve: (title, transcript, resumen, temas, pendientes, decisiones, propuestas)
        """
        title = row.get("title") or ""
        transcript = row.get("transcript") or ""

        try:
            minutes = json.loads(row.get("minutes_json") or "null") or {}
        except Exception:  # noqa: BLE001
            minutes = {}
        try:
            insights = json.loads(row.get("insights_json") or "null") or {}
        except Exception:  # noqa: BLE001
            insights = {}

        resumen = minutes.get("resumen") or ""

        # temas
        if minutes.get("temas"):
            temas = self._flatten_str_or_dict_list(minutes["temas"])
        else:
            temas = self._flatten_str_or_dict_list(insights.get("temas") or [], key="text")

        # pendientes
        if minutes.get("pendientes"):
            pendientes = self._flatten_str_or_dict_list(minutes["pendientes"])
        else:
            pendientes = self._flatten_str_or_dict_list(insights.get("pendientes") or [])

        decisiones = self._flatten_str_or_dict_list(minutes.get("decisiones") or [])
        propuestas = self._flatten_str_or_dict_list(minutes.get("propuestas") or [])

        return (str(title), str(transcript), str(resumen), temas, pendientes, decisiones, propuestas)

    def _fts_index_meeting(self, conn, meeting_id: int, row: dict) -> None:
        """Upsert de una reunión en el índice FTS. Usa la conexión abierta del llamante."""
        if not self._fts_enabled:
            return
        try:
            fields = self._meeting_fts_fields(row)
            conn.execute("DELETE FROM meetings_fts WHERE rowid=?", (meeting_id,))
            conn.execute(
                "INSERT INTO meetings_fts(rowid, title, transcript, resumen, temas, "
                "pendientes, decisiones, propuestas) VALUES (?,?,?,?,?,?,?,?)",
                (meeting_id, *fields),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FTS index error (meeting %s): %s", meeting_id, exc)

    def _fts_backfill(self, conn) -> None:
        """Reconstruye el índice FTS si está desincronizado con la tabla meetings."""
        if not self._fts_enabled:
            return
        try:
            # No basta comparar counts: una reunión presente en meetings pero ausente
            # del índice (o una fila FTS huérfana) puede dejar counts iguales y aun así
            # estar desincronizada. Detectamos por rowid faltante/huérfano (LEFT JOIN).
            missing = conn.execute(
                "SELECT count(*) FROM meetings m "
                "LEFT JOIN meetings_fts f ON f.rowid = m.id WHERE f.rowid IS NULL"
            ).fetchone()[0]
            orphans = conn.execute(
                "SELECT count(*) FROM meetings_fts f "
                "LEFT JOIN meetings m ON m.id = f.rowid WHERE m.id IS NULL"
            ).fetchone()[0]
            if missing == 0 and orphans == 0:
                return
            logger.info("FTS backfill: faltantes=%d huérfanos=%d — reconstruyendo índice…", missing, orphans)
            conn.execute("DELETE FROM meetings_fts")
            rows = conn.execute(
                "SELECT id, title, transcript, minutes_json, insights_json FROM meetings"
            ).fetchall()
            for row in rows:
                self._fts_index_meeting(conn, row[0], {
                    "title": row[1],
                    "transcript": row[2],
                    "minutes_json": row[3],
                    "insights_json": row[4],
                })
            conn.commit()
            logger.info("FTS backfill completado: %d reuniones indexadas", len(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("FTS backfill error: %s", exc)

    def meetings_index(self, limit: int = 200) -> list:
        """Devuelve índice liviano de reuniones (id, title, started_at, resumen) para el Asistente de reuniones."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, started_at, minutes_json FROM meetings ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                minutes = json.loads(row["minutes_json"] or "null") or {}
            except Exception:  # noqa: BLE001
                minutes = {}
            result.append({
                "id": row["id"],
                "title": row["title"],
                "started_at": row["started_at"],
                "resumen": minutes.get("resumen") or "",
            })
        return result

    def meetings_search(self, query: str, limit: int = 50, match: str = "and",
                        raise_errors: bool = False) -> list:
        """Busca reuniones por texto completo (FTS5) o LIKE si FTS no está disponible.

        Devuelve lista de dicts con: id, title, started_at, duration_seconds, snippet
        y (solo en la ruta FTS) score = bm25 proyectado (negativo; más negativo =
        mejor match). Campo ADITIVO: los llamadores previos siguen funcionando y
        deben leerlo con .get("score") (el fallback LIKE no lo incluye).

        match="and"  → los tokens se unen con AND (comportamiento por defecto, para la
                       caja de búsqueda del dashboard: busca reuniones que contengan
                       TODOS los tokens).
        match="or"   → los tokens se unen con OR (para el Asistente de reuniones, que
                       recibe preguntas en lenguaje natural donde no todos los tokens
                       son términos clave).
        raise_errors → si True, un OperationalError de FTS se propaga en vez de
                       devolver [] (permite al llamante distinguir "sin resultados"
                       de "la consulta rompió FTS"; lo usa el servidor MCP).
        """
        query = (query or "").strip()
        if not query:
            return []

        if self._fts_enabled:
            # Sanitizar: dividir en tokens, escapar comillas, prefijo de término
            tokens = [t for t in query.split() if t]
            if not tokens:
                return []
            escaped_tokens = ['"' + t.replace('"', '""') + '"*' for t in tokens]
            if match == "or":
                fts_query = " OR ".join(escaped_tokens)
            else:
                fts_query = " ".join(escaped_tokens)

            try:
                with self._connect() as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        SELECT f.rowid AS id,
                               m.title AS title,
                               m.started_at AS started_at,
                               m.duration_seconds AS duration_seconds,
                               snippet(meetings_fts, -1, char(2), char(3), '…', 12) AS snippet,
                               bm25(meetings_fts, 8.0, 1.0, 6.0, 5.0, 3.0, 4.0, 2.0) AS score
                        FROM meetings_fts f
                        JOIN meetings m ON m.id = f.rowid
                        WHERE meetings_fts MATCH ?
                        ORDER BY bm25(meetings_fts, 8.0, 1.0, 6.0, 5.0, 3.0, 4.0, 2.0)
                        LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                    return [dict(row) for row in rows]
            except sqlite3.OperationalError as exc:
                logger.debug("FTS query error (query=%r): %s", query, exc)
                if raise_errors:
                    raise
                return []
        else:
            # Fallback LIKE
            like = f"%{query}%"
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, title, started_at, duration_seconds, "
                    "substr(transcript,1,200) AS snippet "
                    "FROM meetings WHERE title LIKE ? OR transcript LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
                return [dict(row) for row in rows]

    def prune_older_than(self, days: int) -> int:
        """Elimina transcripciones más antiguas que *days* días.

        Si days <= 0 no hace nada (semántica: conservar siempre).
        Devuelve el número de filas eliminadas.
        """
        if days <= 0:
            return 0
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM transcriptions WHERE date(created_at) < date(?)",
                (cutoff,),
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info("Poda de historial: %d transcripciones eliminadas (anteriores a %s)", deleted, cutoff)
        return deleted
