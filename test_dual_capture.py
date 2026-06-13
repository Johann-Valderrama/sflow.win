"""Fase 0 (modo reunión): spike de CAPTURA DUAL simultánea mic + loopback.

Esta es la pieza no probada de la que depende todo el modo reunión: ¿pueden
correr a la vez sounddevice (MicSource) y pyaudiowpatch (LoopbackSource) sin
pisarse por recursos WASAPI, sin glitches, y con un drift de reloj aceptable?

Lanza ambas fuentes en paralelo, cada una con su propio buffer y timestamps de
llegada, durante N segundos (default 30). Al terminar informa:
  - si ambos canales recibieron audio (RMS),
  - tasa efectiva de muestras por canal vs. la nominal (16 kHz) → DRIFT,
  - huecos máximos entre callbacks (indicador de GLITCH/underrun),
  - y guarda dos WAV separados (mic_yo.wav / loopback_ellos.wav) para escuchar.

Diarización gratis: tu voz va por el mic ("Yo"), el audio remoto por el
loopback ("Ellos"). Mientras corre: habla al micrófono Y reproduce algo con
sonido (un video, música) para que ambos canales tengan señal.

Uso:
    venv\\Scripts\\python test_dual_capture.py [segundos]
"""
import sys
import time
import wave
import threading

import numpy as np

from config import SAMPLE_RATE
from core.recorder import MicSource, LoopbackSource

# La consola de Windows (cp1252) no puede imprimir símbolos Unicode; forzamos UTF-8
# con reemplazo seguro para que el veredicto nunca rompa por encoding.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _play_test_tone(seconds: int):
    """Reproduce un tono en bucle por la salida por defecto para dar señal al loopback.

    Garantiza que el canal "Ellos" tenga audio aunque no haya nada más sonando
    (WASAPI loopback no entrega buffers en silencio total). Best-effort: si winsound
    no está disponible, el spike sigue y solo avisará de silencio en el loopback.
    """
    try:
        import io
        import winsound
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            t = np.arange(48000 * seconds) / 48000.0
            # Mezcla de dos tonos para que se note como "voz remota" sintética
            tone = (np.sin(2 * np.pi * 330 * t) + 0.5 * np.sin(2 * np.pi * 550 * t))
            w.writeframes((tone / 1.5 * 9000).astype(np.int16).tobytes())
        data = buf.getvalue()
        # winsound no admite SND_MEMORY|SND_ASYNC juntos: reproducimos en bloqueo
        # dentro de un hilo daemon (mismo patrón que test_loopback.py).
        threading.Thread(
            target=lambda: winsound.PlaySound(data, winsound.SND_MEMORY),
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        print(f"  (aviso: no se pudo reproducir tono de prueba: {exc!r})", flush=True)


def _record_channel(source, label, store):
    """Arranca una AudioSource y acumula (frames, timestamps de llegada)."""
    frames = store["frames"]
    arrivals = store["arrivals"]
    t0 = time.monotonic()
    store["t_start"] = t0

    def _cb(chunk: np.ndarray):
        # chunk llega como (N, 1) int16 16 kHz mono — igual para mic y loopback
        arrivals.append((time.monotonic() - t0, chunk.shape[0]))
        frames.append(chunk.copy())

    try:
        source.start(_cb)
        store["started"] = True
    except Exception as exc:  # noqa: BLE001 — queremos ver cualquier fallo de arranque
        store["error"] = repr(exc)
        store["started"] = False


def _save_wav(path, frames):
    if not frames:
        return
    data = np.concatenate(frames, axis=0)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(data.tobytes())


def _analyze(label, store, wall_seconds):
    frames = store["frames"]
    arrivals = store["arrivals"]
    print(f"\n=== Canal {label} ===")
    if store.get("error"):
        print(f"  FALLO al arrancar: {store['error']}")
        return None
    if not frames:
        print("  FALLO: no llegó ningún bloque de audio.")
        return None

    total_samples = sum(f.shape[0] for f in frames)
    data = np.concatenate(frames, axis=0).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(data**2)))
    peak = float(np.abs(data).max())

    effective_rate = total_samples / wall_seconds
    drift_pct = (effective_rate - SAMPLE_RATE) / SAMPLE_RATE * 100.0

    # Huecos entre llegadas de callback (gap = glitch si es muy grande)
    times = [a[0] for a in arrivals]
    gaps = np.diff(times) if len(times) > 1 else np.array([0.0])
    max_gap = float(gaps.max()) if len(gaps) else 0.0
    mean_gap = float(gaps.mean()) if len(gaps) else 0.0

    print(f"  callbacks         : {len(arrivals)}")
    print(f"  muestras totales  : {total_samples}  ({total_samples/SAMPLE_RATE:.2f}s de audio)")
    print(f"  tasa efectiva     : {effective_rate:.1f} Hz  (nominal {SAMPLE_RATE})")
    print(f"  DRIFT vs nominal  : {drift_pct:+.3f}%")
    print(f"  gap callback medio: {mean_gap*1000:.1f} ms   máx: {max_gap*1000:.1f} ms")
    print(f"  RMS={rms:.5f}  pico={peak:.5f}  "
          + ("(senal OK)" if rms > 1e-4 else "(SILENCIO - sin audio en este canal)"))

    return {
        "samples": total_samples,
        "effective_rate": effective_rate,
        "drift_pct": drift_pct,
        "max_gap_ms": max_gap * 1000,
        "rms": rms,
    }


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"Spike de captura dual mic + loopback — {seconds}s")
    print("Mientras corre: HABLA al micrófono y REPRODUCE algo con sonido (video/música).")
    print("Cuenta atrás para empezar...", flush=True)
    for i in (3, 2, 1):
        print(f"  {i}...", flush=True)
        time.sleep(1)
    print("GRABANDO!\n", flush=True)

    # Tono de prueba para garantizar señal en el loopback (best-effort, no bloquea)
    _play_test_tone(seconds)

    mic_store = {"frames": [], "arrivals": [], "started": False, "error": None}
    sys_store = {"frames": [], "arrivals": [], "started": False, "error": None}

    mic = MicSource()
    loop = LoopbackSource()

    wall_start = time.monotonic()
    # Arrancar ambas fuentes en hilos para que el coste de apertura no sesgue el t0
    t_mic = threading.Thread(target=_record_channel, args=(mic, "Yo (mic)", mic_store))
    t_sys = threading.Thread(target=_record_channel, args=(loop, "Ellos (loopback)", sys_store))
    t_mic.start()
    t_sys.start()
    t_mic.join()
    t_sys.join()

    time.sleep(seconds)

    mic.stop()
    loop.stop()
    wall_seconds = time.monotonic() - wall_start

    print(f"\nDetenido. Tiempo de pared real: {wall_seconds:.2f}s")

    mic_res = _analyze("Yo (mic)", mic_store, wall_seconds)
    sys_res = _analyze("Ellos (loopback)", sys_store, wall_seconds)

    _save_wav("mic_yo.wav", mic_store["frames"])
    _save_wav("loopback_ellos.wav", sys_store["frames"])
    print("\nWAV guardados: mic_yo.wav / loopback_ellos.wav (escúchalos para validar calidad).")

    # ---------------- Veredicto ----------------
    print("\n========== VEREDICTO ==========")
    ok = True
    if not mic_res:
        print("[X] Mic NO capturo. La captura dual no es viable con esta configuracion.")
        ok = False
    if not sys_res:
        print("[X] Loopback NO capturo. Revisa que haya un dispositivo de salida activo.")
        ok = False

    if mic_res and sys_res:
        # Drift entre relojes: diferencia de muestras acumuladas entre ambos
        drift_between = mic_res["effective_rate"] - sys_res["effective_rate"]
        drift_ms_per_min = abs(drift_between) / SAMPLE_RATE * 60_000
        print(f"Drift relativo mic<->loopback: {drift_between:+.1f} Hz "
              f"(~{drift_ms_per_min:.0f} ms de desalineacion por minuto)")

        glitch_thr = 250  # ms — un hueco mayor sugiere underrun/competencia de recursos
        mic_glitch = mic_res["max_gap_ms"] > glitch_thr
        sys_glitch = sys_res["max_gap_ms"] > glitch_thr
        if mic_glitch or sys_glitch:
            print(f"[!] Glitch: hueco de callback > {glitch_thr} ms detectado "
                  f"(mic={mic_res['max_gap_ms']:.0f}ms, loop={sys_res['max_gap_ms']:.0f}ms).")
            ok = False
        else:
            print(f"[OK] Sin glitches: huecos max por debajo de {glitch_thr} ms en ambos canales.")

        if abs(mic_res["drift_pct"]) > 2 or abs(sys_res["drift_pct"]) > 2:
            print("[!] Drift > 2% en algun canal: el resampling o el reloj se desvian mas de lo esperado.")
            ok = False
        else:
            print("[OK] Drift por canal dentro de +/-2% (alinear por timestamp de llegada es suficiente).")

    print("\n" + ("[VIABLE] CAPTURA DUAL OK - el modo reunion puede construirse sobre esta base."
                  if ok else
                  "[PROBLEMA] Revisa los avisos antes de seguir con el modo reunion."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
