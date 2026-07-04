"""Tests para la unidad 6.2: dictado crudo/Undo + diccionario sugerido.

Cubre:
(a) migración idempotente de raw_text (correr _init_db dos veces)
(b) insert con raw_text y sin él
(c) transcribe() captura crudo cuando hay reemplazos del diccionario, None si no
(d) generación de sugerencias desde el diff de una corrección manual
(e) dedup de sugerencias (mismo replace_from case-insensitive)
(f) aceptar/descartar sugerencia
(g) los llamadores existentes de transcribe()/translate() no se rompen (firma por defecto sigue siendo str)
"""

import io
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# (a) Migración idempotente + (b) insert con/sin raw_text
# ---------------------------------------------------------------------------
class TestRawTextMigration:
    def test_migration_idempotent(self, tmp_path):
        """Correr _init_db dos veces no debe lanzar (duplicate column se ignora)."""
        from db.database import TranscriptionDB
        db_path = str(tmp_path / "test.db")
        db1 = TranscriptionDB(db_path=db_path)
        # Segunda inicialización sobre la misma DB: debe ser un no-op seguro.
        db2 = TranscriptionDB(db_path=db_path)
        assert db2 is not None

    def test_insert_with_raw_text(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        tid = db.insert(text="hola Johann", raw_text="hola Johan")
        row = db.get_by_id(tid)
        assert row["text"] == "hola Johann"
        assert row["raw_text"] == "hola Johan"

    def test_insert_without_raw_text(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        tid = db.insert(text="hola mundo")
        row = db.get_by_id(tid)
        assert row["text"] == "hola mundo"
        assert row["raw_text"] is None

    def test_get_by_id_missing(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        assert db.get_by_id(9999) is None


# ---------------------------------------------------------------------------
# (c) transcribe() captura el crudo pre-diccionario
# ---------------------------------------------------------------------------
class TestTranscribeReturnRaw:
    @pytest.fixture(autouse=True)
    def _reset_dictionary_cache(self):
        """Aísla estado global entre tests: caché de core.dictionary y el
        singleton de backends (core.backends._instances cachea el cliente Groq
        mockeado de un test anterior; sin resetearlo, el segundo test reutiliza
        el mock viejo en vez de tomar el nuevo return_value)."""
        from core import dictionary
        from core.backends import _instances, _instances_lock
        yield
        dictionary._db = None
        dictionary._cache = dictionary._EMPTY_CACHE
        with _instances_lock:
            _instances.clear()

    @patch("core.backends.groq_backend.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_return_raw_with_dictionary_replacement(self, mock_groq_cls, tmp_path, monkeypatch):
        """Con return_raw=True y un reemplazo activo del diccionario, raw != final."""
        from core import dictionary
        from db.database import TranscriptionDB
        from core.transcriber import Transcriber

        db_path = str(tmp_path / "test.db")
        db = TranscriptionDB(db_path=db_path)
        db.add_dictionary_entry(replace_to="Johann", replace_from="Johan")
        monkeypatch.setattr(dictionary, "_db", db)
        dictionary.invalidate()

        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "hola Johan"

        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        text, raw = t.transcribe(buf, return_raw=True)
        assert text == "hola Johann"
        assert raw == "hola Johan"

    @patch("core.backends.groq_backend.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_return_raw_none_without_replacement(self, mock_groq_cls, tmp_path, monkeypatch):
        """Sin reemplazos que apliquen, raw_text es None (ahorro de espacio)."""
        from core import dictionary
        from db.database import TranscriptionDB
        from core.transcriber import Transcriber

        db_path = str(tmp_path / "test.db")
        db = TranscriptionDB(db_path=db_path)
        monkeypatch.setattr(dictionary, "_db", db)
        dictionary.invalidate()

        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "hola mundo"

        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        text, raw = t.transcribe(buf, return_raw=True)
        assert text == "hola mundo"
        assert raw is None

    @patch("core.backends.groq_backend.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_default_signature_unchanged(self, mock_groq_cls):
        """(g) Sin return_raw, transcribe() sigue devolviendo un str (llamadores existentes)."""
        from core.transcriber import Transcriber
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "hola mundo"
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        result = t.transcribe(buf)
        assert isinstance(result, str)
        assert result == "hola mundo"

    def test_empty_buffer_return_raw(self):
        """Alucinación/buffer vacío con return_raw=True: tupla ('', None)."""
        from core.transcriber import Transcriber
        t = Transcriber()
        buf = io.BytesIO(b"")
        result = t.transcribe(buf, return_raw=True)
        assert result == ("", None)


# ---------------------------------------------------------------------------
# (d) Generación de sugerencias desde diff + (e) dedup
# ---------------------------------------------------------------------------
class TestSuggestDictionaryPairs:
    def test_single_word_correction_suggests_pair(self):
        from core.dictionary import suggest_dictionary_pairs
        old = "hola me llamo Johan y trabajo en velos"
        new = "hola me llamo Johann y trabajo en velos"
        pairs = suggest_dictionary_pairs(old, new)
        assert ("Johan", "Johann") in pairs

    def test_full_rewrite_suggests_nothing(self):
        """Reescritura completa del texto no debe generar sugerencias (heurística conservadora)."""
        from core.dictionary import suggest_dictionary_pairs
        old = "el reporte de ventas necesita revision antes del viernes"
        new = "hay que enviar el documento financiero completo mañana temprano sin falta"
        pairs = suggest_dictionary_pairs(old, new)
        assert pairs == []

    def test_short_words_not_suggested(self):
        """Palabras muy cortas (<=2 chars) no generan sugerencia (ruido: artículos, etc.)."""
        from core.dictionary import suggest_dictionary_pairs
        old = "yo voy a ir"
        new = "yo voy a ver"
        pairs = suggest_dictionary_pairs(old, new)
        assert pairs == []

    def test_identical_text_suggests_nothing(self):
        from core.dictionary import suggest_dictionary_pairs
        assert suggest_dictionary_pairs("hola mundo", "hola mundo") == []

    def test_dedup_against_existing_entry(self, tmp_path):
        """add_suggested_entry no inserta si ya existe una entrada con ese replace_from (case-insensitive)."""
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        db.add_dictionary_entry(replace_to="Johann", replace_from="Johan")
        result = db.add_suggested_entry(replace_from="johan", replace_to="Johann")
        assert result is None
        assert len(db.list_suggested_dictionary()) == 0

    def test_suggested_entry_starts_disabled(self, tmp_path):
        """[O4] Las sugerencias nacen SIEMPRE con enabled=0."""
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        eid = db.add_suggested_entry(replace_from="Johan", replace_to="Johann")
        assert eid is not None
        entries = db.list_dictionary()
        entry = next(e for e in entries if e["id"] == eid)
        assert entry["enabled"] == 0
        assert entry["source"] == "suggested"


# ---------------------------------------------------------------------------
# (f) Aceptar / descartar sugerencia
# ---------------------------------------------------------------------------
class TestSuggestedDictionaryReview:
    def test_accept_suggested_enables_and_relabels(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        eid = db.add_suggested_entry(replace_from="Johan", replace_to="Johann")
        updated = db.accept_suggested_entry(eid)
        assert updated == 1
        entries = db.list_dictionary()
        entry = next(e for e in entries if e["id"] == eid)
        assert entry["enabled"] == 1
        assert entry["source"] == "manual"

    def test_discard_suggested_deletes(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        eid = db.add_suggested_entry(replace_from="Johan", replace_to="Johann")
        deleted = db.delete_dictionary_entry(eid)
        assert deleted == 1
        assert db.list_suggested_dictionary() == []

    def test_count_suggested(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        assert db.count_suggested_dictionary() == 0
        db.add_suggested_entry(replace_from="Johan", replace_to="Johann")
        assert db.count_suggested_dictionary() == 1

    def test_accept_nonexistent_returns_zero(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        assert db.accept_suggested_entry(9999) == 0
