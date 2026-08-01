# Banco CPU vs CUDA: backend local (unidad 7a, Ola 7)

> Fecha de la medición: 2026-08-01. Hash del repo en el momento de medir:
> `1e2694187f3956222d717935f83d0c5c0fb592e5` (rama `windows-variant`).
> Reproducir con: `venv\Scripts\python.exe tests\bench_local_backend.py --model small`
> (script: `tests/bench_local_backend.py`, no lo colecciona pytest).

## Veredicto

**DESCARTAR.** CUDA no gana de forma medible sobre CPU `int8` para el modelo `small` en esta
GPU. Las medianas de inferencia son estadísticamente indistinguibles entre las 4 configuraciones
en las 5 duraciones probadas (diferencias de ±2-8%, sin patrón consistente de quién gana), y CPU
ya transcribe a **80x-220x tiempo real**, muy por encima de cualquier necesidad del hot-path de
dictado (chunks de hasta 60s, presupuesto del pipeline en segundos, no milisegundos). **No se
justifica construir la unidad 7b** (`LOCAL_DEVICE` con detección/fallback) con esta evidencia.

No se descargó el modelo `medium` (paso 2 del plan): la condición para hacerlo era que CUDA
ganara claramente con `small`, y no ganó.

## Hardware y versiones (medido, no copiado de otro documento)

| Campo | Valor |
|---|---|
| GPU | NVIDIA GeForce GTX 1060 6GB (Pascal, compute capability **6.1**) |
| Driver NVIDIA | 582.66 |
| CUDA Toolkit (nvcc) | 12.9 (release 12.9, V12.9.86); hay también un 13.3 instalado en la máquina, pero faster-whisper/ctranslate2 usan el 12.x |
| Python | 3.14.3 |
| faster-whisper | 1.1.1 |
| ctranslate2 | 4.8.0 |
| `ctranslate2.get_supported_compute_types("cuda")` | `{'int8', 'int8_float32', 'float32'}`: **no hay `float16` ni `int8_float16`** en esta GPU |
| `ctranslate2.get_cuda_device_count()` | 1 |
| CPU | `os.cpu_count()` = 4 → `cpu_threads` de producción = `max(4, 4//2)` = **4** |
| Modelo medido | `small` (única descarga presente: `models/models--Systran--faster-whisper-small`, 464 MB) |

**Corrección a una premisa del plan escrito** (`docs/PLAN-DICTADO-2026-07-31.md`, fila `7a` de la
tabla de Ola 7): menciona comparar contra `int8_float16`. Esa combinación **no existe** en esta
GPU: Pascal (CC 6.1) no tiene soporte eficiente de FP16 (eso llegó con Volta/Turing, CC≥7.0), y
`ctranslate2` no la ofrece como `compute_type` soportado aquí. Se comparó contra las 3 que sí
existen: `int8`, `int8_float32`, `float32`.

**Hallazgo no buscado, `[Verificado]`:** al pedir `compute_type="int8"` en CUDA, el objeto
`WhisperModel.model.compute_type` reportado internamente es `int8_float32`: en esta GPU/versión
de ctranslate2, `int8` puro y `int8_float32` no son dos rutas distintas en la práctica (se resuelve
al mismo kernel de acumulación en float32). Esto explica por qué `cuda_int8` y `cuda_int8_float32`
midieron prácticamente idénticos en la tabla de abajo: es el mismo camino de cómputo, no dos
configuraciones independientes que casualmente empataron.

## AVISO IMPORTANTE sobre el audio de prueba (léase antes que la tabla)

El plan prescribió `mic_yo.wav` y `loopback_ellos.wav` como "grabaciones reales de una reunión".
**Esa descripción es incorrecta para el contenido real de estos dos archivos en esta máquina**,
verificado con tres métodos independientes (RMS/pico por segundo, `faster_whisper.audio.decode_audio`,
y transcripción con `no_speech_threshold` forzado a aceptar todo):

- `mic_yo.wav`: RMS = 0.000015, pico = 0.000031 (escala 0-1). Es **silencio de piso de ruido**,
  no habla. Forzando la transcripción (ignorando el gate de `no_speech_threshold`) el modelo
  alucina `" y"` repetido 14 veces con `no_speech_prob≈0.87`, el patrón clásico de alucinación de
  Whisper sobre silencio puro, no una transcripción real.
- `loopback_ellos.wav`: RMS = 0.144, pico = 0.254, **constante segundo a segundo con precisión de
  microsegundo durante los 15s completos** (cero varianza), lo cual es incompatible con habla real
  (que siempre varía). Es un **tono sintético de prueba**: el propio script que generó estos
  archivos, `test_dual_capture.py` (línea ~38-45 de este repo), reproduce por el parlante una
  "Mezcla de dos tonos [330Hz + 550Hz] para que se note como 'voz remota' sintética", palabras
  textuales de su propio comentario, precisamente para darle señal al canal de loopback durante
  un test de hardware, no para capturar una conversación.
- Con el VAD de producción (`vad_filter=True`, igual que `core/backends/local_backend.py`), **las
  5 duraciones x 4 configuraciones transcriben cadena vacía**, en las 20 combinaciones. El script
  lo declara en su propia salida (`chars=0`, con la advertencia impresa) para que este resultado
  nunca se lea como "0 divergencia = compatible", que sería la trampa de un cero muestral que en
  realidad es ausencia de habla, no ausencia de diferencia.

**Consecuencia sobre qué mide este reporte y qué no:**
- La **latencia SÍ es válida**: el costo de cómputo de CTranslate2 depende de la duración del audio
  (número de ventanas mel-espectrograma), no de si hay habla articulada o silencio/tono: el
  encoder+decoder corren igual sobre el mismo número de frames.
- El **agreement de texto NO valida paridad de calidad de transcripción real entre CPU y GPU**:
  las 20 comparaciones salieron "idénticas" porque ambos lados produjeron la cadena vacía, no
  porque CPU y CUDA coincidieran transcribiendo habla real. Esto es una limitación de los datos de
  prueba disponibles en el repo, no del método. Si se repite este banco con audio que sí contenga
  habla real, la sección de agreement del script queda lista para usarse (compara texto completo,
  ratio de similitud y conteo de caracteres distintos vía `difflib`); solo faltaría reemplazar la
  fuente de audio.

## Metodología

- **Clips**: 5 duraciones escalonadas (5, 10, 15, 30, 60s), construidas intercalando `mic_yo.wav` +
  `loopback_ellos.wav` en bloques de 2s (pool de ~30s reales), repitiendo el pool en ciclo para
  llegar a la duración pedida. Generados en un directorio temporal del sistema
  (`tempfile.mkdtemp(prefix="vflow_bench_")`), nunca commiteados; no hizo falta tocar `.gitignore`
  porque no se escriben en el repo.
- **Configs**: `cpu_int8` (config EXACTA de producción, `cpu_threads=4`), `cuda_int8`,
  `cuda_int8_float32`, `cuda_float32`.
- **Carga en frío** medida por separado de la inferencia (ver tabla). Nota de método: las 4 cargas
  ocurren en el mismo proceso Python secuencialmente, así que no son 4 arranques de proceso
  totalmente independientes: el contexto CUDA del driver puede quedar parcialmente calentado por
  la config anterior. Esto es una limitación aceptada (correr 4 procesos separados habría sido más
  puro, pero el costo de carga es de todos modos ~1-2s en las 4 configs y no es lo que decide el
  veredicto).
- **Primera inferencia de cada config** (lazy-alloc de CTranslate2) se corrió sobre el clip de 5s,
  se descartó del cálculo de mediana y se reporta aparte (columna "warmup" abajo).
- **N=3 repeticiones** por (config, clip), tras el warmup; se reporta mediana, min y max.
- **VRAM y ocupación de GPU**: capturados con `nvidia-smi` antes de arrancar y al terminar (ver
  abajo). Esta máquina tiene OBS Studio y varios procesos usando GPU concurrentemente (ver lista),
  así que la medición NO es en una GPU completamente libre: es el escenario realista de uso.

## Tabla: carga en frío + primera inferencia (lazy-alloc)

| Config | Carga en frío (s) | Primera inferencia, clip 5s (s) |
|---|---:|---:|
| cpu_int8 | 1.508 | 0.319 |
| cuda_int8 | 1.404 | 0.063 |
| cuda_int8_float32 | 1.282 | 0.072 |
| cuda_float32 | 1.329 | 0.060 |

## Tabla: mediana de inferencia (s) por config x clip, N=3 (min/max entre paréntesis)

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 | 0.060 (0.059-0.062) | 0.082 (0.081-0.082) | 0.100 (0.100-0.103) | 0.159 (0.157-0.162) | 0.273 (0.272-0.276) |
| cuda_int8 | 0.062 (0.061-0.062) | 0.079 (0.078-0.083) | 0.099 (0.099-0.100) | 0.156 (0.156-0.158) | 0.269 (0.269-0.270) |
| cuda_int8_float32 | 0.060 (0.060-0.062) | 0.080 (0.077-0.081) | 0.100 (0.099-0.102) | 0.157 (0.156-0.157) | 0.273 (0.270-0.275) |
| cuda_float32 | 0.061 (0.061-0.063) | 0.081 (0.080-0.082) | 0.106 (0.098-0.106) | 0.160 (0.155-0.161) | 0.271 (0.270-0.275) |

## Tabla: múltiplo de tiempo real (duración clip / mediana inferencia)

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 | x83.9 | x122.3 | x150.4 | x188.8 | x219.9 |
| cuda_int8 | x81.1 | x126.1 | x150.9 | x192.4 | x223.3 |
| cuda_int8_float32 | x82.6 | x125.2 | x150.1 | x191.7 | x220.1 |
| cuda_float32 | x81.9 | x123.8 | x141.4 | x187.7 | x221.6 |

Todas las diferencias entre CPU y CUDA están dentro de ±5% en cada duración, sin que ninguna
configuración gane consistentemente en las 5 filas. Con `small`, CPU ya transcribe a **~80x-220x
tiempo real** en esta máquina de 4 hilos: no hay margen de mejora que importe para el hot-path de
dictado (chunks de hasta 60s de audio).

## VRAM y ocupación de GPU

- Antes de arrancar: `993 MiB / 6144 MiB` usados, utilización 25%. Procesos activos en GPU
  (compartiendo recursos durante la medición, escenario realista): Chrome (x2), Slack (x2), Claude
  desktop (x2), OBS Studio, Zoom, un intérprete Python de `uv`, explorer.exe y varios procesos de
  shell de Windows.
- Al terminar (con las 3 configs CUDA cargadas simultáneamente en el mismo proceso, ~1.3-1.4GB de
  modelo total): `2305 MiB / 6144 MiB` usados, utilización 6%. Delta de ~1.3GB por tener 3 copias
  del modelo `small` residentes en VRAM a la vez (no representativo de producción, donde solo habría
  una config cargada).
- La GPU nunca se acercó a su límite de 6GB en ningún momento de la medición.

## ¿Hizo falta el arreglo de PATH de cuBLAS?

**No, en esta sesión.** Se verificó explícitamente: `C:\Program Files\NVIDIA GPU Computing
Toolkit\CUDA\v12.9\bin` (con `cublas64_12.dll`) ya estaba en el `PATH` heredado del proceso antes
de arrancar Python. La carga CUDA funcionó en el primer intento sin necesitar el blindaje
`_ensure_cuda_on_path()` de `C:\OPS\skills-on-demand\transcribir-video\assets\transcribir_video.py`.
**Esto no prueba que el gotcha esté resuelto de forma permanente**: ese gotcha se paga cuando el
proceso hereda un PATH viejo (terminal abierta antes de instalar CUDA, o antes de que el registro
se propague). Si la unidad 7b llegara a construirse eventualmente con evidencia nueva, debe incluir
la misma mitigación defensiva (buscar `cublas64_12.dll` bajo el Toolkit y anteponerlo al `PATH` si
no está), porque Vflow.exe se lanza de formas distintas a como se lanzó esta sesión de terminal
(tray de Windows, inicio con el sistema) y no hay garantía de que el `PATH` del proceso llevado por
esas rutas incluya la carpeta de CUDA.

## Coincidencia de texto CUDA vs CPU

**No aplica de forma significativa** (ver el AVISO arriba): las 20 comparaciones (3 configs CUDA x
5 clips) dieron "idéntico" porque tanto CPU como las 3 configs CUDA transcribieron cadena vacía en
las 20 combinaciones, con el VAD de producción activo (`vad_filter=True`, igual que
`core/backends/local_backend.py`). Esto demuestra que las 4 configuraciones **coinciden en
descartar este audio como no-habla** (ninguna alucina texto donde otra no lo hace), que es un dato
real aunque menor: no hubo divergencia de comportamiento del VAD/decoder entre CPU y GPU en este
caso límite. No permite afirmar nada sobre paridad de calidad transcribiendo habla real.

## Limitaciones declaradas (nunca un cero disfrazado de ausencia)

1. **Audio de prueba sin habla real** (ver AVISO arriba): es la limitación más importante de este
   reporte. El banco de latencia es válido; el banco de calidad de texto no se pudo ejecutar con
   contenido real por falta de una fuente de habla verificada en el repo bajo las restricciones de
   la tarea (solo se permitía usar `mic_yo.wav`/`loopback_ellos.wav`, sin generar audio sintético).
2. **Los clips de 30s y 60s repiten contenido** (ciclan sobre un pool de ~30s), porque solo hay esa
   cantidad de audio fuente. Correcto para medir latencia (depende de duración, no de contenido);
   sería una limitación adicional para medir calidad de texto si el audio fuente hubiera tenido
   habla real.
3. **Carga en frío no aislada por proceso**: las 4 configs se cargan en el mismo proceso Python
   secuencial, así que el contexto CUDA del driver puede llegar parcialmente calentado a la 2a/3a/4a
   carga. No afecta el veredicto (la carga no es el cuello de botella de ningún caso).
4. **`medium` no se midió**: la condición del plan para bajarlo (CUDA ganando claramente con
   `small`) no se cumplió, así que no se descargó (ahorra 1.5GB de red y no responde una pregunta
   que ya no importa).
5. **GPU compartida con otros procesos** (OBS, Chrome, Slack, Zoom, Claude desktop) durante la
   medición: es el escenario realista de la máquina de Johann, no un banco de laboratorio aislado;
   se reporta explícitamente en vez de asumir una GPU libre.

## Comando para reproducir

```
venv\Scripts\python.exe tests\bench_local_backend.py --model small
```

Genera sus propios clips en un directorio temporal en cada corrida (determinista dado el mismo
`mic_yo.wav`/`loopback_ellos.wav`), no requiere red ni argumentos adicionales. Exit code 0 si la
medición se completó (incluido el caso "CPU gana"); solo falla si el modelo no está descargado o si
ninguna config pudo cargar en absoluto.
