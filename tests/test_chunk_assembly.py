"""Tests de la unidad 0.2: join con gracia de workers de chunk en vuelo.

Cubre `main._join_pending_chunks`, la función pura extraída del hot path de
`_transcribe_final` para evitar la pérdida silenciosa de un chunk cuyo worker
todavía espera respuesta de la API cuando el tramo final de un dictado largo
le gana la carrera de ensamblado.

Se testea la función standalone (threads sintéticos, sin audio ni API real)
en vez de instanciar `VflowApp` completo (requiere QApplication, hotkeys,
recorder, etc.) — ver brief de la unidad 0.2.
"""

import threading
import time

from main import _join_pending_chunks


def _make_thread(target):
    t = threading.Thread(target=target, daemon=True)
    return t


class TestJoinPendingChunks:
    def test_worker_finishes_within_grace_is_not_lost(self):
        """(a) Un worker lento que termina DENTRO de la gracia no se reporta perdido.

        Esta es la carrera que hoy pierde texto en silencio: el tramo final
        ensambla _chunk_results antes de que el worker anterior termine de
        escribir su resultado. Con el join, el caller espera a que termine
        antes de leer _chunk_results, así que su texto sí entraría al ensamblado.
        """
        results = {}

        def worker():
            time.sleep(0.3)
            results["done"] = True

        t = _make_thread(worker)
        t.start()

        lost = _join_pending_chunks(
            [t], gen=1, get_generation=lambda: 1, grace_seconds=2.0
        )

        assert lost is False
        assert results.get("done") is True
        assert not t.is_alive()

    def test_worker_exceeds_grace_is_reported_lost(self):
        """(b) Un worker que excede la gracia se detecta vivo y se reporta perdido.

        El texto parcial (de otros workers/el tramo final) se ensambla igual;
        esta función solo informa si hubo pérdida, no bloquea el ensamblado.
        """
        release = threading.Event()

        def worker():
            release.wait(timeout=5.0)

        t = _make_thread(worker)
        t.start()
        try:
            lost = _join_pending_chunks(
                [t], gen=1, get_generation=lambda: 1, grace_seconds=0.3
            )
            assert lost is True
            assert t.is_alive()
        finally:
            # Limpieza: liberar el worker para no dejar threads colgando en la suite.
            release.set()
            t.join(timeout=2.0)

    def test_generation_change_during_join_aborts_without_alarm(self):
        """(c) Si la generación cambia durante la espera, se corta rápido y no se alarma.

        Simula al usuario iniciando un dictado nuevo mientras el join anterior
        sigue esperando: debe abortar mucho antes de agotar la gracia total.
        """
        release = threading.Event()

        def worker():
            release.wait(timeout=5.0)

        t = _make_thread(worker)
        t.start()

        state = {"gen": 1}

        def flip_generation_soon():
            time.sleep(0.25)
            state["gen"] = 2  # el usuario ya inició un dictado nuevo

        flipper = threading.Thread(target=flip_generation_soon, daemon=True)
        flipper.start()

        try:
            start = time.monotonic()
            lost = _join_pending_chunks(
                [t],
                gen=1,
                get_generation=lambda: state["gen"],
                grace_seconds=5.0,  # gracia larga: el abort debe ganarle, no la gracia
            )
            elapsed = time.monotonic() - start

            assert lost is False
            # Debe abortar muy por debajo de la gracia total (5s): confirma que
            # cortó por cambio de generación, no porque el worker terminó solo.
            assert elapsed < 1.5
        finally:
            flipper.join(timeout=2.0)
            release.set()
            t.join(timeout=2.0)

    def test_silent_chunk_worker_does_not_trigger_alarm(self):
        """(d) Un worker que termina sin escribir nada (chunk silencioso) no alarma.

        Un chunk sin texto detectado (`if text:` en _chunk_worker) es silencio
        legítimo, no una pérdida — la detección es SIEMPRE por worker vivo tras
        la gracia, nunca por huecos de índice en _chunk_results.
        """

        def worker():
            pass  # termina de inmediato, sin escribir en _chunk_results

        t = _make_thread(worker)
        t.start()
        t.join(timeout=2.0)  # dar tiempo real a que termine antes del join lógico

        lost = _join_pending_chunks(
            [t], gen=1, get_generation=lambda: 1, grace_seconds=2.0
        )

        assert lost is False

    def test_no_pending_threads_returns_false(self):
        """Lista vacía (sin workers en vuelo) nunca reporta pérdida."""
        lost = _join_pending_chunks(
            [], gen=1, get_generation=lambda: 1, grace_seconds=2.0
        )
        assert lost is False

    def test_multiple_workers_one_slow_one_fast(self):
        """Un worker rápido y uno lento (ambos dentro de gracia): ninguno se pierde."""
        order = []

        def fast_worker():
            order.append("fast")

        def slow_worker():
            time.sleep(0.3)
            order.append("slow")

        t_fast = _make_thread(fast_worker)
        t_slow = _make_thread(slow_worker)
        t_fast.start()
        t_slow.start()

        lost = _join_pending_chunks(
            [t_fast, t_slow], gen=1, get_generation=lambda: 1, grace_seconds=2.0
        )

        assert lost is False
        assert set(order) == {"fast", "slow"}
