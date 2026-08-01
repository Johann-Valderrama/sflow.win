#!/usr/bin/env python
"""Banco de medicion CPU vs CUDA para el backend local (unidad 7a, Ola 7).

NO es un test de pytest (no lo colecciona la suite: no matchea `test_*.py`/
`*_test.py`). Se corre a mano:

    venv\\Scripts\\python.exe tests\\bench_local_backend.py [--model small|medium] [--video RUTA]

Responde UNA pregunta: ¿vale la pena tocar `core/backends/local_backend.py`
para usar la GPU (GTX 1060 6GB, CUDA 12.9) en vez de CPU? Mide, no opina.
Sale con exit 0 en cuanto la medicion se completo (aunque el veredicto sea
"CPU gana"); exit != 0 si no pudo medir nada, INCLUIDO el caso en que el
audio de prueba no contiene habla real (ver el guardian mas abajo).

CORRECCION 2026-08-01 (v2): la primera version de este script media sobre
`mic_yo.wav` + `loopback_ellos.wav`, que resultaron ser silencio de piso de
ruido + un tono sintetico de prueba (ver docs/benchmarks/local-backend-gpu-
2026-08-01.md, seccion SUPERADA). Con `vad_filter=True` (igual que
produccion) el VAD descartaba esos clips COMPLETOS antes de decodificar, asi
que CPU y CUDA "empataban" porque ninguna de las dos hacia el trabajo caro
de decodificar. Esta version extrae audio REAL de un video con habla en
espanol y VERIFICA, antes de medir, que cada clip produce texto no vacio:
sin esa verificacion, un banco de latencia sobre audio sin habla es un
numero que se lee igual de bien que uno bueno, y por eso el script ABORTA
(exit != 0) si no logra un clip con habla real.

Metodologia (ver docs/PLAN-DICTADO-2026-07-31.md, unidad 7a, y el reporte en
docs/benchmarks/local-backend-gpu-2026-08-01.md):

- Compara la config EXACTA de produccion (`cpu`/`int8`, mismo `cpu_threads`
  que `core/backends/local_backend.py:294`) contra las 3 configs CUDA que el
  ctranslate2 instalado soporta en esta GPU (`int8`, `int8_float32`,
  `float32`; NO existe `float16`/`int8_float16` en Pascal, CC 6.1 < 7.0).
- 5 clips de audio REAL (nunca sintetico) de 5, 10, 15, 30 y 60s, extraidos
  de un video con habla en espanol, tomando cada clip de un MINUTO distinto
  del video (nunca 5 cortes seguidos del mismo tramo), verificados con
  VAD + transcripcion no vacia antes de usarse en la medicion.
- Carga en frio medida aparte de la inferencia. Primera inferencia de cada
  config (lazy-alloc de CTranslate2) se descarta del calculo de mediana y se
  reporta aparte, rotulada.
- N=3 repeticiones por (config, clip); se reporta mediana, min y max, nunca
  solo el promedio.
- Comparacion de TEXTO entre CPU-int8 y cada config CUDA: coincidencia
  exacta o diff de caracteres (ahora si es significativa: hay habla real).
"""
import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from difflib import SequenceMatcher

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.backends.local_backend import _get_models_dir, _is_model_downloaded  # noqa: E402

# Material de estudio PERSONAL de Johann (transcript de un video de YouTube de
# terceros, capa privada `_JOS`), usado SOLO localmente para cronometrar un
# modelo local: nunca sale de la maquina, no se commitea, no se distribuye.
# Si esta ruta ya no existe (el material se reorganizo/archivo), pasa otra
# con --video: cualquier audio con habla continua en espanol sirve (WHISPER_
# LANGUAGE default es "es"). Mas candidatos en C:\OPS\_JOS\Estudio\Youtube\.
DEFAULT_VIDEO = (
    r"C:\OPS\_JOS\Estudio\Youtube\SaaS Factory\2026.07.22_saas factory irvyn"
    r"\2026.07.22_1257_saas factory irvyn.mp4"
)

CLIP_DURATIONS_S = [5, 10, 15, 30, 60]
SAMPLE_RATE = 16000
N_REPS = 3
MIN_RMS = 0.01  # por debajo de esto se trata como silencio/piso de ruido, no habla

# Puntos de partida candidatos (segundos), esparcidos en minutos DISTINTOS a
# lo largo del video (no 5 cortes seguidos del mismo tramo). Si alguno cae en
# silencio/musica/aplausos, el guardian de verificacion pasa al siguiente.
CANDIDATE_STARTS_S = [300, 1500, 2700, 3900, 5100, 900, 2100, 3300, 4500, 5400, 600, 1800, 2400]

# Config EXACTA de produccion + las 3 configs CUDA soportadas por esta GPU
# (ctranslate2.get_supported_compute_types("cuda") en esta maquina ->
#  {'int8', 'int8_float32', 'float32'}; NO hay float16/int8_float16, Pascal).
CPU_THREADS = max(4, (os.cpu_count() or 4) // 2)
CONFIGS = [
    ("cpu_int8", dict(device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)),
    ("cuda_int8", dict(device="cuda", compute_type="int8")),
    ("cuda_int8_float32", dict(device="cuda", compute_type="int8_float32")),
    ("cuda_float32", dict(device="cuda", compute_type="float32")),
]


# ---------------------------------------------------------------------
# Extraccion de audio real desde el video (PyAV), mismo patron que
# core/url_transcribe.py:_decode_audio_to_pcm, pero con SEEK a un punto
# concreto en vez de decodificar el archivo completo (el video dura ~97min;
# decodificarlo entero para sacar 5 clips cortos seria un desperdicio).
# ---------------------------------------------------------------------

def _extract_segment(video_path: str, start_s: float, duration_s: float):
    import av  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    container = av.open(video_path)
    try:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        offset = int(start_s / stream.time_base)
        container.seek(offset, stream=stream, backward=True, any_frame=False)
        chunks = []
        total_samples = 0
        target_samples = int(duration_s * SAMPLE_RATE)
        for frame in container.decode(stream):
            if frame.time is not None and frame.time < start_s:
                continue
            for rs in resampler.resample(frame):
                arr = rs.to_ndarray().reshape(-1)
                chunks.append(arr)
                total_samples += len(arr)
            if total_samples >= target_samples:
                break
    finally:
        container.close()
    if not chunks:
        return np.array([], dtype=np.int16)
    data = np.concatenate(chunks).astype(np.int16)
    return data[:target_samples]


def _rms_peak_arr(arr) -> tuple[float, float]:
    if len(arr) == 0:
        return 0.0, 0.0
    fa = arr.astype("float64") / 32768.0
    return float((fa ** 2).mean() ** 0.5), float(abs(fa).max())


def _write_wav(path: str, arr) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(arr.tobytes())


def _quick_transcribe(model, arr) -> str:
    """Transcripcion rapida (para el guardian de verificacion), misma
    config de VAD que produccion."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(arr.tobytes())
    buf.seek(0)
    segments, _info = model.transcribe(
        buf, language="es", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    return " ".join(seg.text for seg in segments).strip()


def build_clips(video_path: str, workdir: str, verify_model) -> dict[int, str]:
    """Extrae y VERIFICA los 5 clips escalonados; aborta si no logra habla real.

    Guardian (el punto entero de la correccion v2): para cada duracion,
    prueba candidatos de CANDIDATE_STARTS_S (cada uno en un minuto no usado
    por otro clip) hasta encontrar uno con RMS>MIN_RMS Y transcripcion NO
    vacia. Si ningun candidato sirve para una duracion, el script ABORTA con
    exit != 0: un banco de latencia sobre un clip sin habla es peor que no
    medir, porque el numero se lee igual de bien que uno bueno.
    """
    if not os.path.isfile(video_path):
        sys.exit(
            f"ERROR: no existe el video de audio real '{video_path}'.\n"
            "Pasa --video con otra fuente de habla continua en espanol "
            "(candidatos en C:\\OPS\\_JOS\\Estudio\\Youtube\\)."
        )

    clips = {}
    used_minutes: set[int] = set()
    for dur in CLIP_DURATIONS_S:
        chosen = None
        for start in CANDIDATE_STARTS_S:
            minute = start // 60
            if minute in used_minutes:
                continue
            arr = _extract_segment(video_path, start, dur)
            if len(arr) < int(dur * SAMPLE_RATE * 0.9):
                continue  # decodificacion corta (cerca de EOF o seek fallido)
            rms, peak = _rms_peak_arr(arr)
            if rms < MIN_RMS:
                continue  # silencio/piso de ruido, no gastar en transcribir
            text = _quick_transcribe(verify_model, arr)
            if not text.strip():
                continue  # VAD no encontro habla en este tramo (musica, aplausos, pausa larga)
            chosen = (start, arr, rms, peak, text)
            used_minutes.add(minute)
            break
        if chosen is None:
            sys.exit(
                f"ABORTADO: no se encontro un clip de {dur}s con habla real verificable "
                f"(RMS>{MIN_RMS} y transcripcion no vacia) en ninguno de los "
                f"{len(CANDIDATE_STARTS_S)} candidatos probados. No se puede medir sin audio "
                "real: un numero sobre silencio se lee igual de bien que uno bueno, y no lo es."
            )
        start, arr, rms, peak, text = chosen
        path = os.path.join(workdir, f"clip_{dur:02d}s.wav")
        _write_wav(path, arr)
        clips[dur] = path
        print(
            f"Clip {dur:>2}s: minuto {start // 60} (t={start}s), rms={rms:.4f} peak={peak:.4f}, "
            f"verificado con habla real ({len(text)} chars): {text[:80]!r}..."
        )
    return clips


# ---------------------------------------------------------------------
# GPU: VRAM y ocupacion por otros procesos
# ---------------------------------------------------------------------

def nvidia_smi_snapshot() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        n_apps = len(apps.splitlines()) if apps else 0
        return f"{out} | {n_apps} proceso(s) usando GPU"
    except Exception as e:  # noqa: BLE001 - diagnostico best-effort, nunca aborta el bench
        return f"[nvidia-smi no disponible: {e}]"


# ---------------------------------------------------------------------
# Carga de modelo + inferencia cronometrada
# ---------------------------------------------------------------------

def load_model(model_size: str, models_dir: str, cfg: dict):
    from faster_whisper import WhisperModel  # import diferido, igual que produccion

    t0 = time.perf_counter()
    model = WhisperModel(
        model_size,
        download_root=models_dir,
        local_files_only=True,
        **cfg,
    )
    load_s = time.perf_counter() - t0
    return model, load_s


def transcribe_timed(model, wav_path: str) -> tuple[str, float]:
    with open(wav_path, "rb") as f:
        pcm_bytes = f.read()
    buf = io.BytesIO(pcm_bytes)
    buf.seek(0)
    t0 = time.perf_counter()
    segments, _info = model.transcribe(
        buf, language="es", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    text = " ".join(seg.text for seg in segments).strip()
    dt = time.perf_counter() - t0
    return text, dt


# ---------------------------------------------------------------------
# Comparacion de texto (ahora sobre habla real: coincidencia exacta o diff)
# ---------------------------------------------------------------------

def text_agreement(a: str, b: str) -> dict:
    if a == b:
        return {"identico": True, "ratio": 1.0, "chars_distintos": 0}
    sm = SequenceMatcher(None, a, b)
    ratio = sm.ratio()
    chars_distintos = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )
    return {"identico": False, "ratio": round(ratio, 4), "chars_distintos": chars_distintos}


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="small", choices=["small", "medium"])
    ap.add_argument("--video", default=DEFAULT_VIDEO, help="Video/audio con habla real en espanol")
    args = ap.parse_args()

    models_dir = _get_models_dir()
    if not _is_model_downloaded(args.model):
        print(f"ERROR: el modelo '{args.model}' no esta descargado en {models_dir}")
        print("Este script NO descarga modelos (decision explicita: solo la unidad que decide bajar")
        print("'medium' lo hace, y solo si CUDA gana claramente con 'small').")
        return 1

    print(f"=== Banco local_backend, modelo '{args.model}' ===")
    print(f"cpu_threads de produccion (os.cpu_count()={os.cpu_count()}): {CPU_THREADS}")
    print(f"Estado GPU antes de arrancar: {nvidia_smi_snapshot()}")
    print()

    workdir = tempfile.mkdtemp(prefix="vflow_bench_")
    print(f"Directorio temporal de clips (no commiteado): {workdir}")
    print(f"Fuente de audio real: {args.video}")

    # Modelo liviano SOLO para el guardian de verificacion (CPU, se descarta despues).
    from faster_whisper import WhisperModel  # noqa: PLC0415
    verify_model = WhisperModel(
        args.model, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS,
        download_root=models_dir, local_files_only=True,
    )
    clips = build_clips(args.video, workdir, verify_model)
    del verify_model
    print()

    results = {}  # config_name -> {"load_s":.., "warmup_s":.., "per_clip": {dur: {...}}, "error":...}
    texts = {}    # config_name -> {dur: text}

    for name, cfg in CONFIGS:
        print(f"--- Config: {name} ({cfg}) ---")
        entry = {"error": None, "per_clip": {}}
        try:
            model, load_s = load_model(args.model, models_dir, cfg)
        except Exception as e:  # noqa: BLE001 - se reporta y se sigue con las demas
            print(f"  FALLO al cargar: {type(e).__name__}: {e}")
            entry["error"] = f"{type(e).__name__}: {e}"
            results[name] = entry
            print()
            continue

        entry["load_s"] = round(load_s, 4)
        print(f"  Carga en frio: {load_s:.3f}s")

        # Primera inferencia de la config = paga el lazy-alloc de CTranslate2.
        # Se descarta del calculo de mediana; se reporta aparte.
        warm_dur = CLIP_DURATIONS_S[0]
        try:
            _warm_text, warm_s = transcribe_timed(model, clips[warm_dur])
        except Exception as e:  # noqa: BLE001
            print(f"  FALLO en warmup/primera inferencia: {type(e).__name__}: {e}")
            entry["error"] = f"warmup: {type(e).__name__}: {e}"
            results[name] = entry
            print()
            continue
        entry["warmup_s"] = round(warm_s, 4)
        entry["warmup_clip_s"] = warm_dur
        print(f"  Primera inferencia (lazy-alloc, clip {warm_dur}s, descartada de la mediana): {warm_s:.3f}s")

        vram_after_load = nvidia_smi_snapshot()

        texts[name] = {}
        for dur in CLIP_DURATIONS_S:
            path = clips[dur]
            timings = []
            text_out = None
            fail = None
            for rep in range(N_REPS):
                try:
                    text_out, dt = transcribe_timed(model, path)
                    timings.append(dt)
                except Exception as e:  # noqa: BLE001
                    fail = f"{type(e).__name__}: {e}"
                    break
            if fail:
                print(f"  clip {dur:>2}s: FALLO rep {len(timings)+1}/{N_REPS}: {fail}")
                entry["per_clip"][dur] = {"error": fail}
                continue
            median_s = statistics.median(timings)
            entry["per_clip"][dur] = {
                "median_s": round(median_s, 4),
                "min_s": round(min(timings), 4),
                "max_s": round(max(timings), 4),
                "rtf": round(dur / median_s, 2) if median_s > 0 else None,
                "n_reps": N_REPS,
            }
            texts[name][dur] = text_out
            n_chars = len(text_out or "")
            vacio = "  <-- texto VACIO (inesperado: el clip paso el guardian de verificacion)" if n_chars == 0 else ""
            print(f"  clip {dur:>2}s: mediana={median_s:.3f}s min={min(timings):.3f}s "
                  f"max={max(timings):.3f}s  (tiempo_real x{dur/median_s:.1f})  chars={n_chars}{vacio}")

        if "cuda" in name:
            entry["vram_snapshot_tras_carga"] = vram_after_load
        results[name] = entry
        print()

    # ------------------------------------------------------------------
    # Coincidencia de texto: CPU-int8 (baseline) vs cada config CUDA
    # ------------------------------------------------------------------
    print("=== Coincidencia de texto (CPU-int8 vs cada config CUDA, sobre habla real) ===")
    baseline = texts.get("cpu_int8", {})
    agreement = {}
    for name, _cfg in CONFIGS:
        if name == "cpu_int8" or name not in texts:
            continue
        agreement[name] = {}
        for dur in CLIP_DURATIONS_S:
            if dur not in baseline or dur not in texts[name]:
                continue
            agr = text_agreement(baseline[dur], texts[name][dur])
            agreement[name][dur] = agr
            flag = "IDENTICO" if agr["identico"] else f"ratio={agr['ratio']} chars_distintos={agr['chars_distintos']}"
            print(f"  {name:>20} vs cpu_int8, clip {dur:>2}s: {flag}")
    print()

    print(f"Estado GPU al terminar: {nvidia_smi_snapshot()}")
    print()

    # ------------------------------------------------------------------
    # Tabla resumen final (mediana de inferencia por config x clip)
    # ------------------------------------------------------------------
    print("=== Tabla resumen: mediana de inferencia (s) por config x clip ===")
    header = "config".ljust(20) + "".join(f"{f'{d}s':>10}" for d in CLIP_DURATIONS_S)
    print(header)
    for name, _cfg in CONFIGS:
        entry = results.get(name, {})
        if entry.get("error"):
            print(name.ljust(20) + "  ERROR: " + entry["error"][:60])
            continue
        row = name.ljust(20)
        for dur in CLIP_DURATIONS_S:
            pc = entry.get("per_clip", {}).get(dur, {})
            if "median_s" in pc:
                row += f"{pc['median_s']:>10.3f}"
            else:
                row += f"{'ERR':>10}"
        print(row)

    dump_path = os.path.join(workdir, "resultados.json")
    with open(dump_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "agreement": agreement, "texts": texts, "model": args.model,
                   "cpu_threads": CPU_THREADS}, f, indent=2, ensure_ascii=False)
    print(f"\nJSON crudo (temporal, para pegar en el reporte): {dump_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
