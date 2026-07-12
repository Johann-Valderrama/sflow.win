"""Tests para la unidad 0.1 'fix de concurrencia start/stop de reunión' (F1).

Bug cerrado: MeetingSession.stop() ponía ``_active=False`` al instante pero
tardaba segundos-minutos en terminar (joins + 2 llamadas LLM: generate_minutes
y generate_chapters). Durante esa ventana ``is_active()`` ya devolvía False y
un 2º start (desde main.py o el dashboard) podía arrancar una reunión B
mientras stop(A) seguía leyendo/mutando atributos de instancia EN VIVO.

Cubre el diseño cerrado (ver core/meeting.py):
  - ``start()`` rechaza mientras ``stop()`` sigue drenando/generando el acta
    (``is_stopping()``), aunque ``is_active()`` ya sea False.
  - ``stop()`` marca ``_stopping=True`` bajo el lock ANTES de soltar, y SIEMPRE
    lo baja en el ``finally``, incluso si algo dentro del cuerpo (acta,
    persistencia) lanza una excepción.
  - Los daemons de insights/consolidación llevan un token de generación
    (``_session_gen``): si para cuando terminan ya arrancó una reunión B,
    descartan su merge en vez de corromper el estado de B (y SIEMPRE liberan
    ``_insight_running``, incluida la ruta de descarte).
  - Doble ``stop()`` concurrente: solo uno hace el cierre real; el otro es un
    early-return limpio ("already_stopped").

Sin audio real (MicSource/LoopbackSource se reemplazan por fakes no-op) y sin
LLM real (generate_minutes/generate_chapters/update_state/consolidate se
mockean) — mismo patrón que tests/test_meeting_live.py y
tests/test_cross_memory.py.
"""

import threading
import time as _time

import pytest

import core.insights as insights_mod
from core.meeting import MeetingSession


class _FakeSource:
    """Reemplaza MicSource/LoopbackSource: no toca hardware real, start/stop no-op."""

    def start(self, callback):
        return None

    def stop(self):
        return None


@pytest.fixture(autouse=True)
def fake_audio_sources(monkeypatch):
    """Ningún test de este módulo debe tocar sounddevice/pyaudiowpatch reales."""
    monkeypatch.setattr("core.meeting.MicSource", _FakeSource)
    monkeypatch.setattr("core.meeting.LoopbackSource", _FakeSource)


@pytest.fixture(autouse=True)
def fast_llm(monkeypatch):
    """Acta/capítulos instantáneos y vacíos por defecto (sin red, sin LLM real).

    Los tests que necesitan un stop() LENTO sobrescriben generate_minutes
    dentro del propio test (con su propio monkeypatch, no afecta a los demás).
    """
    monkeypatch.setattr(insights_mod, "generate_minutes", lambda *a, **kw: {
        "resumen": "", "temas": [], "decisiones": [], "pendientes": [], "propuestas": [],
    })
    monkeypatch.setattr(insights_mod, "generate_chapters", lambda *a, **kw: [])


# ---------------------------------------------------------------------------
# (a) start() durante un stop() lento → rechazado y sin tocar el estado de A
# ---------------------------------------------------------------------------

class TestStartRejectedWhileStopping:
    def test_start_rejected_during_slow_stop_then_succeeds_after(self, monkeypatch):
        entered = threading.Event()
        release = threading.Event()

        def slow_generate_minutes(*a, **kw):
            entered.set()
            assert release.wait(timeout=5), "el test no liberó generate_minutes a tiempo"
            return {"resumen": "", "temas": [], "decisiones": [], "pendientes": [], "propuestas": []}

        monkeypatch.setattr(insights_mod, "generate_minutes", slow_generate_minutes)

        m = MeetingSession()
        res_start1 = m.start()
        assert res_start1["ok"] is True
        gen_a = m._session_gen

        stop_result = {}

        def _stop_worker():
            stop_result["res"] = m.stop()

        t = threading.Thread(target=_stop_worker, daemon=True)
        t.start()

        assert entered.wait(timeout=5), "stop() no llegó a generate_minutes a tiempo"
        # Ventana crítica del bug F1: is_active() ya False, pero el cierre sigue en curso.
        assert m.is_active() is False
        assert m.is_stopping() is True

        res_start2 = m.start()
        assert res_start2["ok"] is False
        assert res_start2.get("stopping") is True
        assert res_start2.get("error")  # mensaje accionable, no vacío
        # El rechazo NO debe haber tocado el estado de la reunión que se está cerrando.
        assert m._session_gen == gen_a

        # Dejar que stop() termine su acta.
        release.set()
        t.join(timeout=10)
        assert not t.is_alive(), "stop() no terminó a tiempo"
        assert stop_result["res"]["ok"] is True
        assert m.is_stopping() is False

        # Ahora sí debe poder arrancar una reunión nueva.
        res_start3 = m.start()
        assert res_start3["ok"] is True
        assert m._session_gen == gen_a + 1

        # Limpieza: cerrar la reunión B para no dejar threads colgando entre tests.
        m.stop()


# ---------------------------------------------------------------------------
# (b) daemon con gen viejo NO mergea (insight update y consolidación)
# ---------------------------------------------------------------------------

class TestStaleGenerationDiscardsMerge:
    def test_stale_insight_update_discards_merge_but_clears_flag(self, monkeypatch):
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen_a = m._session_gen
        plain_prev = m._store_to_plain()

        monkeypatch.setattr(insights_mod, "update_state", lambda *a, **kw: {
            "temas": ["tema colado de A"],
            "pendientes": [], "propuestas": [], "citas": [],
        })

        # Simula que una reunión B arrancó (start() real incrementa _session_gen)
        # mientras el daemon de A seguía en vuelo con el gen viejo capturado.
        m._session_gen += 1
        m._insight_running = True  # como lo deja _maybe_update_insights antes de lanzar el hilo

        m._run_insight_update(plain_prev, "[00:01] Yo: hola", gen_a)

        assert m._insights["temas"] == []  # el merge de A se descartó, no contaminó a B
        assert m._insight_running is False  # el flag SIEMPRE se libera, incluida la ruta de descarte

    def test_fresh_gen_insight_update_merges_normally(self, monkeypatch):
        """Control: con el gen vigente, el merge SÍ se aplica (no rompimos el camino feliz)."""
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen_a = m._session_gen
        plain_prev = m._store_to_plain()

        monkeypatch.setattr(insights_mod, "update_state", lambda *a, **kw: {
            "temas": ["tema real"],
            "pendientes": [], "propuestas": [], "citas": [],
        })
        m._insight_running = True

        m._run_insight_update(plain_prev, "[00:01] Yo: hola", gen_a)

        assert [t["text"] for t in m._insights["temas"]] == ["tema real"]
        assert m._insight_running is False

    def test_stale_consolidation_discards_merge_but_clears_flag(self, monkeypatch):
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        gen_a = m._session_gen

        monkeypatch.setattr(insights_mod, "consolidate", lambda transcript, prev: {
            "temas": ["tema colado de A"],
            "pendientes": [], "propuestas": [], "citas": [],
        })

        m._session_gen += 1  # B arrancó mientras la consolidación de A seguía en vuelo
        m._insight_running = True

        m._run_consolidation(gen_a)  # no debe lanzar

        assert m._insights["temas"] == []
        assert m._insight_running is False


# ---------------------------------------------------------------------------
# (c) doble stop() concurrente → solo uno efectúa el cierre real
# ---------------------------------------------------------------------------

class TestDoubleStopConcurrent:
    def test_second_stop_is_clean_early_return(self):
        m = MeetingSession()
        res_start = m.start()
        assert res_start["ok"] is True

        results = []
        results_lock = threading.Lock()

        def _stop_worker():
            r = m.stop()
            with results_lock:
                results.append(r)

        t1 = threading.Thread(target=_stop_worker, daemon=True)
        t2 = threading.Thread(target=_stop_worker, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive()
        assert len(results) == 2
        already_stopped = [r for r in results if r.get("already_stopped")]
        real_stop = [r for r in results if not r.get("already_stopped")]
        # La sección bajo lock al inicio de stop() decide esto de forma atómica:
        # exactamente un hilo hace el cierre real, el otro ve _active=False y
        # sale limpio sin tocar nada.
        assert len(real_stop) == 1
        assert len(already_stopped) == 1
        assert real_stop[0]["ok"] is True
        assert already_stopped[0] == {"ok": True, "already_stopped": True}
        assert m.is_stopping() is False


# ---------------------------------------------------------------------------
# (d) excepción dentro de stop() → _stopping se libera igual (finally)
# ---------------------------------------------------------------------------

class TestStopExceptionClearsStoppingFlag:
    def test_exception_in_stop_body_still_clears_stopping_and_unblocks_start(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("acta rota")

        monkeypatch.setattr(insights_mod, "generate_minutes", boom)

        m = MeetingSession()
        res_start = m.start()
        assert res_start["ok"] is True

        with pytest.raises(RuntimeError):
            m.stop()

        # El finally corrió pese a la excepción: no queda bloqueado rechazando
        # cualquier start() futuro.
        assert m.is_stopping() is False
        assert m.is_active() is False  # ya se había puesto en la Fase 1, antes del try

        res_start2 = m.start()
        assert res_start2["ok"] is True

        # Limpieza: restaurar generate_minutes rápido antes del stop() final
        # (el "boom" de este test seguiría activo hasta el teardown del fixture).
        monkeypatch.setattr(insights_mod, "generate_minutes", lambda *a, **kw: {
            "resumen": "", "temas": [], "decisiones": [], "pendientes": [], "propuestas": [],
        })
        m.stop()
