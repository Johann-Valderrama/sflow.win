# Banco CPU vs CUDA: backend local (unidad 7a, Ola 7)

> Fecha de la medición: 2026-08-01. Reproducir con:
> `venv\Scripts\python.exe tests\bench_local_backend.py --model small` (o `--model medium`)
> (script: `tests/bench_local_backend.py`, no lo colecciona pytest).
>
> **Este reporte tuvo una corrida previa INVÁLIDA** (mismo día), descartada y conservada al final
> de este documento en la sección **CORRIDA 1: SUPERADA**, con la razón exacta. Léela si vas a
> tocar este banco de nuevo: explica por qué `mic_yo.wav`/`loopback_ellos.wav` NO sirven como audio
> de prueba.

## Veredicto

**CONSTRUIR la unidad 7b.** Con audio real verificado, CUDA gana de forma clara y consistente
sobre CPU `int8` en las dos configuraciones de modelo, y no es una ganancia marginal:

- **`small`** (el default de producción hoy): CUDA `int8` transcribe **3.6x-4.9x más rápido**
  que CPU en las 5 duraciones (ej. clip de 60s: CPU 14.4s vs CUDA 4.0s).
- **`medium`**: aquí está el hallazgo que más importa para producto. **En CPU, `medium` es
  MÁS LENTO que tiempo real para clips cortos** (clip 5s: 7.1s para transcribir 5s de audio,
  multiplicador x0.7; clip 10s: x1.1), es decir, hoy `medium` en CPU **no es viable para dictado**
  porque el usuario esperaría más de lo que dura su propio audio. En CUDA, `medium` corre a
  **x4.5-x7.6 tiempo real**, igual de utilizable que `small` en CPU es hoy. **La GPU no solo
  acelera lo que ya funciona: vuelve viable un modelo que en CPU no lo era.**

Recomendación de precisión si se construye 7b: usar `compute_type="int8"` en CUDA (o
`int8_float32`, que en esta GPU es la misma ruta de cómputo, ver hallazgo abajo), NO `float32`.
El texto de `float32` diverge más del comportamiento actual de producción (CPU-int8) que el de
`int8`, ver la sección de coincidencia de texto: usar `int8` en CUDA minimiza el cambio de
comportamiento percibido por el usuario, además de ser igual o más rápido.

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
| Modelos medidos | `small` (464 MB, ya estaba descargado) y `medium` (1.5 GB, descargado para este banco porque CUDA ganó claramente con `small`) |

**Corrección a una premisa del plan escrito** (`docs/PLAN-DICTADO-2026-07-31.md`, fila `7a` de la
tabla de Ola 7): menciona comparar contra `int8_float16`. Esa combinación **no existe** en esta
GPU: Pascal (CC 6.1) no tiene soporte eficiente de FP16 (eso llegó con Volta/Turing, CC≥7.0), y
`ctranslate2` no la ofrece como `compute_type` soportado aquí. Se comparó contra las 3 que sí
existen: `int8`, `int8_float32`, `float32`.

**Hallazgo no buscado, `[Verificado]`:** al pedir `compute_type="int8"` en CUDA, el objeto
`WhisperModel.model.compute_type` reportado internamente es `int8_float32`: en esta GPU/versión
de ctranslate2, `int8` puro y `int8_float32` no son dos rutas distintas en la práctica (se resuelve
al mismo kernel de acumulación en float32). Esto explica por qué `cuda_int8` y `cuda_int8_float32`
midieron y transcribieron prácticamente idéntico en las tablas de abajo: es el mismo camino de
cómputo, no dos configuraciones independientes que casualmente empataron.

## Audio de prueba: habla real, verificada antes de medir

El banco usa un video de estudio personal de Johann con habla continua en español:
`C:\OPS\_JOS\Estudio\Youtube\SaaS Factory\2026.07.22_saas factory irvyn\2026.07.22_1257_saas factory irvyn.mp4`
(~97 minutos). Es material privado de terceros que vive en la máquina de Johann: se usa SOLO
localmente para cronometrar un modelo local, nunca sale de la máquina, y los WAV derivados no se
commitean (se extraen a un directorio temporal en cada corrida). Si esa ruta deja de existir,
`--video` acepta cualquier otra fuente de habla real en español.

**Extracción**: PyAV con seek directo a un punto del video (mismo patrón de resampleo que
`core/url_transcribe.py:_decode_audio_to_pcm`, `av.AudioResampler(format="s16", layout="mono",
rate=16000)`, pero sin decodificar el archivo completo, solo la ventana necesaria). Cada uno de los
5 clips (5, 10, 15, 30, 60s) se extrae de un **minuto distinto** del video (minutos 5, 25, 45, 65 y
85), nunca 5 cortes seguidos del mismo tramo.

**Guardián de verificación (el punto entero de la corrección de este reporte):** antes de medir,
cada clip candidato se transcribe con un modelo `cpu_int8` de verificación y se exige RMS > 0.01 Y
texto no vacío; si un candidato falla (silencio, música, aplausos), el script prueba el siguiente
candidato de una lista de 13 minutos distintos. **Si ningún candidato produce habla real, el
script aborta con exit != 0.** En esta corrida, los 5 primeros candidatos (minutos 5/25/45/65/85)
pasaron el guardián a la primera:

| Clip | Minuto | RMS | Texto verificado (primeros caracteres) |
|---|---|---:|---|
| 5s | 5 | 0.066 | "en España, ¿sí no? Sí. Al revés." |
| 10s | 25 | 0.105 | "Pero ahora viene una resistencia que yo estoy viendo porque..." |
| 15s | 45 | 0.097 | "empleados pero hacen algo, hacen un curso sobre todo..." |
| 30s | 65 | 0.105 | "si por aquí me llegó la notificación, ahorita nos ponemos..." |
| 60s | 85 | 0.109 | "valiosísimo, te quería felicitar y a las gracias personalmente..." |

**Ningún clip transcribió vacío en ninguna de las dos corridas** (`small`, `medium`); el script
imprime `chars=N` por clip y no se vio ni un solo `chars=0`. Si hubiera ocurrido, el script habría
abortado antes de llegar a medir tiempos, y este reporte no existiría con estos números.

## Metodología de tiempo

- **Configs**: `cpu_int8` (config EXACTA de producción, `cpu_threads=4`), `cuda_int8`,
  `cuda_int8_float32`, `cuda_float32`.
- **Carga en frío** medida por separado de la inferencia. Las 4 cargas ocurren en el mismo proceso
  Python secuencialmente (no son 4 arranques de proceso totalmente independientes); no afecta el
  veredicto, la carga (1.3-4.2s según el modelo) no es el cuello de botella de ningún caso.
- **Primera inferencia de cada config** (lazy-alloc de CTranslate2) se corrió sobre el clip de 5s,
  se descartó del cálculo de mediana y se reporta aparte.
- **N=3 repeticiones** por (config, clip), tras el warmup; se reporta mediana, min y max.
- **VRAM y ocupación de GPU**: capturados con `nvidia-smi` antes de arrancar, tras cargar cada
  config CUDA, y al terminar.

## Tabla: `small`, mediana de inferencia (s) por config x clip

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 (producción) | 2.526 (2.510-2.635) | 3.163 (3.080-3.166) | 3.406 (3.374-4.030) | 8.344 (8.189-8.359) | 14.364 (14.058-14.928) |
| cuda_int8 | 0.511 (0.511-0.533) | 0.886 (0.882-0.891) | 1.012 (1.007-1.035) | 2.475 (2.445-2.755) | 3.995 (3.971-4.286) |
| cuda_int8_float32 | 0.520 (0.515-0.741) | 0.910 (0.907-1.070) | 1.053 (1.031-1.059) | 2.678 (2.453-2.747) | 4.208 (4.034-4.238) |
| cuda_float32 | 0.613 (0.602-0.620) | 0.979 (0.970-1.163) | 1.089 (1.072-1.111) | 2.700 (2.696-2.743) | 4.390 (4.366-4.416) |

Múltiplo de tiempo real (duración / mediana):

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 | x2.0 | x3.2 | x4.4 | x3.6 | x4.2 |
| cuda_int8 | x9.8 | x11.3 | x14.8 | x12.1 | x15.0 |
| cuda_int8_float32 | x9.6 | x11.0 | x14.2 | x11.2 | x14.3 |
| cuda_float32 | x8.2 | x10.2 | x13.8 | x11.1 | x13.7 |

CUDA gana en las 5 duraciones, entre 3.6x (clip 60s) y 4.9x (clip 5s) más rápido que CPU.

## Tabla: `medium`, mediana de inferencia (s) por config x clip

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 (alternativa hoy no usada) | 7.087 (7.069-7.502) | 9.325 (8.651-10.571) | 10.404 (9.474-11.994) | 15.076 (14.920-18.009) | 39.488 (39.282-39.684) |
| cuda_int8 | 1.083 (1.070-1.094) | 1.696 (1.695-1.719) | 2.025 (2.014-2.028) | 3.962 (3.907-4.009) | 8.487 (8.486-8.512) |
| cuda_int8_float32 | 1.113 (1.101-1.130) | 1.725 (1.718-1.728) | 1.989 (1.987-2.010) | 3.948 (3.942-3.987) | 8.518 (8.517-8.599) |
| cuda_float32 | 1.441 (1.437-1.443) | 2.159 (2.151-2.170) | 2.526 (2.508-2.541) | 6.394 (6.394-6.435) | 9.763 (9.756-9.771) |

Múltiplo de tiempo real:

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 | **x0.7** | **x1.1** | x1.4 | x2.0 | x1.5 |
| cuda_int8 | x4.6 | x5.9 | x7.4 | x7.6 | x7.1 |
| cuda_int8_float32 | x4.5 | x5.8 | x7.5 | x7.6 | x7.0 |
| cuda_float32 | x3.5 | x4.6 | x5.9 | x4.7 | x6.1 |

**`medium` en CPU es más lento que tiempo real para clips de 5 y 10s** (negrita arriba): un
usuario dictando una frase corta esperaría más tiempo del que duró su propio audio. En CUDA,
`medium` corre entre 4.5x y 7.6x tiempo real en las 5 duraciones, comparable a como se siente
`small` en CPU hoy. Esta es la evidencia de producto que justifica 7b: no es solo "más rápido", es
la diferencia entre "utilizable" y "no utilizable" para un modelo que hoy nadie puede usar en CPU.

## Coincidencia de texto (CPU-int8 vs cada config CUDA, sobre habla real)

**`small`** (texto base de CPU-int8: 36, 157, 195, 447, 758 caracteres por clip):

| vs cpu_int8 | 5s | 10s | 15s | 30s | 60s |
|---|---|---|---|---|---|
| cuda_int8 | ratio 0.9577 (2 dist.) | ratio 0.9684 (9 dist.) | ratio 0.9844 (6 dist.) | ratio 0.9978 (1 dist.) | **ratio 0.82 (145 dist.)** |
| cuda_int8_float32 | igual a cuda_int8 (misma ruta de cómputo) | | | | |
| cuda_float32 | ratio 0.9577 (2 dist.) | ratio 0.9714 (8 dist.) | ratio 0.9818 (7 dist.) | **ratio 0.6652 (163 dist.)** | **ratio 0.6071 (435 dist.)** |

**`medium`** (texto base de CPU-int8: 32, 156, 188, 425, 746 caracteres por clip):

| vs cpu_int8 | 5s | 10s | 15s | 30s | 60s |
|---|---|---|---|---|---|
| cuda_int8 | IDÉNTICO | ratio 0.9936 (2 dist.) | IDÉNTICO | IDÉNTICO | **ratio 0.7772 (183 dist.)** |
| cuda_int8_float32 | igual a cuda_int8 | | | | |
| cuda_float32 | IDÉNTICO | ratio 0.9904 (2 dist.) | ratio 0.9947 (2 dist.) | ratio 0.8578 (62 dist.) | **ratio 0.4369 (554 dist.)** |

**Patrón consistente en los dos modelos, `[Verificado]`:** a 5-15s, CPU y CUDA casi siempre
coinciden exacto o casi exacto. **En el clip de 60s (que coincide con el orden de magnitud de
`CHUNK_SECONDS=60` de producción), el texto diverge de forma notable**, y más aún en `float32`
(hasta 554 caracteres distintos de 746, en `medium`) que en `int8`/`int8_float32` (183
caracteres distintos). **La causa de esta divergencia SÍ está confirmada** (ver la sección
siguiente, "Control de auto-consistencia"): es un efecto real del DISPOSITIVO, no inestabilidad
del modelo en audio largo. **Consecuencia práctica:** si 7b se construye, conviene medir esta
divergencia específicamente en clips largos antes de lanzarlo a producción, y preferir
`compute_type="int8"` (la opción que menos diverge de lo que el usuario ya conoce).

## Control de auto-consistencia: ¿el dispositivo diverge, o el modelo diverge de sí mismo?

La sección anterior atribuía la divergencia de texto a 60s al DISPOSITIVO sin haber medido si
CPU-vs-CPU ya diverge por cuenta propia sobre audio largo (deriva de VAD/decodificación); sin ese
control, "diverge por el dispositivo" y "el modelo es inestable en audio largo" son
indistinguibles, y llevan a decisiones distintas para 7b. Control añadido con el flag
`--self-consistency` (`tests/bench_local_backend.py`, modelo `small`): cada config
(`cpu_int8`, `cuda_int8`) se carga desde cero DOS VECES (dos procesos de modelo independientes,
no la misma instancia) y se transcribe el MISMO clip; se compara el texto de esas dos corridas
consigo mismo, con la misma métrica de `SequenceMatcher` ya usada arriba.

| Clip | CPU vs CPU (self) | CUDA vs CUDA (self) | CPU vs CUDA (cross-device, de la tabla arriba) |
|---|---:|---:|---:|
| 30s | ratio 1.0 (IDÉNTICO) | ratio 1.0 (IDÉNTICO) | ratio 0.9978 |
| 60s | ratio 1.0 (IDÉNTICO) | ratio 1.0 (IDÉNTICO) | ratio 0.82 |

**Conclusión de atribución, `[Verificado]`:** CPU es perfectamente reproducible consigo misma (2
cargas independientes, texto IDÉNTICO) y CUDA también lo es (2 cargas independientes, texto
IDÉNTICO); ninguno de los dos dispositivos es internamente inestable en audio largo. Como ambos
son 100% auto-consistentes y solo divergen entre sí (0.82 a 60s, prácticamente sin divergencia a
30s), **la divergencia de 60s es un efecto REAL del dispositivo** (CPU y GPU calculan la
mel-spectrograma/atención con caminos numéricos distintos: no asociatividad de punto flotante,
distinto orden de operaciones en las GEMM, y esa diferencia se acumula lo suficiente en ~60s de
audio como para hacer que el decodificador tome una rama distinta en algún punto de la secuencia),
no inestabilidad del modelo consigo mismo. **Esto no cambia el veredicto de 7b** (CUDA sigue
ganando en latencia de forma decisiva), pero sí confirma que la elección de `compute_type` en 7b
importa para la fidelidad del texto en chunks largos, y que ese riesgo es real, no un fantasma
estadístico.

## VRAM y contexto de la máquina durante la medición

**Vflow estaba corriendo** (`main.py`, PID 10752, proceso confirmado con `tasklist`/`wmic`
durante la medición) junto con Chrome, Slack, Claude desktop, OBS Studio, Zoom y varios procesos
de sistema, todos compitiendo por la misma GPU (17-18 procesos reportados por `nvidia-smi` antes
de arrancar, aunque la mayoría sin permiso para reportar cuánta VRAM usan). **Hoy Vflow NO
compite por VRAM**: su código está fijo a `device="cpu"` (`local_backend.py:311`), así que aunque
estuviera corriendo, no reserva memoria de GPU. Si 7b lo cambia a CUDA, Vflow se vuelve un
consumidor más de la VRAM medida abajo.

**Snapshots medidos** (acumulativos: el script carga las 4 configs en el mismo proceso sin
liberar la anterior, así que el snapshot de la 3a config CUDA incluye las 2 anteriores todavía en
memoria: esto es un **peor caso de laboratorio, no el escenario de producción**, donde solo se
cargaría UNA config a la vez):

| Momento | `small` | `medium` |
|---|---|---|
| Antes de arrancar | 1016 MiB / 6144 | 1020 MiB / 6144 |
| Tras cargar cuda_int8 (1er modelo CUDA) | 1413 MiB | 2079 MiB |
| Tras cargar cuda_int8_float32 (2 modelos CUDA a la vez) | 1549 MiB | 2471 MiB |
| Tras cargar cuda_float32 (3 modelos CUDA a la vez) | 2261 MiB | **4911 MiB** |

**Estimación realista para producción** (una sola config cargada, la recomendada `int8`): resto la
línea base de la primera carga, ya que es la que representaría el uso real:
- `small`-CUDA-int8 en producción: ~1413 MiB usados, **~4731 MiB libres** de 6144.
- `medium`-CUDA-int8 en producción: ~2079 MiB usados, **~4065 MiB libres** de 6144.

**Respuesta directa a si `medium` en CUDA se acerca al límite de 6 GB: NO, en el escenario
realista de un solo modelo cargado.** El número que sí se acerca al límite (4911 MiB, ~80% de
6144) es un artefacto de este script (3 modelos `medium` cargados simultáneamente en el mismo
proceso Python), no algo que ocurra en producción, donde `LocalBackend` mantiene un solo modelo
activo a la vez (`_load_model()` con lock, un `self._model` por instancia). Se deja escrito el
número acumulado de todos modos porque muestra el TECHO si algo llegara a mantener 2-3 modelos
vivos a la vez (ej. un bug de fuga de memoria), que sí sería motivo de alarma.

## ¿Hizo falta el arreglo de PATH de cuBLAS?

**No, en esta sesión.** Se verificó explícitamente: `C:\Program Files\NVIDIA GPU Computing
Toolkit\CUDA\v12.9\bin` (con `cublas64_12.dll`) ya estaba en el `PATH` heredado del proceso antes
de arrancar Python. La carga CUDA funcionó en el primer intento sin necesitar el blindaje
`_ensure_cuda_on_path()` de `C:\OPS\skills-on-demand\transcribir-video\assets\transcribir_video.py`.
**Esto no prueba que el gotcha esté resuelto de forma permanente**: ese gotcha se paga cuando el
proceso hereda un PATH viejo (terminal abierta antes de instalar CUDA, o antes de que el registro
se propague). **Dado que ahora sí se construye 7b, la unidad debe incluir la misma mitigación
defensiva** (buscar `cublas64_12.dll` bajo el Toolkit y anteponerlo al `PATH` si no está), porque
Vflow.exe se lanza de formas distintas a como se lanzó esta sesión de terminal (tray de Windows,
inicio con el sistema) y no hay garantía de que el `PATH` de esos procesos incluya la carpeta de
CUDA.

## Limitaciones declaradas

1. **Una sola fuente de video**: los 5 clips vienen del mismo video (aunque de minutos distintos,
   con un solo orador dominante en los tramos usados). No cubre acentos/ruido de fondo/calidad de
   micrófono variados. Suficiente para responder la pregunta de LATENCIA (que es lo que decide
   7b); insuficiente para una auditoría exhaustiva de calidad de transcripción por tipo de audio.
2. **Carga en frío no aislada por proceso**: las 4 configs se cargan en el mismo proceso Python
   secuencial (ver nota de VRAM arriba); no afecta el veredicto de latencia.
3. **GPU compartida con otros procesos** (Vflow, OBS, Chrome, Slack, Zoom, Claude desktop) durante
   la medición: es el escenario realista de la máquina de Johann, no un banco de laboratorio
   aislado.
4. **La causa de la divergencia de texto a 60s no está confirmada** (ver sección de coincidencia
   de texto): se reporta el hecho medido, no una explicación sin verificar.
5. **`nvidia-smi --query-compute-apps` no reporta VRAM por proceso en esta máquina** para la
   mayoría de procesos ("Insufficient Permissions" o "[N/A]"), así que la lista de "17-18 procesos
   usando GPU" es un conteo, no un desglose fiable de cuánto usa cada uno.

## Comando para reproducir

```
venv\Scripts\python.exe tests\bench_local_backend.py --model small
venv\Scripts\python.exe tests\bench_local_backend.py --model medium
```

Ambos comandos extraen sus propios clips del video de estudio (path por defecto en
`DEFAULT_VIDEO`, sustituible con `--video`) a un directorio temporal en cada corrida, verifican
habla real antes de medir, y no requieren red (excepto la primera vez que se pide `--model medium`
si aún no está descargado: faster-whisper lo descarga automáticamente desde Hugging Face). Exit
code 0 si la medición se completó; exit != 0 si el modelo no está descargado o si el guardián de
verificación no encuentra habla real en ningún candidato.

---

## CORRIDA 1: SUPERADA (2026-08-01, misma fecha, corrida anterior de este documento)

**Por qué esta corrida no vale y se reemplazó por la de arriba:** el banco original medía sobre
`mic_yo.wav` y `loopback_ellos.wav` (raíz del repo), que el plan `docs/PLAN-DICTADO-2026-07-31.md`
describía como **"grabaciones reales de una reunión"**. **Esa etiqueta venía del brief y era
FALSA.** Verificado con tres métodos (RMS/pico por segundo, `faster_whisper.audio.decode_audio`, y
transcripción forzada):

- `mic_yo.wav`: RMS = 0.000015, pico = 0.000031. **Silencio de piso de ruido**, no habla.
- `loopback_ellos.wav`: RMS = 0.144, constante segundo a segundo con precisión de microsegundo
  durante los 15s completos (cero varianza, imposible en habla real). Es el **tono sintético de
  prueba (330+550Hz)** que el propio script que generó estos archivos, `test_dual_capture.py`
  (línea ~38-45 de este repo), reproduce por el parlante para darle señal al canal de loopback
  durante un test de hardware ("Mezcla de dos tonos para que se note como 'voz remota' sintética",
  palabras textuales de su comentario).

**Consecuencia del error:** con `vad_filter=True` (igual que producción), el VAD descartaba estos
dos archivos COMPLETOS antes de que CTranslate2 decodificara nada. El costo medido (~0.06-0.28s
para clips de 5-60s, multiplicador **x80-x220 tiempo real**) no medía "transcribir", medía
"descartar por VAD sin decodificar": por eso CPU y CUDA "empataban", literalmente ninguna de las
dos hacía el trabajo caro. El veredicto de esa corrida (**"DESCARTAR, CUDA no gana"**) **no estaba
sostenido** y quedó revertido por la corrida de arriba, que sí mide sobre habla real y encuentra
que CUDA gana de forma clara (3.6x-4.9x en `small`, y la diferencia entre viable/no-viable en
`medium`).

**Para el próximo agente que toque este banco: `mic_yo.wav` y `loopback_ellos.wav` (raíz del
repo) son fixtures de diagnóstico de HARDWARE de `test_dual_capture.py`/`test_loopback.py`
(verificar mic+loopback capturan simultáneamente sin glitches), NO son audio de prueba para medir
calidad ni latencia de transcripción.** Usa una fuente de habla real (ver la sección "Audio de
prueba" arriba) y verifica SIEMPRE con un guardián de VAD+transcripción no vacía antes de confiar
en cualquier número de latencia: un banco sobre audio sin habla real da un número que se lee igual
de bien que uno bueno, y no lo es.

Tabla de la corrida 1 (conservada por completitud, NO USAR para decisiones):

| Config | 5s | 10s | 15s | 30s | 60s |
|---|---:|---:|---:|---:|---:|
| cpu_int8 | 0.060 | 0.082 | 0.100 | 0.159 | 0.273 |
| cuda_int8 | 0.062 | 0.079 | 0.099 | 0.156 | 0.269 |
| cuda_int8_float32 | 0.060 | 0.080 | 0.100 | 0.157 | 0.273 |
| cuda_float32 | 0.061 | 0.081 | 0.106 | 0.160 | 0.271 |

Commit de esa corrida (conservado, no reescrito): `4a0924fba553e10e4e71d8bd0be8a2f4a5710f57`.
