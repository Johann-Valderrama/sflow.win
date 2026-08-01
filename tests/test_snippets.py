"""Tests para la unidad 4a de PLAN-DICTADO-2026-07-31: tabla `snippets` +
migración idempotente + CRUD (db/database.py).

Cubre:
(a) CRUD básico: alta/baja/edición/listado
(b) migración idempotente: correr _init_db dos veces, Y sobre una base creada
    con el esquema VIEJO (sin la tabla snippets) — el caso real de un usuario
    que ya tiene datos, no solo una base virgen
(c) unicidad del disparador (trigger_key), incluidas variantes de mayúsculas
    y acentos
(d) rechazo del disparador vacío/solo-espacios
(e) tope de tamaño del body, y que el body NO se sanea de otras formas
    (saltos de línea, comillas, acentos sobreviven intactos)
(f) enabled filtrando el listado
(g) hit_count incrementando

Aislamiento: cada test usa una DB SQLite bajo tmp_path (pytest), nunca la
real de dev. El fixture `db` comprueba explícitamente que la ruta temporal
es distinta de config.DB_PATH y que el archivo se creó donde se esperaba,
antes de confiar en cualquier resultado (un aislamiento que falla en
silencio deja la prueba escribiendo en la base de producción).
"""

import os
import sqlite3

import pytest


# ---------------------------------------------------------------------------
# normalize_trigger — función pura, sin DB
# ---------------------------------------------------------------------------
class TestNormalizeTrigger:
    def test_lowercases(self):
        from db.database import normalize_trigger
        assert normalize_trigger("FIRMA CORREO") == "firma correo"

    def test_strips_accents(self):
        from db.database import normalize_trigger
        assert normalize_trigger("petición") == normalize_trigger("peticion")
        assert normalize_trigger("petición") == "peticion"

    def test_collapses_internal_whitespace_and_trims(self):
        from db.database import normalize_trigger
        assert normalize_trigger("  firma   correo  ") == "firma correo"

    def test_case_and_accents_together(self):
        from db.database import normalize_trigger
        assert normalize_trigger("Firma Correo") == normalize_trigger("firma correo")
        assert normalize_trigger("Petición") == normalize_trigger("peticion")

    def test_empty_and_none(self):
        from db.database import normalize_trigger
        assert normalize_trigger("") == ""
        assert normalize_trigger(None) == ""
        assert normalize_trigger("   ") == ""


# ---------------------------------------------------------------------------
# CRUD básico
# ---------------------------------------------------------------------------
class TestSnippetsCRUD:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        import config

        self.db_path = str(tmp_path / "test_snippets.db")
        # Verificación de aislamiento: la ruta temporal no puede coincidir
        # con la base real de producción, o esta prueba (y todas las que
        # heredan este fixture) estarían escribiendo ahí sin que nadie lo note.
        assert self.db_path != config.DB_PATH
        self.db = TranscriptionDB(db_path=self.db_path)
        assert os.path.exists(self.db_path), "la DB temporal no se creó donde se esperaba"
        yield

    def test_add_and_get_snippet(self):
        sid = self.db.add_snippet("firma correo", "Saludos,\nJohann")
        assert sid >= 1
        row = self.db.get_snippet(sid)
        assert row["trigger"] == "firma correo"
        assert row["trigger_key"] == "firma correo"
        assert row["body"] == "Saludos,\nJohann"
        assert row["enabled"] == 1
        assert row["hit_count"] == 0
        assert row["created_at"] is not None

    def test_get_snippet_missing_returns_none(self):
        assert self.db.get_snippet(99999) is None

    def test_list_snippets_default_includes_disabled(self):
        sid1 = self.db.add_snippet("uno", "cuerpo uno")
        sid2 = self.db.add_snippet("dos", "cuerpo dos")
        self.db.set_snippet_enabled(sid2, False)
        rows = self.db.list_snippets()
        ids = {r["id"] for r in rows}
        assert ids == {sid1, sid2}

    def test_list_snippets_enabled_only_filters(self):
        sid1 = self.db.add_snippet("uno", "cuerpo uno")
        sid2 = self.db.add_snippet("dos", "cuerpo dos")
        self.db.set_snippet_enabled(sid2, False)
        rows = self.db.list_snippets(enabled_only=True)
        ids = {r["id"] for r in rows}
        assert ids == {sid1}

    def test_update_snippet_trigger_and_body(self):
        sid = self.db.add_snippet("vieja frase", "cuerpo viejo")
        updated = self.db.update_snippet(sid, trigger="nueva frase", body="cuerpo nuevo")
        assert updated == 1
        row = self.db.get_snippet(sid)
        assert row["trigger"] == "nueva frase"
        assert row["trigger_key"] == "nueva frase"
        assert row["body"] == "cuerpo nuevo"

    def test_update_snippet_partial_body_only(self):
        sid = self.db.add_snippet("firma", "cuerpo viejo")
        self.db.update_snippet(sid, body="cuerpo nuevo")
        row = self.db.get_snippet(sid)
        assert row["trigger"] == "firma"
        assert row["body"] == "cuerpo nuevo"

    def test_update_snippet_partial_trigger_only(self):
        sid = self.db.add_snippet("firma", "cuerpo")
        self.db.update_snippet(sid, trigger="firma nueva")
        row = self.db.get_snippet(sid)
        assert row["trigger"] == "firma nueva"
        assert row["body"] == "cuerpo"

    def test_update_snippet_no_fields_is_noop(self):
        sid = self.db.add_snippet("firma", "cuerpo")
        assert self.db.update_snippet(sid) == 0

    def test_update_nonexistent_returns_zero(self):
        assert self.db.update_snippet(99999, trigger="x") == 0

    def test_delete_snippet(self):
        sid = self.db.add_snippet("borrar", "cuerpo")
        assert self.db.delete_snippet(sid) == 1
        assert self.db.get_snippet(sid) is None

    def test_delete_nonexistent_returns_zero(self):
        assert self.db.delete_snippet(99999) == 0

    def test_set_snippet_enabled_toggle(self):
        sid = self.db.add_snippet("firma", "cuerpo")
        assert self.db.set_snippet_enabled(sid, False) == 1
        assert self.db.get_snippet(sid)["enabled"] == 0
        assert self.db.set_snippet_enabled(sid, True) == 1
        assert self.db.get_snippet(sid)["enabled"] == 1

    def test_increment_snippet_hits(self):
        sid = self.db.add_snippet("firma", "cuerpo")
        assert self.db.increment_snippet_hits([sid]) == 1
        assert self.db.get_snippet(sid)["hit_count"] == 1
        # Un segundo llamado suma 1 más. Nota: al igual que
        # increment_dictionary_hits, es un solo UPDATE ... WHERE id IN (...);
        # pasar el mismo id repetido en la LISTA no multiplica el incremento
        # (SQL solo toca la fila una vez), el llamador (el matcher, 4b) es
        # quien debe invocarlo una vez POR OCURRENCIA si quiere contar cada
        # expansión, igual que hace core/dictionary.py::apply_replacements.
        self.db.increment_snippet_hits([sid, sid])
        assert self.db.get_snippet(sid)["hit_count"] == 2

    def test_increment_snippet_hits_empty_list(self):
        assert self.db.increment_snippet_hits([]) == 0


# ---------------------------------------------------------------------------
# Unicidad del disparador
# ---------------------------------------------------------------------------
class TestSnippetsUniqueness:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test_snippets_unique.db")
        self.db = TranscriptionDB(db_path=self.db_path)
        yield

    def test_duplicate_trigger_exact_rejected(self):
        from db.database import DuplicateTriggerError
        self.db.add_snippet("firma correo", "cuerpo 1")
        with pytest.raises(DuplicateTriggerError):
            self.db.add_snippet("firma correo", "cuerpo 2")

    def test_duplicate_trigger_case_insensitive_rejected(self):
        from db.database import DuplicateTriggerError
        self.db.add_snippet("Firma Correo", "cuerpo 1")
        with pytest.raises(DuplicateTriggerError):
            self.db.add_snippet("firma correo", "cuerpo 2")

    def test_duplicate_trigger_accent_insensitive_rejected(self):
        from db.database import DuplicateTriggerError
        self.db.add_snippet("petición", "cuerpo 1")
        with pytest.raises(DuplicateTriggerError):
            self.db.add_snippet("peticion", "cuerpo 2")

    def test_duplicate_trigger_whitespace_variant_rejected(self):
        from db.database import DuplicateTriggerError
        self.db.add_snippet("firma correo", "cuerpo 1")
        with pytest.raises(DuplicateTriggerError):
            self.db.add_snippet("  firma   correo  ", "cuerpo 2")

    def test_first_insert_survives_after_rejected_duplicate(self):
        """El rechazo del segundo INSERT no debe dejar la tabla a medias."""
        from db.database import DuplicateTriggerError
        sid = self.db.add_snippet("firma correo", "cuerpo 1")
        with pytest.raises(DuplicateTriggerError):
            self.db.add_snippet("Firma Correo", "cuerpo 2")
        assert self.db.get_snippet(sid)["body"] == "cuerpo 1"
        assert len(self.db.list_snippets()) == 1

    def test_update_snippet_trigger_colliding_with_another_raises(self):
        from db.database import DuplicateTriggerError
        self.db.add_snippet("uno", "cuerpo uno")
        sid2 = self.db.add_snippet("dos", "cuerpo dos")
        with pytest.raises(DuplicateTriggerError):
            self.db.update_snippet(sid2, trigger="UNO")

    def test_update_snippet_keeping_own_trigger_does_not_raise(self):
        """Re-guardar un snippet con su MISMO disparador (solo cambia el body)
        no debe chocar consigo mismo."""
        sid = self.db.add_snippet("firma", "cuerpo viejo")
        updated = self.db.update_snippet(sid, trigger="firma", body="cuerpo nuevo")
        assert updated == 1
        assert self.db.get_snippet(sid)["body"] == "cuerpo nuevo"

    def test_snippet_trigger_exists_true_and_false(self):
        self.db.add_snippet("firma correo", "cuerpo")
        assert self.db.snippet_trigger_exists("Firma Correo") is True
        assert self.db.snippet_trigger_exists("otra frase") is False

    def test_snippet_trigger_exists_excludes_self_on_edit(self):
        sid = self.db.add_snippet("firma correo", "cuerpo")
        # Sin exclude_id, el propio snippet cuenta como "ya existe".
        assert self.db.snippet_trigger_exists("firma correo") is True
        # Con exclude_id=sid, no debe chocar consigo mismo.
        assert self.db.snippet_trigger_exists("firma correo", exclude_id=sid) is False


# ---------------------------------------------------------------------------
# Validación: disparador vacío, body vacío/tope de tamaño, no-saneado
# ---------------------------------------------------------------------------
class TestSnippetsValidation:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test_snippets_validation.db")
        self.db = TranscriptionDB(db_path=self.db_path)
        yield

    def test_empty_trigger_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet("", "cuerpo")

    def test_whitespace_only_trigger_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet("   ", "cuerpo")

    def test_none_trigger_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet(None, "cuerpo")

    def test_empty_body_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet("firma", "")

    def test_whitespace_only_body_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet("firma", "   \n  ")

    def test_none_body_rejected(self):
        with pytest.raises(ValueError):
            self.db.add_snippet("firma", None)

    def test_body_over_max_chars_rejected(self):
        from db.database import _BODY_MAX_CHARS
        too_long = "x" * (_BODY_MAX_CHARS + 1)
        with pytest.raises(ValueError) as exc_info:
            self.db.add_snippet("firma", too_long)
        # El error debe traer el número, no un truncado silencioso.
        assert str(_BODY_MAX_CHARS) in str(exc_info.value)

    def test_body_at_max_chars_accepted(self):
        from db.database import _BODY_MAX_CHARS
        exactly_max = "x" * _BODY_MAX_CHARS
        sid = self.db.add_snippet("firma", exactly_max)
        assert len(self.db.get_snippet(sid)["body"]) == _BODY_MAX_CHARS

    def test_update_body_over_max_chars_rejected(self):
        from db.database import _BODY_MAX_CHARS
        sid = self.db.add_snippet("firma", "cuerpo corto")
        with pytest.raises(ValueError):
            self.db.update_snippet(sid, body="x" * (_BODY_MAX_CHARS + 1))
        # El body original no debe haberse tocado.
        assert self.db.get_snippet(sid)["body"] == "cuerpo corto"

    def test_body_preserves_newlines_quotes_and_accents(self):
        """El body es texto libre que termina pegado en otra app: saltos de
        línea, comillas y acentos deben sobrevivir LITERALES, sin sanear."""
        body = 'Estimado/a:\n\n"Gracias por su atención", quedamos a su disposición.\n\nSaludos,\nJohann Valderrama Niño'
        sid = self.db.add_snippet("firma formal", body)
        assert self.db.get_snippet(sid)["body"] == body

    def test_trigger_is_stored_literal_not_normalized(self):
        """La columna `trigger` guarda lo que el usuario escribió tal cual;
        la forma normalizada vive aparte, en trigger_key."""
        sid = self.db.add_snippet("Firma Correo", "cuerpo")
        row = self.db.get_snippet(sid)
        assert row["trigger"] == "Firma Correo"
        assert row["trigger_key"] == "firma correo"


# ---------------------------------------------------------------------------
# Migración idempotente
# ---------------------------------------------------------------------------
class TestSnippetsMigration:
    def test_migration_idempotent_running_twice(self, tmp_path):
        """Correr _init_db dos veces sobre la MISMA base ya actualizada no
        debe fallar ni duplicar la tabla/índice."""
        from db.database import TranscriptionDB
        db_path = str(tmp_path / "twice.db")
        db1 = TranscriptionDB(db_path=db_path)
        sid = db1.add_snippet("firma", "cuerpo")
        db2 = TranscriptionDB(db_path=db_path)  # segunda inicialización, misma DB
        # No duplicó la fila ni rompió el índice: el dato sigue ahí, una sola vez.
        assert db2.get_snippet(sid)["body"] == "cuerpo"
        assert len(db2.list_snippets()) == 1
        with sqlite3.connect(db_path) as conn:
            idx_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_snippets_trigger_key'"
            ).fetchone()[0]
        assert idx_count == 1

    def test_migration_applies_to_preexisting_database_without_snippets_table(
        self, tmp_path, monkeypatch
    ):
        """El caso real: un usuario que YA tiene la app instalada (su base no
        tiene la tabla `snippets`, porque la creó una versión anterior).

        Se simula el esquema VIEJO deshabilitando _SNIPPETS_DDL con
        monkeypatch (en vez de una base virgen), se comprueba que de verdad
        falta la tabla, y luego se reabre con el código real para confirmar
        que la migración la agrega sin romper los datos preexistentes.
        """
        from db.database import TranscriptionDB

        db_path = str(tmp_path / "legacy.db")

        # 1. Crea la base como la dejaría una versión SIN snippets, con datos
        #    reales preexistentes (una transcripción y una entrada de
        #    diccionario, para comprobar que la migración no las toca).
        monkeypatch.setattr(TranscriptionDB, "_SNIPPETS_DDL", [])
        legacy_db = TranscriptionDB(db_path=db_path)
        old_tid = legacy_db.insert(text="transcripción anterior a snippets")
        old_dict_id = legacy_db.add_dictionary_entry(replace_to="Vflow", replace_from="v flow")

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        # Confirma que el aislamiento del esquema viejo funcionó de verdad
        # (si esto no se cumple, el resto del test no prueba nada real).
        assert "snippets" not in tables

        # 2. Quita el parche: vuelve la DDL real de snippets. Reabre la MISMA
        #    base de datos, como haría el usuario al actualizar la app.
        monkeypatch.undo()
        upgraded_db = TranscriptionDB(db_path=db_path)

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "snippets" in tables

        # Los datos preexistentes sobreviven intactos.
        assert upgraded_db.get_by_id(old_tid)["text"] == "transcripción anterior a snippets"
        assert upgraded_db.dictionary_from_exists("v flow")
        assert old_dict_id is not None

        # La tabla nueva funciona de inmediato, sobre la base recién migrada.
        sid = upgraded_db.add_snippet("firma correo", "Saludos,\nJohann")
        assert upgraded_db.get_snippet(sid)["body"] == "Saludos,\nJohann"

        # 3. Un tercer arranque (el usuario reinicia la app otra vez) tampoco
        #    debe fallar ni duplicar nada.
        again_db = TranscriptionDB(db_path=db_path)
        assert again_db.get_snippet(sid)["body"] == "Saludos,\nJohann"
        assert len(again_db.list_snippets()) == 1
