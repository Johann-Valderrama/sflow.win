"""Tests de la unidad 1.3 'Validación de rutas configurables en /api/settings'.

Cierra el bug de la auditoría: PENDING_EXPORT_DIR y OPS_BRIEFING_PATH aceptaban
cualquier string arbitrario (vector de persistencia vía la carpeta Startup de
Windows para el primero; lectura de cualquier .md legible para el segundo).

Cubre (sin salir a la red, sin tocar el .env real del usuario más de lo que ya
hacen los tests hermanos de settings):
  (a) ruta bajo C:\\Windows y bajo la carpeta Startup -> rechazada;
  (b) %APPDATA%\\Vflow\\exports -> aceptada;
  (c) ruta UNC \\\\server\\share\\carpeta -> aceptada SIN llamar a isdir (no toca la red);
  (d) ruta local inexistente -> rechazada;
  (e) ruta relativa -> rechazada;
  (f) OPS_BRIEFING_PATH sin .md -> rechazado; con .md -> aceptado; vacío -> aceptado;
  (g) el resto del POST de /api/settings sigue funcionando (shape intacto).
"""

import os

import pytest

from web.server import _validate_briefing_path, _validate_export_dir


# ---------------------------------------------------------------------------
# Funciones puras: _validate_export_dir
# ---------------------------------------------------------------------------

class TestValidateExportDir:
    def test_empty_is_valid(self):
        assert _validate_export_dir("") is None
        assert _validate_export_dir("   ") is None

    def test_relative_path_rejected(self):
        err = _validate_export_dir("carpeta\\relativa")
        assert err is not None

    def test_windows_root_rejected(self, monkeypatch):
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        err = _validate_export_dir(r"C:\Windows\System32\algo")
        assert err is not None

    def test_startup_folder_rejected(self, monkeypatch, tmp_path):
        appdata = tmp_path / "AppData" / "Roaming"
        monkeypatch.setenv("APPDATA", str(appdata))
        startup = str(appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
        err = _validate_export_dir(startup)
        assert err is not None

    def test_appdata_vflow_subfolder_accepted(self, monkeypatch, tmp_path):
        """%APPDATA%\\Vflow\\exports debe seguir pasando (no blacklistear APPDATA entero)."""
        appdata = tmp_path / "AppData" / "Roaming"
        vflow_exports = appdata / "Vflow" / "exports"
        vflow_exports.mkdir(parents=True)
        monkeypatch.setenv("APPDATA", str(appdata))
        err = _validate_export_dir(r"%APPDATA%\Vflow\exports")
        assert err is None

    def test_unc_path_accepted_without_touching_network(self, monkeypatch):
        """Una ruta UNC no debe disparar isdir() (shares pueden estar offline)."""
        calls = []
        monkeypatch.setattr(os.path, "isdir", lambda p: calls.append(p) or False)
        err = _validate_export_dir(r"\\server\share\carpeta")
        assert err is None
        assert calls == []  # isdir() nunca se llamó para la ruta UNC

    def test_local_nonexistent_rejected(self, tmp_path):
        missing = str(tmp_path / "no_existe_esta_carpeta")
        err = _validate_export_dir(missing)
        assert err is not None

    def test_local_existing_accepted(self, tmp_path):
        existing = tmp_path / "exports"
        existing.mkdir()
        err = _validate_export_dir(str(existing))
        assert err is None


# ---------------------------------------------------------------------------
# Funciones puras: _validate_briefing_path
# ---------------------------------------------------------------------------

class TestValidateBriefingPath:
    def test_empty_is_valid(self):
        assert _validate_briefing_path("") is None

    def test_non_md_rejected(self):
        err = _validate_briefing_path(r"C:\notas\briefing.txt")
        assert err is not None

    def test_md_accepted_case_insensitive(self):
        assert _validate_briefing_path(r"C:\notas\briefing.md") is None
        assert _validate_briefing_path(r"C:\notas\briefing.MD") is None

    def test_no_existence_check(self, tmp_path):
        """El módulo ops_briefing ya es fail-open; no debe exigirse que el archivo exista."""
        never_created = tmp_path / "no_existe.md"
        assert not never_created.exists()
        err = _validate_briefing_path(str(never_created))
        assert err is None


# ---------------------------------------------------------------------------
# Endpoint /api/settings — comportamiento end-to-end
# ---------------------------------------------------------------------------
try:
    import flask as _flask  # noqa: F401
    _has_flask = True
except ImportError:
    _has_flask = False


@pytest.mark.skipif(not _has_flask, reason="flask not installed")
class TestSettingsEndpointValidation:
    @pytest.fixture(autouse=True)
    def client(self, tmp_path):
        from web.server import app, _db
        _db.db_path = str(tmp_path / "test.db")
        _db._init_db()
        self.app = app
        self.client = app.test_client()

    def test_bad_export_dir_rejected_with_field_error(self, monkeypatch):
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        resp = self.client.post(
            "/api/settings",
            json={"pending_export_dir": r"C:\Windows\System32"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "pending_export_dir" in body["error"]

    def test_bad_briefing_path_rejected_with_field_error(self):
        resp = self.client.post(
            "/api/settings",
            json={"ops_briefing_path": r"C:\notas\briefing.txt"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "ops_briefing_path" in body["error"]

    def test_rejected_field_not_persisted_others_still_saved(self, monkeypatch):
        """El campo inválido no se persiste; los demás campos del mismo POST sí."""
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        # Estado previo conocido
        self.client.post("/api/settings", json={"dictation_modes_enabled": "false"})

        resp = self.client.post(
            "/api/settings",
            json={
                "pending_export_dir": r"C:\Windows\System32",
                "dictation_modes_enabled": "true",
            },
        )
        assert resp.status_code == 400

        data = self.client.get("/api/settings").get_json()
        # El campo rechazado no debió cambiar a la ruta prohibida.
        assert data["pending_export_dir"] != r"C:\Windows\System32"
        # El campo válido del mismo POST sí se aplicó (comportamiento campo-a-campo).
        assert data["dictation_modes_enabled"] is True

    def test_valid_export_dir_and_briefing_roundtrip(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        briefing = tmp_path / "briefing.md"

        resp = self.client.post(
            "/api/settings",
            json={
                "pending_export_dir": str(export_dir),
                "ops_briefing_path": str(briefing),
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

        data = self.client.get("/api/settings").get_json()
        assert data["pending_export_dir"] == str(export_dir)
        assert data["ops_briefing_path"] == str(briefing)

    def test_rest_of_settings_post_shape_unaffected(self):
        """El resto del POST de settings (sin rutas) sigue funcionando igual (shape intacto)."""
        resp = self.client.post(
            "/api/settings",
            json={
                "dictation_modes_enabled": "true",
                "dictation_mode_map": "notepad.exe:chat",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

        data = self.client.get("/api/settings").get_json()
        assert data["dictation_modes_enabled"] is True
        assert data["dictation_mode_map"] == "notepad.exe:chat"
