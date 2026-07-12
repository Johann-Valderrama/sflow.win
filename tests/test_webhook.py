"""Tests de la unidad 6.1 'Webhook saliente al generar acta' (core/webhook.py).

Cubre (sin salir a internet real salvo el receptor local del flujo end-to-end):
  (a) firma HMAC verificable con el secreto;
  (b) anti-SSRF: loopback/privadas/link-local RECHAZADAS sin WEBHOOK_ALLOW_LOCAL,
      aceptadas con él; https exigido por default;
  (c) payload scope=pendientes sin transcript ni resumen; scope=acta con minutes;
  (d) reintentos con backoff ante fallo (requests.post mockeado);
  (e) meeting_id None → no envía y loguea;
  (f) fallo del webhook no propaga (el hilo daemon muere solo);
  (g) dead-drop escribe la tarea v1 (YAML + vista humana, unidad 5.4/O1-O11);
      dir vacío / meeting_id None / sin pendientes = no hace nada; idempotencia
      create-only por hash de contenido; machine_id estable por instalación;
  (h) el secreto nunca aparece en el GET de settings;
  (i) flujo real local: receptor HTTP en puerto libre recibe el POST con firma válida.
"""

import hashlib
import hmac
import http.server
import json
import os
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
# (g) dead-drop local — contrato de tarea v1 (unidad 5.4, docs/CONTRATO-MACROSISTEMA.md)
# ---------------------------------------------------------------------------

def _meeting_con_pendientes(pendientes, *, meeting_id=42, title="Reunión 2026-07-12 08:42",
                             started_at="2026-07-12 08:42:00"):
    minutes = {"resumen": "x", "pendientes": pendientes}
    return {
        "id": meeting_id,
        "title": title,
        "started_at": started_at,
        "duration_seconds": 900,
        "minutes_json": json.dumps(minutes, ensure_ascii=False),
        "insights_json": json.dumps({}, ensure_ascii=False),
        # El transcript existe en la fila pero NUNCA debe salir en el dead-drop.
        "transcript": "[00:10 Yo] hola SECRETO_TRANSCRIPT",
    }


def _yaml_block(content: str) -> str:
    return content.split("\n---\n", 1)[0]


def _try_yaml_load(text: str):
    """Parsea con PyYAML si está disponible (transitiva ya instalada en este venv,
    NO agregada como dependencia nueva por esta unidad); si no, None y el test cae
    a verificación por substring del YAML manual."""
    try:
        import yaml  # noqa: PLC0415  (import perezoso solo para el test)
    except ImportError:
        return None
    return yaml.safe_load(text)


def test_deaddrop_dir_vacio_no_hace_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    m = _meeting_con_pendientes([{"texto": "Tarea"}])
    assert webhook.export_pendientes(m, "") is None


def test_deaddrop_sin_pendientes_no_escribe_archivo(monkeypatch, tmp_path):
    """O7: una reunión sin pendientes NO genera archivo (ya no escribe
    '_(sin pendientes registrados)_' como el dead-drop viejo)."""
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    drop = tmp_path / "drop"
    m = _meeting_con_pendientes([])
    path = webhook.export_pendientes(m, str(drop))
    assert path is None
    assert not drop.exists() or list(drop.glob("*.md")) == []


def test_deaddrop_meeting_id_none_no_escribe_archivo(monkeypatch, tmp_path):
    """Guard SAVE_HISTORY (B3): reunión no persistida (meeting_id None) nunca
    genera archivo, aunque tenga pendientes y export_dir esté configurado."""
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    drop = tmp_path / "drop"
    m = _meeting_con_pendientes([{"texto": "Tarea"}], meeting_id=None)
    path = webhook.export_pendientes(m, str(drop))
    assert path is None
    assert not drop.exists() or list(drop.glob("*.md")) == []


def test_deaddrop_yaml_parseable_y_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    m = _meeting_con_pendientes([
        {"texto": "Enviar el contrato a Acme", "responsable": "Johann",
         "fecha": "2026-07-15", "t": 252},
        {"texto": "Revisar presupuesto"},
    ])
    path = webhook.export_pendientes(m, str(tmp_path / "drop"))
    assert path is not None
    content = open(path, encoding="utf-8").read()
    assert "SECRETO_TRANSCRIPT" not in content
    assert os.path.basename(path).startswith("vflow-pendientes-")
    assert os.path.basename(path).endswith(".md")

    yaml_text = _yaml_block(content)
    data = _try_yaml_load(yaml_text)
    if data is not None:
        assert data["tipo"] == "tarea-vflow"
        assert data["schema_version"] == 1
        assert data["origen"] == "vflow-meeting"
        assert data["meeting_id"] == 42
        assert data["fecha_reunion"] == "2026-07-12"
        assert len(data["pendientes"]) == 2
        p0 = data["pendientes"][0]
        assert p0["texto"] == "Enviar el contrato a Acme"
        assert p0["responsable"] == "Johann"
        assert p0["due"] == {"fecha": "2026-07-15", "hora": None}
        assert p0["transcript_offset"] == "04:12"   # 252s -> 04:12
        assert len(p0["id"]) == 8
        p1 = data["pendientes"][1]
        assert p1["responsable"] is None
        assert p1["due"] == {"fecha": None, "hora": None}
        assert p1["transcript_offset"] is None
    else:
        assert "schema_version: 1" in yaml_text
        assert "tipo: tarea-vflow" in yaml_text
        assert "meeting_id: 42" in yaml_text
        assert '"Enviar el contrato a Acme"' in yaml_text
        assert '"04:12"' in yaml_text
        assert "responsable: null" in yaml_text

    # Vista humana debajo del YAML.
    human = content.split("\n---\n", 1)[1]
    assert "Enviar el contrato a Acme" in human
    assert "@Johann" in human


def test_deaddrop_naming_instalacion_meeting_id_hash(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    m = _meeting_con_pendientes([{"texto": "Tarea A"}], meeting_id=7)
    path = webhook.export_pendientes(m, str(tmp_path / "drop"))
    machine_id = webhook.get_machine_id()
    fname = os.path.basename(path)
    prefix = f"vflow-pendientes-{machine_id}-7-"
    assert fname.startswith(prefix)
    hash_part = fname[len(prefix):-len(".md")]
    assert len(hash_part) == 8
    assert all(c in "0123456789abcdef" for c in hash_part)


def test_deaddrop_ids_de_pendientes_estables_entre_exports(monkeypatch, tmp_path):
    """Mismo texto (con diferencias triviales de espacio/mayúsculas) → mismo id,
    tanto dentro de un export como entre reuniones/exports distintos."""
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    m1 = _meeting_con_pendientes([{"texto": "  Enviar   el Contrato  "}], meeting_id=1)
    m2 = _meeting_con_pendientes([{"texto": "Enviar el contrato"}], meeting_id=2)

    p1 = webhook.export_pendientes(m1, str(tmp_path / "drop"))
    p2 = webhook.export_pendientes(m2, str(tmp_path / "drop"))

    data1 = _try_yaml_load(_yaml_block(open(p1, encoding="utf-8").read()))
    data2 = _try_yaml_load(_yaml_block(open(p2, encoding="utf-8").read()))
    if data1 is not None and data2 is not None:
        assert data1["pendientes"][0]["id"] == data2["pendientes"][0]["id"]
    else:
        assert webhook._pendiente_id("  Enviar   el Contrato  ") == webhook._pendiente_id("Enviar el contrato")


def test_deaddrop_reexport_identico_es_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    drop = tmp_path / "drop"
    m = _meeting_con_pendientes([{"texto": "Tarea estable"}], meeting_id=5)

    path1 = webhook.export_pendientes(m, str(drop))
    content1 = open(path1, encoding="utf-8").read()
    path2 = webhook.export_pendientes(m, str(drop))
    content2 = open(path2, encoding="utf-8").read()

    assert path1 == path2
    assert content1 == content2
    assert len(list(drop.glob("*.md"))) == 1   # create-only: no se duplicó ni sobrescribió


def test_deaddrop_acta_cambiada_genera_archivo_nuevo_sin_tocar_el_viejo(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    drop = tmp_path / "drop"
    m1 = _meeting_con_pendientes([{"texto": "Tarea version 1"}], meeting_id=5)
    path1 = webhook.export_pendientes(m1, str(drop))
    content1_before = open(path1, encoding="utf-8").read()

    m2 = _meeting_con_pendientes([{"texto": "Tarea version 2 (editada)"}], meeting_id=5)
    path2 = webhook.export_pendientes(m2, str(drop))

    assert path1 != path2
    assert os.path.exists(path1)
    assert open(path1, encoding="utf-8").read() == content1_before  # el viejo, intacto
    assert len(list(drop.glob("*.md"))) == 2


# ---------------------------------------------------------------------------
# (g2) machine_id — id corto estable por instalación (O3)
# ---------------------------------------------------------------------------

def test_machine_id_estable_entre_dos_llamadas(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(tmp_path / "appdata"))
    id1 = webhook.get_machine_id()
    id2 = webhook.get_machine_id()
    assert id1 == id2
    assert len(id1) == 8


def test_machine_id_persiste_en_archivo(monkeypatch, tmp_path):
    data_dir = tmp_path / "appdata"
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(data_dir))
    id1 = webhook.get_machine_id()
    marker = data_dir / webhook._MACHINE_ID_FILENAME
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == id1


def test_machine_id_fallback_determinista_si_no_puede_persistir(monkeypatch, tmp_path):
    # APP_DATA_DIR apunta a un ARCHIVO (no directorio): os.makedirs falla y
    # tampoco existe machine_id.txt ahí dentro → cae al fallback determinista.
    blocked = tmp_path / "no-es-un-directorio"
    blocked.write_text("bloqueado", encoding="utf-8")
    monkeypatch.setattr(webhook, "APP_DATA_DIR", str(blocked))
    id1 = webhook.get_machine_id()
    id2 = webhook.get_machine_id()
    assert id1 == id2
    assert len(id1) == 8


# ---------------------------------------------------------------------------
# (g3) transcript_offset — mm:ss del 't' del pendiente, o null (O2: nunca fecha límite)
# ---------------------------------------------------------------------------

def test_mmss_convierte_segundos():
    assert webhook._mmss(252) == "04:12"
    assert webhook._mmss(0) == "00:00"
    assert webhook._mmss(None) is None
    assert webhook._mmss("no numérico") is None
    assert webhook._mmss(-5) is None


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
