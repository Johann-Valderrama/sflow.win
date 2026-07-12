"""Tests de carrera sintéticos para la unidad 1.1 (concurrencia del gate proactivo).

``ProactiveGate`` (core/proactive.py) es mutado desde 3 hilos reales de la app
(tick Qt, daemon de insight, daemon de consolidación — ver core/meeting.py). Antes
de esta unidad no tenía ningún lock: ``_purge_expired`` podía reasignar
``self._queue`` mientras otro hilo hacía ``append`` sobre la lista vieja (tarjeta
perdida), y ``should_push``/``mark_pushed`` eran llamadas separadas (TOCTOU: dos
hilos podían ver presupuesto disponible antes de que ninguno lo marcara, y ambos
empujaban tarjeta).

Estos tests ejercitan la clase con HILOS REALES (no solo timestamps sintéticos)
para que una regresión de concurrencia (lock retirado, helper que vuelve a
adquirir el lock, etc.) falle de forma determinista y no solo "a veces" en
producción.

También cubre el _last_error por tarea de core/insights.py (unidad 1.1, punto 5):
un error de la tarea "batch" no debe pisar/limpiar el de "live" ni viceversa.
"""
import threading
import time as _time

import pytest

from core import insights
from core.proactive import ProactiveGate


# ---------------------------------------------------------------------------
# (a) N hilos encolando mientras otro hace pop_deliverable/purge en loop:
#     ninguna tarjeta se pierde ni explota (cuenta total consistente).
# ---------------------------------------------------------------------------

class TestConcurrentEnqueuePop:
    def test_no_card_lost_under_concurrent_enqueue_and_pop(self):
        gate = ProactiveGate()
        n_producers = 8
        per_producer = 50
        total = n_producers * per_producer

        delivered = []
        delivered_lock = threading.Lock()
        stop = threading.Event()

        def producer(idx: int):
            for i in range(per_producer):
                gate.enqueue({"key": f"p{idx}-{i}", "tipo": "deteccion", "texto": "x"})

        def consumer():
            # lull=True fuerza entrega inmediata de la más vieja (sin esperar los
            # 60s de forced-delivery): ejercita pop+purge bajo carga real de
            # enqueues concurrentes hasta que la cola quede vacía.
            while not stop.is_set() or gate.queue_size() > 0:
                card = gate.pop_deliverable(lull=True)
                if card is not None:
                    with delivered_lock:
                        delivered.append(card["key"])

        consumer_thread = threading.Thread(target=consumer)
        producers = [threading.Thread(target=producer, args=(i,)) for i in range(n_producers)]

        consumer_thread.start()
        for t in producers:
            t.start()
        for t in producers:
            t.join()
        stop.set()
        consumer_thread.join(timeout=10)

        assert not consumer_thread.is_alive(), "el consumidor no drenó la cola a tiempo"
        # Ni perdidas ni duplicadas: exactamente las `total` keys, una vez cada una.
        assert len(delivered) == total
        assert len(set(delivered)) == total
        assert gate.queue_size() == 0


# ---------------------------------------------------------------------------
# (b) try_push concurrente desde varios hilos con presupuesto para 1 → exactamente
#     1 push gana (antes: should_push/mark_pushed separados permitían que 2+
#     hilos vieran presupuesto libre antes de que ninguno lo consumiera).
# ---------------------------------------------------------------------------

class TestTryPushAtomic:
    def test_try_push_concurrent_exactly_one_wins(self):
        gate = ProactiveGate()
        n_threads = 20
        barrier = threading.Barrier(n_threads)
        results = [None] * n_threads

        def worker(idx: int):
            barrier.wait()  # maximiza la ventana de carrera: todos llegan a la vez
            results[idx] = gate.try_push(
                "deteccion", {"key": f"k{idx}", "tipo": "deteccion", "texto": "x"}
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        wins = [r for r in results if r]
        assert len(wins) == 1, f"se esperaba exactamente 1 push ganador, hubo {len(wins)}"
        assert gate.queue_size() == 1

    def test_try_push_dedup_rejection_does_not_consume_budget(self):
        """(c) Si el dedup de enqueue rechaza la tarjeta, el presupuesto NO se
        consume: mark_pushed solo debe ocurrir tras un enqueue exitoso."""
        gate = ProactiveGate()
        # t0 grande (mismo truco que test_proactive.py): con _last_nonpending_push_at
        # en 0.0 por defecto, un `now` chico (p.ej. 100) aún no pasa el presupuesto
        # de 300s aunque nunca se haya consumido — no es lo que este test ejercita.
        t0 = 1000.0
        # Encolar y entregar 'dup' para que quede en delivered_keys (dedup futuro).
        gate.enqueue({"key": "dup", "tipo": "deteccion", "texto": "x"}, now=t0)
        card = gate.pop_deliverable(lull=True, now=t0)
        assert card is not None and card["key"] == "dup"

        # Reintentar la MISMA key: enqueue debe rechazarla (ya entregada) → el
        # try_push completo debe fallar SIN consumir presupuesto.
        ok = gate.try_push(
            "deteccion", {"key": "dup", "tipo": "deteccion", "texto": "y"}, now=t0 + 100
        )
        assert ok is False
        # Presupuesto intacto: una key NUEVA debe poder pasar should_push ahora.
        assert gate.should_push("otra-clase", now=t0 + 100.1) is True
        card2 = gate.try_push(
            "deteccion", {"key": "nueva", "tipo": "deteccion", "texto": "z"}, now=t0 + 100.2
        )
        assert card2 is True
        assert gate.queue_size() == 1  # solo 'nueva' quedó en cola ('dup' fue rechazada)

    def test_try_push_concurrent_with_dedup_and_distinct_keys_mixed(self):
        """Réplica más realista del bug original: varios hilos compitiendo, algunos
        con la MISMA key (deben deduplicarse sin tocar presupuesto) y otros con
        keys distintas (compiten de verdad por el único push del presupuesto)."""
        gate = ProactiveGate()
        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results = [None] * n_threads

        def worker(idx: int):
            # La mitad comparte key ("shared"), la otra mitad tiene keys únicas.
            key = "shared" if idx % 2 == 0 else f"unique-{idx}"
            barrier.wait()
            results[idx] = gate.try_push(
                "deteccion", {"key": key, "tipo": "deteccion", "texto": "x"}
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # El presupuesto (1 push no-pendiente/5min) solo permite que UNA key
        # distinta gane, sin importar cuántos hilos compitieran por "shared".
        assert sum(1 for r in results if r) == 1
        assert gate.queue_size() == 1


# ---------------------------------------------------------------------------
# (d) _last_error por tarea (core/insights.py): un error de "batch" no pisa el
#     de "live" ni viceversa, incluso bajo escritura concurrente sostenida.
# ---------------------------------------------------------------------------

class TestLastErrorPerTask:
    @pytest.fixture(autouse=True)
    def _reset_last_error(self):
        insights._set_last_error("live", None)
        insights._set_last_error("batch", None)
        yield
        insights._set_last_error("live", None)
        insights._set_last_error("batch", None)

    def test_last_error_isolated_between_tasks_sequential(self):
        insights._set_last_error("live", "boom-live")
        insights._set_last_error("batch", "boom-batch")
        assert insights.last_error("live") == "boom-live"
        assert insights.last_error("batch") == "boom-batch"
        # Limpiar solo "batch" no debe afectar a "live".
        insights._set_last_error("batch", None)
        assert insights.last_error("live") == "boom-live"
        assert insights.last_error("batch") is None

    def test_concurrent_live_and_batch_writers_never_cross_contaminate(self):
        stop = threading.Event()
        violations = []
        violations_lock = threading.Lock()

        def live_writer():
            i = 0
            while not stop.is_set():
                insights._set_last_error("live", f"err-live-{i}")
                val = insights.last_error("batch")
                if val is not None and not val.startswith("err-batch-"):
                    with violations_lock:
                        violations.append(("batch key corrupted by live writer", val))
                i += 1

        def batch_writer():
            i = 0
            while not stop.is_set():
                insights._set_last_error("batch", f"err-batch-{i}")
                val = insights.last_error("live")
                if val is not None and not val.startswith("err-live-"):
                    with violations_lock:
                        violations.append(("live key corrupted by batch writer", val))
                i += 1

        t1 = threading.Thread(target=live_writer)
        t2 = threading.Thread(target=batch_writer)
        t1.start()
        t2.start()
        _time.sleep(0.2)
        stop.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert violations == []
        assert insights.last_error("live").startswith("err-live-")
        assert insights.last_error("batch").startswith("err-batch-")

    def test_meeting_consumer_reads_live_task_by_default(self):
        """core/meeting.py:1084 llama `_insights.last_error()` sin argumento — el
        default debe seguir siendo 'live' (contrato preservado de la unidad 1.1)."""
        insights._set_last_error("live", "live-err")
        insights._set_last_error("batch", "batch-err")
        assert insights.last_error() == "live-err"
