#!/usr/bin/env python
"""Banco de medicion CPU vs CUDA para el backend local (unidad 7a, Ola 7).

NO es un test de pytest (no lo colecciona la suite: no matchea `test_*.py`/
`*_test.py`). Se corre a mano:

    venv\\Scripts\\python.exe tests\\bench_local_backend.py [--model small|medium]

Responde UNA pregunta: ¿vale la pena tocar `core/backends/local_backend.py`
para usar la GPU (GTX 1060 6GB, CUDA 12.9) en vez de CPU? Mide, no opina.
Sale con exit 0 en cuanto la medicion se completo (aunque el veredicto sea
"CPU gana"); exit != 0 solo si no pudo medir nada.

Metodologia (ver docs/PLAN-DICTADO-2026-07-31.md, unidad 7a, y el reporte en
docs/benchmarks/local-backend-gpu-2026-08-01.md):

- Compara la config EXACTA de produccion (`cpu`/`int8`, mismo `cpu_threads`
  que `core/backends/local_backend.py:294`) contra las 3 configs CUDA que el
  ctranslate2 instalado soporta en esta GPU (`int8`, `int8_float32`,
  `float32`; NO existe `float16`/`int8_float16` en Pascal — CC 6.1 < 7.0).
- 5 clips de audio REAL (nunca sintetico), derivados de `mic_yo.wav` +
  `loopback_ellos.wav` (grabaciones reales de una reunion) intercalados en
  bloques de 2s y repetidos en ciclo hasta la duracion pedida. Generados en
  un directorio TEMPORAL, nunca commiteados.
- Carga en frio medida aparte de la inferencia. Primera inferencia de cada
  config (lazy-alloc de CTranslate2) se descarta del calculo de mediana y se
  reporta aparte, rotulada.
- N=3 repeticiones por (config, clip); se reporta mediana + min/max, nunca
  solo el promedio.
- Comparacion de TEXTO entre CPU-int8 y cada config CUDA: es AGREEMENT entre
  configuraciones (no hay ground truth humano), nunca "precision"/"exactitud".
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

SOURCE_FILES = ["mic_yo.wav", "loopback_ellos.wav"]
CLIP_DURATIONS_S = [5, 10, 15, 30, 60]
BLOCK_S = 2.0  # tamano del bloque de intercalado Yo/Ellos al construir el pool
SAMPLE_RATE = 16000
N_REPS = 3

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
# Construccion de clips reales por duracion escalonada
# ---------------------------------------------------------------------

def _read_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SAMPLE_RATE, f"{path}: sample rate inesperado {w.getframerate()}"
        assert w.getnchannels() == 1, f"{path}: se esperaba mono"
        assert w.getsampwidth() == 2, f"{path}: se esperaba PCM 16-bit"
        return w.readframes(w.getnframes())


def _build_pool(source_paths: list[str]) -> bytes:
    """Intercala los archivos fuente en bloques de BLOCK_S segundos.

    Da un pool de audio REAL (~30s con las 2 fuentes de 15s) que alterna
    Yo/Ellos, en vez de pegar primero todo un archivo y luego el otro.
    """
    frames_list = [_read_pcm(p) for p in source_paths]
    block_bytes = int(BLOCK_S * SAMPLE_RATE) * 2  # 2 bytes/sample
    pool = bytearray()
    offsets = [0] * len(frames_list)
    while True:
        added_any = False
        for i, frames in enumerate(frames_list):
            start = offsets[i]
            if start < len(frames):
                pool += frames[start:start + block_bytes]
                offsets[i] += block_bytes
                added_any = True
        if not added_any:
            break
    return bytes(pool)


def _write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def _rms_peak(pcm: bytes) -> tuple[float, float]:
    """RMS y pico normalizados a [0,1] (16-bit), para avisar si un archivo
    fuente es efectivamente silencio antes de gastar tiempo transcribiendo."""
    import array
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0, 0.0
    peak = max(abs(s) for s in samples) / 32768.0
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768.0
    return rms, peak


def build_clips(workdir: str) -> dict[int, str]:
    """Genera los 5 clips escalonados en `workdir` (temporal, no commiteado).

    Los clips >~30s REPITEN contenido del pool (ciclan), porque solo hay
    ~30s de audio fuente real disponible. Esto es correcto para medir
    LATENCIA (depende de la duracion, no del contenido); para AGREEMENT de
    texto es una limitacion declarada (ver el reporte): el mismo segmento
    aparece 2 veces dentro del clip de 60s.

    AVISO, no oculta un cero: antes de construir los clips, imprime RMS/pico
    de cada archivo fuente. Un RMS por debajo de ~0.001 es indistinguible de
    silencio de piso de ruido (ver el reporte: en esta maquina uno de los dos
    archivos prescritos resulto ser justamente eso).
    """
    src_paths = [os.path.join(REPO_ROOT, f) for f in SOURCE_FILES]
    for p in src_paths:
        if not os.path.isfile(p):
            sys.exit(f"ERROR: falta el audio fuente {p}")
        rms, peak = _rms_peak(_read_pcm(p))
        aviso = "  <-- RMS de silencio/piso de ruido (no hay habla audible aqui)" if rms < 0.001 else ""
        print(f"Fuente {os.path.basename(p)}: rms={rms:.6f} peak={peak:.6f}{aviso}")
    pool = _build_pool(src_paths)
    pool_s = len(pool) / 2 / SAMPLE_RATE
    print(f"Pool base construido: {pool_s:.2f}s reales (intercalado Yo/Ellos en bloques de {BLOCK_S}s)")

    clips = {}
    for dur in CLIP_DURATIONS_S:
        needed_bytes = int(dur * SAMPLE_RATE) * 2
        reps = (needed_bytes // len(pool)) + 1
        pcm = (pool * reps)[:needed_bytes]
        path = os.path.join(workdir, f"clip_{dur:02d}s.wav")
        _write_wav(path, pcm)
        clips[dur] = path
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
        return f"{out}" + (f" | procesos usando GPU: {apps}" if apps else " | sin otros procesos en GPU")
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
# Comparacion de texto (AGREEMENT, no "precision": no hay ground truth)
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
    args = ap.parse_args()

    models_dir = _get_models_dir()
    if not _is_model_downloaded(args.model):
        print(f"ERROR: el modelo '{args.model}' no esta descargado en {models_dir}")
        print("Este script NO descarga modelos (decision explicita: solo la unidad que decide bajar")
        print("'medium' lo hace, y solo si CUDA gana claramente con 'small').")
        return 1

    print(f"=== Banco local_backend — modelo '{args.model}' ===")
    print(f"cpu_threads de produccion (os.cpu_count()={os.cpu_count()}): {CPU_THREADS}")
    print(f"Estado GPU antes de arrancar: {nvidia_smi_snapshot()}")
    print()

    workdir = tempfile.mkdtemp(prefix="vflow_bench_")
    print(f"Directorio temporal de clips (no commiteado): {workdir}")
    clips = build_clips(workdir)
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
            vacio = "  <-- texto VACIO (ver aviso de RMS arriba; no asumir 0 divergencia)" if n_chars == 0 else ""
            print(f"  clip {dur:>2}s: mediana={median_s:.3f}s min={min(timings):.3f}s "
                  f"max={max(timings):.3f}s  (tiempo_real x{dur/median_s:.1f})  chars={n_chars}{vacio}")

        if "cuda" in name:
            entry["vram_snapshot_tras_carga"] = vram_after_load
        results[name] = entry
        print()

    # ------------------------------------------------------------------
    # Agreement de texto: CPU-int8 (baseline) vs cada config CUDA
    # ------------------------------------------------------------------
    print("=== Agreement de texto (CPU-int8 vs cada config CUDA; NO es precision, no hay ground truth) ===")
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
            if agr["identico"] and len(baseline[dur]) == 0:
                flag = "IDENTICO, pero AMBOS textos estan VACIOS (no valida agreement de habla real)"
            elif agr["identico"]:
                flag = "IDENTICO"
            else:
                flag = f"ratio={agr['ratio']} chars_distintos={agr['chars_distintos']}"
            print(f"  {name:>20} vs cpu_int8, clip {dur:>2}s: {flag}")

    total_baseline_chars = sum(len(t) for t in baseline.values())
    if total_baseline_chars == 0:
        print()
        print("AVISO IMPORTANTE: el baseline cpu_int8 transcribio VACIO en los 5 clips. Esto NO significa")
        print("que CPU y CUDA acuerden en habla real: significa que el audio fuente (mic_yo.wav +")
        print("loopback_ellos.wav) no contiene habla detectable por el VAD de produccion. Revisar RMS")
        print("impreso arriba antes de leer la seccion de agreement como evidencia de calidad de texto.")
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
        json.dump({"results": results, "agreement": agreement, "model": args.model,
                   "cpu_threads": CPU_THREADS}, f, indent=2, ensure_ascii=False)
    print(f"\nJSON crudo (temporal, para pegar en el reporte): {dump_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
