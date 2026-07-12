"""Tests para la unidad 3.1 'polling incremental del panel en vivo'.

Cubre el contrato NUEVO de ``GET /api/meeting`` (web/server.py) y el accessor
``MeetingSession.get_generation()`` (core/meeting.py):

  - Sin ``since`` (ausente o no-entero): respuesta COMPLETA, byte-idéntica al
    modo legado ``{status, segments, insights, last_minutes}`` (compatibilidad).
  - Con ``since=N`` válido: shape incremental
    ``{gen, status, insights, last_minutes, segments_from, segments, total}``,
    con ``segments`` siendo solo el delta desde el índice N.
  - ``since`` fuera de rango (> total) o negativo: clamp, nunca 500.
  - ``since`` no-entero: Flask ``request.args.get(..., type=int)`` devuelve
    None ante un ValueError de conversión → se trata exactamente como
    "ausente" (mismo shape legado completo).
  - Cambio de generación entre la lectura de segmentos y la de `gen`: el
    servidor lee segmentos PRIMERO y `gen` DESPUÉS (documentado en
    ``web/server.py::meeting_status``), así que el `gen` devuelto nunca queda
    "atrasado" respecto a los segmentos — garantiza que el cliente detecte
    SIEMPRE el cambio de generación por mismatch de `gen`.
  - Los tests existentes de status()/transcript_segments() (test_meeting_live,
    test_assistant_live) no se tocan y siguen verdes (verificado en el run
    conjunto de la suite, no repetido aquí).

Sin audio real ni LLM: se opera directamente sobre los atributos internos del
singleton ``MEETING`` (mismo patrón que tests/test_meeting_feedback.py y
tests/test_assistant_live.py::TestEndpointRouting), con monkeypatch para que
cada test revierta su propio estado.
"""

import pytest


def _seg(t, speaker, text):
    return {"t": float(t), "speaker": speaker, "text": text}


@pytest.fixture()
def client():
    from web import server as web_server
    web_server.app.config["TESTING"] = True
    with web_server.app.test_client() as c:
        yield c


@pytest.fixture()
def meeting_segments(monkeypatch):
    """300 segmentos sintéticos sobre el singleton MEETING, revertidos al cerrar."""
    from web import server as web_server
    segs = [_seg(i, "Yo" if i % 2 == 0 else "Ellos", f"segmento numero {i}") for i in range(300)]
    monkeypatch.setattr(web_server.MEETING, "_segments", segs)
    return segs


# ---------------------------------------------------------------------------
# 1. Sin `since` -> shape completo actual (compatibilidad)
# ---------------------------------------------------------------------------

class TestFullModeUnchanged:
    def test_no_since_returns_full_legacy_shape(self, client, meeting_segments):
        from web import server as web_server
        r = client.get("/api/meeting")
        assert r.status_code == 200
        data = r.get_json()
        # Shape legado exacto: exactamente estas 4 claves, nada de gen/total/segments_from.
        assert set(data.keys()) == {"status", "segments", "insights", "last_minutes"}
        assert len(data["segments"]) == 300
        # Contenido idéntico al que devuelven las llamadas directas (mismo dato).
        assert data["status"] == web_server.MEETING.status()
        assert data["segments"] == web_server.MEETING.transcript_segments()
        assert data["insights"] == web_server.MEETING.get_insights()
        assert data["last_minutes"] == web_server.MEETING.get_last_minutes()

    def test_non_integer_since_treated_as_absent(self, client, meeting_segments):
        """`since=abc` no es parseable como int: Flask type=int devuelve None,
        que el handler trata EXACTAMENTE igual que `since` ausente (full shape)."""
        r = client.get("/api/meeting?since=abc")
        assert r.status_code == 200
        data = r.get_json()
        assert set(data.keys()) == {"status", "segments", "insights", "last_minutes"}
        assert len(data["segments"]) == 300


# ---------------------------------------------------------------------------
# 2. `since=N` válido -> delta correcto
# ---------------------------------------------------------------------------

class TestIncrementalDelta:
    def test_since_midpoint_returns_delta_only(self, client, meeting_segments):
        r = client.get("/api/meeting?since=295")
        assert r.status_code == 200
        data = r.get_json()
        assert set(data.keys()) == {
            "gen", "status", "insights", "last_minutes", "segments_from", "segments", "total",
        }
        assert data["segments_from"] == 295
        assert data["total"] == 300
        assert len(data["segments"]) == 5
        assert [s["text"] for s in data["segments"]] == [f"segmento numero {i}" for i in range(295, 300)]
        assert isinstance(data["gen"], int)
        # status/insights/last_minutes siguen completos (ya son livianos).
        assert data["status"]["segment_count"] == 300

    def test_since_zero_returns_everything_as_delta(self, client, meeting_segments):
        r = client.get("/api/meeting?since=0")
        data = r.get_json()
        assert data["segments_from"] == 0
        assert data["total"] == 300
        assert len(data["segments"]) == 300


# ---------------------------------------------------------------------------
# 3. `since` > len -> clamp, sin 500
# ---------------------------------------------------------------------------

class TestSinceBeyondLength:
    def test_since_larger_than_total_clamps_to_empty_delta(self, client, meeting_segments):
        r = client.get("/api/meeting?since=9999")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 300
        assert data["segments_from"] == 300  # clampado a total
        assert data["segments"] == []

    def test_since_equal_total_returns_empty_delta(self, client, meeting_segments):
        r = client.get("/api/meeting?since=300")
        assert r.status_code == 200
        data = r.get_json()
        assert data["segments_from"] == 300
        assert data["segments"] == []


# ---------------------------------------------------------------------------
# 4. `since` negativo / no-entero -> no revienta
# ---------------------------------------------------------------------------

class TestSinceInvalidInputs:
    def test_negative_since_clamps_to_zero(self, client, meeting_segments):
        """Documentado: `since` negativo SÍ se trata como incremental (no como
        ausente) pero clampado a 0 — a diferencia de un `since` no-parseable,
        que Flask nunca entrega al handler como entero (ver test de arriba)."""
        r = client.get("/api/meeting?since=-5")
        assert r.status_code == 200
        data = r.get_json()
        assert "gen" in data  # modo incremental, no el legado
        assert data["segments_from"] == 0
        assert data["total"] == 300
        assert len(data["segments"]) == 300

    def test_float_like_since_treated_as_absent(self, client, meeting_segments):
        r = client.get("/api/meeting?since=1.5")
        assert r.status_code == 200
        data = r.get_json()
        assert set(data.keys()) == {"status", "segments", "insights", "last_minutes"}

    def test_empty_since_param_treated_as_absent(self, client, meeting_segments):
        r = client.get("/api/meeting?since=")
        assert r.status_code == 200
        data = r.get_json()
        assert set(data.keys()) == {"status", "segments", "insights", "last_minutes"}


# ---------------------------------------------------------------------------
# 5. Cambio de generación: orden de lectura segmentos->gen (foto atómica)
# ---------------------------------------------------------------------------

class TestGenerationOrdering:
    def test_gen_reported_is_never_older_than_segments_returned(self, client, meeting_segments, monkeypatch):
        """Simula una start() real ocurriendo justo ENTRE la lectura de segmentos
        y la lectura de gen del handler (mismo patrón de concurrencia que
        tests/test_meeting_lifecycle.py: _session_gen += 1). El handler lee
        transcript_segments() primero y get_generation() después (decisión
        documentada en web/server.py::meeting_status), así que el `gen` que
        vuelve en la respuesta debe ser el NUEVO (posterior al bump), aunque
        los segmentos devueltos sigan siendo los de ANTES del bump. Esto es lo
        que garantiza que el cliente detecte el cambio de generación por
        mismatch de `gen` en vez de depender solo de `total < since`."""
        from web import server as web_server
        m = web_server.MEETING
        old_gen = m.get_generation()
        original_transcript_segments = m.transcript_segments

        def _segments_then_bump_gen():
            segs = original_transcript_segments()
            m._session_gen += 1  # simula el start() concurrente
            return segs

        monkeypatch.setattr(m, "transcript_segments", _segments_then_bump_gen)

        r = client.get("/api/meeting?since=0")
        assert r.status_code == 200
        data = r.get_json()
        assert data["gen"] == old_gen + 1
        # Los segmentos devueltos son los de la generación VIEJA (300), no se
        # perdieron ni se mezclaron con la nueva — solo el gen quedó "adelantado".
        assert len(data["segments"]) == 300

    def test_get_generation_reflects_session_gen(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        assert m.get_generation() == m._session_gen
        m._session_gen += 3
        assert m.get_generation() == m._session_gen


# ---------------------------------------------------------------------------
# 6. El helper JS está DEFINIDO en AMBOS documentos HTML (bug de integración
#    real: el dashboard '/' y '/reunion' son documentos SEPARADOS; definir
#    fetchMeetingIncremental solo en uno deja al otro con ReferenceError en
#    cada poll — la vista en vivo entera muere).
# ---------------------------------------------------------------------------

class TestHelperPresentInBothDocuments:
    DEFINITION = "async function fetchMeetingIncremental"

    def test_dashboard_document_defines_helper(self, client):
        r = client.get("/")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert self.DEFINITION in html, (
            "el documento del dashboard ('/') no DEFINE fetchMeetingIncremental: "
            "loadMeeting() moriría con ReferenceError en cada poll"
        )
        assert "const _mtCursors" in html
        assert "__MT_INCREMENTAL_JS__" not in html  # el placeholder se sustituyó
        assert "ó" in html  # acento real intacto (encoding del template extraído, unidad 4.1)

    def test_reunion_document_defines_helper(self, client):
        r = client.get("/reunion")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert self.DEFINITION in html, (
            "el documento de /reunion no DEFINE fetchMeetingIncremental: "
            "loadLive() moriría con ReferenceError en cada poll"
        )
        assert "const _mtCursors" in html
        assert "__MT_INCREMENTAL_JS__" not in html  # el placeholder se sustituyó
        assert "ó" in html  # acento real intacto (encoding del template extraído, unidad 4.1)

    def test_every_document_that_calls_helper_also_defines_it(self, client):
        """Genérico contra la regresión: CADA documento servido que INVOQUE
        fetchMeetingIncremental( debe contener también su definición."""
        for route in ("/", "/reunion"):
            html = client.get(route).get_data(as_text=True)
            if "fetchMeetingIncremental(" in html.replace(self.DEFINITION, ""):
                assert self.DEFINITION in html, (
                    f"{route} llama fetchMeetingIncremental pero no lo define"
                )


# ---------------------------------------------------------------------------
# 7. No se tocan los tests existentes de status()/segments (control de humo)
# ---------------------------------------------------------------------------

class TestExistingContractsUntouched:
    def test_status_shape_unchanged(self, meeting_segments):
        from web import server as web_server
        st = web_server.MEETING.status()
        assert set(st.keys()) == {
            "active", "started_at", "elapsed", "elapsed_fmt", "segment_count",
            "sys_available", "insight_running", "error", "paused", "levels",
            "template", "template_label", "proactive_mode", "cards",
        }

    def test_transcript_segments_signature_unchanged(self, meeting_segments):
        from web import server as web_server
        segs = web_server.MEETING.transcript_segments()
        assert len(segs) == 300
        assert set(segs[0].keys()) == {"t", "time", "speaker", "text"}
