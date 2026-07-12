"""Tests de la unidad 6.1 'Webhook saliente al generar acta' (core/webhook.py).

Cubre (sin salir a internet real salvo el receptor local del flujo end-to-end):
  (a) firma HMAC verificable con el secreto;
  (b) anti-SSRF: loopback/privadas/link-local RECHAZADAS sin WEBHOOK_ALLOW_LOCAL,
      aceptadas con él; https exigido por default;
  (c) payload scope=pendientes sin transcript ni resumen; scope=acta con minutes;
  (d) reintentos con backoff ante fallo (requests.post mockeado);
  (e) meeting_id None → no envía y loguea;
  (f) fallo del webhook no propaga (el hilo daemon muere solo);
  (g) dead-drop escribe el .md correcto; dir vacío = no hace nada;
  (h) el secreto nunca aparece en el GET de settings;
  (i) flujo real local: receptor HTTP en puerto libre recibe el POST con firma válida.
"""

import hashlib
import hmac
import http.server
import json
import threading
import time

import pytest

from core import webhook


# ---------------------------------------------------------------------------
# Fixtures de datos
# ---------------------------------------------------------------------------

def _meeting_row():
    minutes = {
        "resumen": "Hablamos del proyecto X y quedaron dos tareas.",
        "decisiones": [{"texto": "Usar Postgres", "t": 120}],
        "pendientes": [
            {"texto": "Enviar propuesta", "responsable": "Yo", "fecha": "2026-07-10"},
            "Revisar contrato",
        ],
        "notas_usuario": [{"time": "01:00", "nota": "OJO: precio literal", "contexto": "ctx"}],
    }
    return {
        "id": 42,
        "title": "Reunión 2026-07-03",
        "started_at": "2026-07-03 10:00:00",
        "duration_seconds": 1830,
        "minutes_json": json.dumps(minutes, ensure_ascii=False),
        "insights_json": json.dumps({}, ensure_ascii=False),
        "chapters_json": json.dumps([{"time": "00:30", "titulo": "Intro"}], ensure_ascii=False),
        # El transcript existe en la fila pero NUNCA debe salir en el payload.
        "transcript": "[00:10 Yo] hola SECRETO_TRANSCRIPT\n[00:20 Ellos] adios",
    }


# ---------------------------------------------------------------------------
# (a) Firma HMAC
# ---------------------------------------------------------------------------

def test_sign_hmac_verificable():
    body = b'{"hello":"world"}'
    secret = "s3cr3t"
    header = webhook.sign(body, secret)
    assert header.startswith("sha256=")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert header == expected
    # Verificación como la haría el receptor.
    got = header.split("=", 1)[1]
    assert hmac.compare_digest(got, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())


def test_sign_cambia_con_secreto():
    body = b"abc"
    assert webhook.sign(body, "uno") != webhook.sign(body, "dos")


# ---------------------------------------------------------------------------
# (b) Anti-SSRF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://127.0.0.1/hook",
    "https://192.168.1.50/hook",
    "https://10.0.0.5/hook",
    "https://169.254.1.1/hook",
    "https://[::1]/hook",
])
def test_ssrf_rechaza_destinos_internos_sin_optin(url):
    with pytest.raises(webhook.WebhookError):
        webhook.validate_url(url, allow_local=False)


def test_ssrf_rechaza_localhost_hostname(monkeypatch):
    # 'localhost' resuelve a loopback → bloqueado sin opt-in.
    with pytest.raises(webhook.WebhookError):
        webhook.validate_url("https://localhost:8000/hook", allow_local=False)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/hook",
    "https://192.168.1.50/hook",
    "http://localhost:9000/hook",
])
def test_ssrf_acepta_internos_con_optin(url):
    ip = webhook.validate_url(url, allow_local=True)
    assert ip  # devuelve una IP


def test_ssrf_https_exigido_por_default():
    with pytest.raises(webhook.WebhookError):
        webhook.validate_url("http://example.com/hook", allow_local=False)
    # http se acepta solo con opt-in local.
    # (no resolvemos example.com aquí; usamos un host interno con opt-in)
    assert webhook.validate_url("http://127.0.0.1/hook", allow_local=True)


def test_ssrf_esquema_invalido():
    with pytest.raises(webhook.WebhookError):
        webhook.validate_url("ftp://example.com/x")


def test_ssrf_ip_publica_pasa(monkeypatch):
    # Forzamos la resolución DNS a una IP pública sin tocar la red real.
    import socket as _socket

    def fake_getaddrinfo(host, port, *a, **k):
        return [(_socket.AF_INET, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr(webhook.socket, "getaddrinfo", fake_getaddrinfo)
    ip = webhook.validate_url("https://example.com/hook", allow_local=False)
    assert ip == "93.184.216.34"


# ---------------------------------------------------------------------------
# (c) Payload por scope
# ---------------------------------------------------------------------------

def test_payload_pendientes_sin_transcript_ni_resumen():
    p = webhook.build_payload(_meeting_row(), "pendientes")
    blob = json.dumps(p, ensure_ascii=False)
    assert "SECRETO_TRANSCRIPT" not in blob
    assert "transcript" not in p
    assert "minutes" not in p          # el resumen/acta no va en scope pendientes
    assert "resumen" not in blob
    assert p["scope"] == "pendientes"
    assert p["event"] == "meeting.minutes"
    assert p["meeting"]["id"] == 42
    assert p["meeting"]["duration_seconds"] == 1830
    assert len(p["pendientes"]) == 2   # sí trae los pendientes


def test_payload_acta_trae_minutes_sin_transcript():
    p = webhook.build_payload(_meeting_row(), "acta")
    blob = json.dumps(p, ensure_ascii=False)
    assert "SECRETO_TRANSCRIPT" not in blob
    assert "transcript" not in p
    assert p["scope"] == "acta"
    assert "minutes" in p
    assert p["minutes"]["resumen"].startswith("Hablamos")
    assert p["chapters"] and p["chapters"][0]["titulo"] == "Intro"


# ---------------------------------------------------------------------------
# (d) Reintentos con backoff
# ---------------------------------------------------------------------------

def test_reintentos_con_backoff(monkeypatch):
    calls = {"post": 0, "sleep": []}

    monkeypatch.setattr(webhook, "validate_url", lambda url, allow_local=False: "93.184.216.34")
    monkeypatch.setattr(webhook.time, "sleep", lambda s: calls["sleep"].append(s))

    class _Resp:
        status_code = 500

    def fake_post(url, **kw):
        calls["post"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    ok = webhook.send_webhook("https://example.com/h", {"a": 1}, "sec", max_attempts=3)
    assert ok is False
    assert calls["post"] == 3               # 3 intentos
    assert calls["sleep"] == [1.0, 2.0]     # backoff exponencial entre intentos


def test_reintento_para_al_primer_2xx(monkeypatch):
    calls = {"post": 0}
    monkeypatch.setattr(webhook, "validate_url", lambda url, allow_local=False: "1.2.3.4")
    monkeypatch.setattr(webhook.time, "sleep", lambda s: None)

    class _Resp:
        status_code = 200

    def fake_post(url, **kw):
        calls["post"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    ok = webhook.send_webhook("https://example.com/h", {"a": 1}, "sec", max_attempts=3)
    assert ok is True
    assert calls["post"] == 1


def test_redirect_3xx_es_terminal_no_reintenta(monkeypatch):
    """F3: una respuesta 307 es un fallo TERMINAL — no se reintenta ni se sigue."""
    calls = {"post": 0, "sleep": []}
    monkeypatch.setattr(webhook, "validate_url", lambda url, allow_local=False: "93.184.216.34")
    monkeypatch.setattr(webhook.time, "sleep", lambda s: calls["sleep"].append(s))

    class _Resp:
        status_code = 307

    def fake_post(url, **kw):
        calls["post"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    ok = webhook.send_webhook("https://example.com/h", {"a": 1}, "sec", max_attempts=3)
    assert ok is False
    assert calls["post"] == 1          # un solo intento, no gasta los 3
    assert calls["sleep"] == []        # no hay backoff: es terminal, no transitorio


def test_post_real_usa_allow_redirects_false(monkeypatch):
    """F3: el POST real siempre pasa allow_redirects=False (inspección de kwargs)."""
    monkeypatch.setattr(webhook, "validate_url", lambda url, allow_local=False: "93.184.216.34")
    monkeypatch.setattr(webhook.time, "sleep", lambda s: None)
    seen_kwargs = {}

    class _Resp:
        status_code = 200

    def fake_post(url, **kw):
        seen_kwargs.update(kw)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    ok = webhook.send_webhook("https://example.com/h", {"a": 1}, "sec", max_attempts=1)
    assert ok is True
    assert seen_kwargs.get("allow_redirects") is False


def test_send_no_lanza_ante_excepcion(monkeypatch):
    monkeypatch.setattr(webhook, "validate_url", lambda url, allow_local=False: "1.2.3.4")
    monkeypatch.setattr(webhook.time, "sleep", lambda s: None)
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: (_ for _ in ()).throw(ConnectionError("boom")))
    # No debe propagar: devuelve False.
    assert webhook.send_webhook("https://example.com/h", {}, "sec", max_attempts=2) is False


# ---------------------------------------------------------------------------
# (e) meeting_id None → no envía y loguea
# ---------------------------------------------------------------------------

def test_dispatch_meeting_id_none_no_envia(monkeypatch, caplog):
    started = []
    monkeypatch.setattr(webhook.threading, "Thread",
                        lambda *a, **k: started.append(True))  # no debería llamarse
    with caplog.at_level("WARNING"):
        res = webhook.dispatch_async({"id": None}, None)
    assert res is False
    assert not started
    assert any("no persistida" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# (f) fallo del webhook no propaga (el hilo muere solo)
# ---------------------------------------------------------------------------

def test_worker_aisla_errores(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_URL", "https://example.com/h")
    monkeypatch.setenv("PENDING_EXPORT_DIR", "")

    def boom(*a, **k):
        raise RuntimeError("explota")

    monkeypatch.setattr(webhook, "build_payload", boom)
    # _dispatch_worker jamás debe propagar.
    webhook._dispatch_worker(_meeting_row())  # si lanzara, el test fallaría


def test_dispatch_async_lanza_hilo_y_no_bloquea(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ENABLED", "false")  # no enviará, pero sí correrá el hilo (dead-drop)
    monkeypatch.setenv("PENDING_EXPORT_DIR", "")
    res = webhook.dispatch_async(_meeting_row(), 42)
    assert res is True
    # Damos un momento a que el hilo daemon termine su trabajo trivial.
    time.sleep(0.2)


# ---------------------------------------------------------------------------
# (g) dead-drop local
# ---------------------------------------------------------------------------

def test_deaddrop_escribe_md(tmp_path):
    path = webhook.export_pendientes(_meeting_row(), str(tmp_path))
    assert path is not None
    content = open(path, encoding="utf-8").read()
    assert "vflow-pendientes-42-2026-07-03.md" in path
    assert "- [ ] Enviar propuesta" in content
    assert "@Yo" in content
    assert "- [ ] Revisar contrato" in content
    # El transcript NO va en el dead-drop.
    assert "SECRETO_TRANSCRIPT" not in content


def test_deaddrop_dir_vacio_no_hace_nada(tmp_path):
    assert webhook.export_pendientes(_meeting_row(), "") is None


def test_deaddrop_sin_pendientes(tmp_path):
    m = _meeting_row()
    m["minutes_json"] = json.dumps({"resumen": "x"}, ensure_ascii=False)
    m["insights_json"] = json.dumps({}, ensure_ascii=False)
    path = webhook.export_pendientes(m, str(tmp_path))
    assert path is not None
    content = open(path, encoding="utf-8").read()
    assert "sin pendientes" in content.lower()


# ---------------------------------------------------------------------------
# (h) el secreto nunca aparece en GET /api/settings
# ---------------------------------------------------------------------------

def test_secreto_no_expuesto_en_settings(monkeypatch):
    # Importamos el server y llamamos a get_settings a través de su test client.
    monkeypatch.setenv("WEBHOOK_SECRET", "mi-secreto-super-privado")
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_URL", "https://example.com/h")
    import importlib
    from web import server as _server
    importlib.reload(_server)  # re-lee el entorno mockeado
    client = _server.app.test_client()
    resp = client.get("/api/settings")
    data = resp.get_json()
    blob = json.dumps(data)
    assert "mi-secreto-super-privado" not in blob
    assert data["has_webhook_secret"] is True
    assert data["webhook_enabled"] is True
    assert data["webhook_url"] == "https://example.com/h"


# ---------------------------------------------------------------------------
# (i) Flujo real local: receptor HTTP recibe el POST con firma válida
# ---------------------------------------------------------------------------

def test_flujo_real_local_recibe_post_firmado(monkeypatch):
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received["body"] = body
            received["sig"] = self.headers.get("X-Vflow-Signature")
            received["event"] = self.headers.get("X-Vflow-Event")
            received["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    secret = "shared-secret"
    payload = webhook.build_payload(_meeting_row(), "pendientes")
    ok = webhook.send_webhook(
        f"http://127.0.0.1:{port}/hook", payload, secret,
        allow_local=True, max_attempts=1,
    )
    t.join(timeout=5)
    server.server_close()

    assert ok is True
    assert received.get("event") == "meeting.minutes"
    assert received.get("ctype") == "application/json"
    # La firma recibida valida con el secreto compartido.
    body = received["body"]
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert received["sig"] == expected
    # El body es el payload minimizado, sin transcript.
    assert b"SECRETO_TRANSCRIPT" not in body
