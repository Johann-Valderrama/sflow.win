"""Tests de la unidad 3.2 "Retención opcional de reuniones" del PLAN-MEJORAS.

MEETING_RETENTION_DAYS (default 0 = conservar SIEMPRE) es la ÚNICA operación
destructiva de este plan: si > 0, al arrancar la app se borran las reuniones
más viejas que N días. Objeción BLOCKER O2 ya resuelta en el diseño:
``meetings_fts`` se mantiene A MANO (sin triggers), así que borrar filas de
``meetings`` sin limpiar primero su entrada FTS dejaría huérfanos fantasma
(``meetings_search`` seguiría devolviendo hits de reuniones ya borradas). Por
eso ``meetings_prune_older_than`` sigue el mismo patrón que
``meeting_delete``/``meetings_delete_all``: FTS-antes-que-filas.

Cobertura (hermética, DB en tmp_path, nunca toca transcriptions.db real):
  1. days=0 -> NO borra nada (fila ni FTS), aunque la reunión sea de hace años.
     Es el test CRÍTICO (default apagado, prueba dura de "0 no borra nada").
  2. days negativo / no numérico -> no-op sin excepción.
  3. days=N -> borra fila Y su entrada FTS (meetings_search deja de verla),
     conserva las reuniones más recientes que N.
  4. FTS deshabilitado (_fts_enabled=False) -> el prune no revienta.
  5. El setting en /api/settings: acepta 0/N, rechaza no-numérico con 400
     (nunca deja el GET subsiguiente en 500).
"""
import os
import sqlite3

import pytest

from db.database import TranscriptionDB


def _set_created_at(db_path: str, meeting_id: int, when: str) -> None:
    """Fuerza created_at de una reunión ya insertada (meeting_insert no acepta
    ese parámetro; el default de la columna es datetime('now'))."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE meetings SET created_at=? WHERE id=?", (when, meeting_id))
        conn.commit()


@pytest.fixture()
def db(tmp_path):
    return TranscriptionDB(db_path=str(tmp_path / "test_meetings_retention.db"))


# ---------------------------------------------------------------------------
# 1. days=0 -> no-op duro (CRÍTICO)
# ---------------------------------------------------------------------------

class TestDaysZeroNeverDeletes:
    def test_ancient_meeting_survives_days_zero(self, db):
        mid = db.meeting_insert(
            "Reunión de hace años", "Contenido único zzqvantico para buscar.", "[]", 60.0,
        )
        _set_created_at(db.db_path, mid, "2000-01-01 00:00:00")

        deleted = db.meetings_prune_older_than(0)

        assert deleted == 0
        assert db.meeting_get(mid) is not None
        results = db.meetings_search("zzqvantico")
        ids_found = [r["id"] for r in results]
        assert mid in ids_found

    def test_multiple_ancient_meetings_all_survive_days_zero(self, db):
        ids = []
        for i in range(3):
            mid = db.meeting_insert(f"Reunión vieja {i}", f"Texto plano numero {i}.", "[]", 30.0)
            _set_created_at(db.db_path, mid, "1999-06-15 08:00:00")
            ids.append(mid)

        deleted = db.meetings_prune_older_than(0)

        assert deleted == 0
        for mid in ids:
            assert db.meeting_get(mid) is not None


# ---------------------------------------------------------------------------
# 2. days negativo / no numérico -> no-op sin excepción
# ---------------------------------------------------------------------------

class TestInvalidDaysIsNoop:
    def test_negative_days_is_noop(self, db):
        mid = db.meeting_insert("Reunión vieja", "Texto de prueba.", "[]", 45.0)
        _set_created_at(db.db_path, mid, "2010-01-01 00:00:00")

        deleted = db.meetings_prune_older_than(-30)

        assert deleted == 0
        assert db.meeting_get(mid) is not None

    def test_non_numeric_env_value_is_noop_without_exception(self, db, monkeypatch):
        """Replica EXACTAMENTE el idiom de arranque de main.py (try/except int()
        alrededor de MEETING_RETENTION_DAYS): un valor no numérico en el .env no
        debe propagar excepción ni borrar nada, igual que el patrón ya probado
        de HISTORY_RETENTION_DAYS."""
        mid = db.meeting_insert("Reunión vieja", "Texto de prueba.", "[]", 45.0)
        _set_created_at(db.db_path, mid, "2010-01-01 00:00:00")

        monkeypatch.setenv("MEETING_RETENTION_DAYS", "no-es-un-numero")

        pruned = None
        try:
            meeting_retention_days = int(os.getenv("MEETING_RETENTION_DAYS", "0") or 0)
            if meeting_retention_days > 0:
                pruned = db.meetings_prune_older_than(meeting_retention_days)
        except Exception:  # noqa: BLE001 — mismo catch-all amplio que main.py
            pruned = None

        assert pruned is None  # nunca se llegó a llamar meetings_prune_older_than
        assert db.meeting_get(mid) is not None


# ---------------------------------------------------------------------------
# 3. days=N -> borra fila Y entrada FTS; conserva lo reciente
# ---------------------------------------------------------------------------

class TestPruneDeletesRowAndFts:
    def test_old_meeting_pruned_recent_kept(self, db):
        old_id = db.meeting_insert(
            "Reunión antigua", "Hablamos del termino unico xyzterm en esta reunion.", "[]", 60.0,
        )
        _set_created_at(db.db_path, old_id, "2000-03-01 00:00:00")

        recent_id = db.meeting_insert(
            "Reunión reciente", "Discutimos otro termino unico wvbterm en esta reunion.", "[]", 60.0,
        )
        # created_at por defecto = datetime('now'), no hace falta tocarlo.

        # Antes de podar: ambas reuniones son visibles.
        assert db.meeting_get(old_id) is not None
        assert db.meeting_get(recent_id) is not None

        deleted = db.meetings_prune_older_than(30)

        assert deleted == 1
        assert db.meeting_get(old_id) is None
        assert db.meeting_get(recent_id) is not None

        # La búsqueda FTS ya no debe devolver hits de la reunión borrada
        # (objeción O2: sin este orden, quedaría un huérfano fantasma en el índice).
        results_old = db.meetings_search("xyzterm")
        assert [r["id"] for r in results_old] == []

        results_recent = db.meetings_search("wvbterm")
        assert recent_id in [r["id"] for r in results_recent]

    def test_returns_zero_when_nothing_to_prune(self, db):
        mid = db.meeting_insert("Reunión reciente", "Texto actual.", "[]", 10.0)
        deleted = db.meetings_prune_older_than(30)
        assert deleted == 0
        assert db.meeting_get(mid) is not None


# ---------------------------------------------------------------------------
# 4. FTS deshabilitado -> el prune no revienta
# ---------------------------------------------------------------------------

class TestPruneWithFtsDisabled:
    def test_prune_survives_fts_disabled(self, db):
        db._fts_enabled = False  # simula un entorno sin extensión FTS5 disponible

        old_id = db.meeting_insert("Reunión vieja sin FTS", "Texto cualquiera.", "[]", 20.0)
        _set_created_at(db.db_path, old_id, "2005-01-01 00:00:00")
        recent_id = db.meeting_insert("Reunión reciente sin FTS", "Texto cualquiera reciente.", "[]", 20.0)

        deleted = db.meetings_prune_older_than(30)

        assert deleted == 1
        assert db.meeting_get(old_id) is None
        assert db.meeting_get(recent_id) is not None


# ---------------------------------------------------------------------------
# 5. /api/settings — acepta 0/N, rechaza no-numérico con 400 (nunca 500)
# ---------------------------------------------------------------------------

try:
    import flask as _flask  # noqa: F401
    _has_flask = True
except ImportError:
    _has_flask = False


@pytest.mark.skipif(not _has_flask, reason="flask not installed")
class TestSettingsEndpoint:
    @pytest.fixture(autouse=True)
    def client(self, tmp_path):
        from web.server import app, _db
        _db.db_path = str(tmp_path / "test_settings.db")
        _db._init_db()
        self.app = app
        self.client = app.test_client()

    def test_get_defaults_to_zero(self, monkeypatch):
        # MEETING_RETENTION_DAYS puede haber quedado seteada por una sesión anterior
        # en %APPDATA%\Vflow\.env (persistente entre corridas, igual que el resto de
        # settings de este archivo); aislamos explícitamente el valor por default.
        monkeypatch.delenv("MEETING_RETENTION_DAYS", raising=False)
        resp = self.client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.get_json()["meeting_retention_days"] == 0

    def test_accepts_zero(self):
        resp = self.client.post("/api/settings", json={"meeting_retention_days": "0"})
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert self.client.get("/api/settings").get_json()["meeting_retention_days"] == 0

    def test_accepts_positive_integer(self):
        resp = self.client.post("/api/settings", json={"meeting_retention_days": "90"})
        assert resp.status_code == 200
        assert self.client.get("/api/settings").get_json()["meeting_retention_days"] == 90

    def test_rejects_non_numeric_with_400_not_500(self):
        resp = self.client.post("/api/settings", json={"meeting_retention_days": "abc"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert "meeting_retention_days" in body["error"]

        # Y sobre todo: el GET subsiguiente sigue vivo, nunca un 500.
        get_resp = self.client.get("/api/settings")
        assert get_resp.status_code == 200
        assert isinstance(get_resp.get_json()["meeting_retention_days"], int)

    def test_rejected_field_not_persisted_others_still_saved(self):
        # Estado previo conocido (mismo idiom que test_settings_validation.py).
        self.client.post("/api/settings", json={"meeting_retention_days": "30"})

        resp = self.client.post(
            "/api/settings",
            json={
                "meeting_retention_days": "not-a-number",
                "dictation_modes_enabled": "true",
            },
        )
        assert resp.status_code == 400

        data = self.client.get("/api/settings").get_json()
        # El campo rechazado no debió cambiar (sigue en el valor previo, no en la basura).
        assert data["meeting_retention_days"] == 30
        assert data["dictation_modes_enabled"] is True  # el otro campo del mismo POST sí se aplicó

    def test_garbage_written_directly_to_env_does_not_500_get(self, monkeypatch):
        """Defensa de segunda línea: si el .env quedó con basura (editado a mano,
        migración vieja), el GET no debe tronar con un ValueError."""
        monkeypatch.setenv("MEETING_RETENTION_DAYS", "garbage")
        resp = self.client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.get_json()["meeting_retention_days"] == 0
