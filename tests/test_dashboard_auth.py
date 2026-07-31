"""Tests del guard de auth local del dashboard (core/localauth.py + web/state.py::_auth_check).

CONTEXTO DE AISLAMIENTO: la fixture de sesión ``_aislar_entorno_real``
(tests/conftest.py) apaga el guard para TODA la suite, porque ~29 archivos de
test usan el ``test_client`` de Flask sin token. Este archivo es el único que lo
ENCIENDE, y lo hace explícitamente con monkeypatch function-scoped.

El token también se inyecta por monkeypatch sobre la caché de módulo
(``localauth._token``): así ningún test toca el disco ni crea
``dashboard_token.txt`` en el repo.
"""
import pytest

from core import localauth
from web.server import app

_TOKEN = "a" * 64
_HEADER = "X-Vflow-Token"
_COOKIE = "vflow_token"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def guard_on(monkeypatch):
    """Enciende el guard y fija un token conocido sin tocar el disco."""
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setattr(localauth, "_token", _TOKEN)
    return _TOKEN


@pytest.fixture
def guard_off(monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "false")
    monkeypatch.setattr(localauth, "_token", _TOKEN)


# ---------------------------------------------------------------------------
# API: sin token -> 401; con cabecera válida -> pasa; con cabecera inválida -> 401
# ---------------------------------------------------------------------------

class TestApiGuard:
    def test_sin_token_devuelve_401(self, client, guard_on):
        """El hueco original: GET /api/transcriptions con un curl pelado."""
        resp = client.get("/api/transcriptions")
        assert resp.status_code == 401
        assert "error" in resp.get_json()

    def test_cabecera_valida_pasa(self, client, guard_on):
        resp = client.get("/api/transcriptions", headers={_HEADER: _TOKEN})
        assert resp.status_code != 401

    def test_cabecera_invalida_devuelve_401(self, client, guard_on):
        resp = client.get("/api/transcriptions", headers={_HEADER: "b" * 64})
        assert resp.status_code == 401

    def test_cookie_valida_pasa(self, client, guard_on):
        client.set_cookie(_COOKIE, _TOKEN, domain="localhost")
        resp = client.get("/api/transcriptions")
        assert resp.status_code != 401

    def test_query_string_no_sirve_en_la_api(self, client, guard_on):
        """`?t=` es exclusivo de las páginas HTML (canje por cookie); la API
        exige cookie o cabecera, para que un token no viaje en URLs de datos."""
        resp = client.get(f"/api/transcriptions?t={_TOKEN}")
        assert resp.status_code == 401

    def test_otros_endpoints_sensibles_tambien_estan_cubiertos(self, client, guard_on):
        for path in ("/api/meetings", "/api/meetings/1", "/api/stats"):
            assert client.get(path).status_code == 401, path


# ---------------------------------------------------------------------------
# Páginas HTML: ?t= válido -> redirect + cookie; sin nada -> 401
# ---------------------------------------------------------------------------

class TestPageGuard:
    @pytest.mark.parametrize("path", ["/", "/reunion"])
    def test_token_en_query_redirige_y_setea_cookie(self, client, guard_on, path):
        resp = client.get(f"{path}?t={_TOKEN}")
        assert resp.status_code in (301, 302, 303, 307, 308)
        # Redirige a la MISMA ruta sin query string (el token no queda en la
        # barra de direcciones ni en el historial).
        assert resp.headers["Location"].endswith(path)
        assert "?" not in resp.headers["Location"]
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert f"{_COOKIE}={_TOKEN}" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie

    @pytest.mark.parametrize("path", ["/", "/reunion"])
    def test_sin_token_devuelve_401_con_html(self, client, guard_on, path):
        resp = client.get(path)
        assert resp.status_code == 401
        assert "bandeja" in resp.get_data(as_text=True).lower()

    def test_token_invalido_en_query_devuelve_401(self, client, guard_on):
        resp = client.get(f"/?t={'b' * 64}")
        assert resp.status_code == 401

    def test_cookie_valida_sirve_la_pagina(self, client, guard_on):
        client.set_cookie(_COOKIE, _TOKEN, domain="localhost")
        resp = client.get("/")
        assert resp.status_code == 200

    def test_static_esta_exento(self, client, guard_on):
        """Assets sin datos del usuario: un archivo inexistente debe dar 404
        (llegó al router), no 401 (lo paró el guard)."""
        assert client.get("/static/no-existe-jamas.js").status_code == 404


# ---------------------------------------------------------------------------
# Guard apagado: el modo en que corre el resto de la suite
# ---------------------------------------------------------------------------

class TestGuardApagado:
    def test_todo_pasa_sin_token(self, client, guard_off):
        assert client.get("/api/transcriptions").status_code != 401
        assert client.get("/").status_code == 200
        assert client.get("/reunion").status_code == 200


# ---------------------------------------------------------------------------
# core/localauth.py: unidades puras
# ---------------------------------------------------------------------------

class TestLocalauthUnit:
    def test_verify_rechaza_vacio_y_none(self, guard_on):
        assert localauth.verify(None) is False
        assert localauth.verify("") is False

    def test_verify_compara_el_token_completo(self, guard_on):
        assert localauth.verify(_TOKEN) is True
        assert localauth.verify(_TOKEN[:-1]) is False
        assert localauth.verify(_TOKEN + "x") is False

    def test_is_enabled_default_es_true(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_AUTH_ENABLED", raising=False)
        assert localauth.is_enabled() is True

    @pytest.mark.parametrize("valor", ["false", "FALSE", " False "])
    def test_is_enabled_solo_lo_apaga_false(self, monkeypatch, valor):
        monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", valor)
        assert localauth.is_enabled() is False

    @pytest.mark.parametrize("valor", ["", "0", "no", "basura"])
    def test_valor_raro_deja_la_proteccion_encendida(self, monkeypatch, valor):
        """Fail-closed: un .env editado a mano no debe abrir el dashboard."""
        monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", valor)
        assert localauth.is_enabled() is True

    def test_verify_denega_si_el_token_no_se_puede_resolver(self, monkeypatch):
        """Fail-CLOSED (asimetría deliberada con core/ops_briefing.py, que es
        fail-open): un error de I/O al leer/crear el token DENIEGA."""
        monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
        monkeypatch.setattr(localauth, "_token", "")

        def _boom():
            raise PermissionError("disco/permisos")

        monkeypatch.setattr(localauth, "_read_or_create_token", _boom)
        assert localauth.verify(_TOKEN) is False

    def test_api_devuelve_401_si_el_token_no_se_puede_resolver(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
        monkeypatch.setattr(localauth, "_token", "")
        monkeypatch.setattr(
            localauth, "_read_or_create_token",
            lambda: (_ for _ in ()).throw(OSError("disco lleno")),
        )
        resp = client.get("/api/transcriptions", headers={_HEADER: _TOKEN})
        assert resp.status_code == 401

    def test_get_token_crea_y_reusa_el_archivo(self, monkeypatch, tmp_path):
        """Crea el archivo la primera vez y lo REUSA después (el archivo es la
        fuente de verdad, no la caché de módulo)."""
        monkeypatch.setattr(localauth, "APP_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(localauth, "_token", "")

        token = localauth.get_token()
        archivo = tmp_path / localauth.TOKEN_FILENAME
        assert archivo.read_text(encoding="ascii").strip() == token
        assert len(token) == 64

        localauth.invalidate()
        assert localauth.get_token() == token

    def test_get_token_regenera_si_el_archivo_quedo_vacio(self, monkeypatch, tmp_path):
        """Creación interrumpida a medias (archivo existe, contenido vacío)."""
        monkeypatch.setattr(localauth, "APP_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(localauth, "_token", "")
        (tmp_path / localauth.TOKEN_FILENAME).write_text("", encoding="ascii")

        token = localauth.get_token()
        assert len(token) == 64
        assert (tmp_path / localauth.TOKEN_FILENAME).read_text(encoding="ascii").strip() == token
