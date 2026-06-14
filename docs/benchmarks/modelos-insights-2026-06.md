# Benchmark de modelos LLM — Insights/Acta/Asistente de reuniones (Vflow)

**Fecha:** 2026-06-14
**Fuente:** [artificialanalysis.ai](https://artificialanalysis.ai) (snapshot 2026-06)
**Dataset crudo:** `./data/artificial-analysis-llms-2026-06.json`

---

## Contexto y objetivo

El backend de **insights** de Vflow vive en `core/insights.py` y cubre tres flujos:

1. **Insight Stream en vivo** — llamadas cada ~60-90 s durante una reunión activa. El LLM recibe el rolling state (`{temas, pendientes, propuestas, citas}`) + un delta de transcript y devuelve el estado actualizado. Prioridad: velocidad baja, TTFT bajo, costo bajo.
2. **Acta post-reunión** — una sola llamada al terminar, con el transcript completo. Produce resumen + decisiones + temas + pendientes (Meeting Wiki). Prioridad: inteligencia alta, contexto suficiente para reuniones largas.
3. **Chat Asistente de reuniones** — chat multi-turno sobre el historial de reuniones. El diseño NO usa embeddings: el modelo recibe directamente las actas relevantes en contexto. Prioridad: **ventana de contexto 1M** (para cargar varias actas/wikis a la vez), inteligencia alta.

El backend OpenRouter (`INSIGHTS_BACKEND=openrouter`) es **opt-in**; Groq sigue siendo el backend recomendado para el Insight Stream en vivo. La filosofía de costo apunta a ~$0.02-0.08/h en uso normal.

---

## Insight clave del análisis

El **Intelligence Index** de Artificial Analysis mide razonamiento duro (matemáticas, código, GPQA). Para los dos regímenes de Vflow los factores dominantes son distintos:

- **Insights en vivo** → velocidad de generación, TTFT y costo. La inteligencia importa menos: el prompt es corto, el JSON de salida tiene forma fija, y un fallo se recupera con fail-safe.
- **Acta + Asistente de reuniones** → inteligencia alta + **ventana de contexto 1M**. El Asistente carga actas completas sin embeddings: si el contexto es 128K, una reunión larga o varias actas juntas pueden no caber. La velocidad y el TTFT son secundarios (on-demand, el usuario espera).

**Hallazgo central:** el modelo actual en Groq, `llama-3.3-70b-versatile`, puntúa **14.5** en el Intelligence Index — el más bajo de la tabla. OpenRouter ofrece acceso a modelos con índice 3× mayor al mismo orden de costo, con la ventaja adicional del contexto 1M para el Asistente de reuniones.

---

## Tabla comparativa

> Blended = mezcla 3:1 input:output, USD/1M tokens. Contexto marcado con \* = fuente docs oficiales del modelo.

| Modelo | Intelligence Index | Velocidad (tok/s) | TTFT (s) | Input $/1M | Output $/1M | **Blended $/1M** | Contexto |
|---|---|---|---|---|---|---|---|
| Llama 3.3 70B — Groq **(actual)** | 14.5 | 95 | 0.64 | 0.58 | 0.71 | **0.61** | 128K\* |
| DeepSeek V4 Flash (non-reasoning) | 36.5 | 120 | 0.92 | 0.14 | 0.28 | **0.18** | 128K\* |
| DeepSeek V4 Flash (reasoning) | 46.5 | 106 | 0.94 | 0.14 | 0.28 | **0.18** | 128K\* |
| DeepSeek V4 Pro (reasoning) | 51.5 | 61 | 1.18 | 0.44 | 0.87 | **0.54** | 128K\* |
| Gemini 2.5 Flash (non-reasoning) | 20.6 | 194 | 0.50 | 0.30 | 2.50 | **0.85** | 1M\* |
| Gemini 2.5 Flash-Lite (non-reasoning) | 12.7 | 271 | 0.32 | 0.10 | 0.40 | **0.18** | 1M\* |
| Gemini 3 Flash (non-reasoning) | 35.0 | 188 | 0.95 | 0.50 | 3.00 | **1.13** | 1M\* |
| Gemini 3 Flash (reasoning) | 46.4 | 195 | 6.04 | 0.50 | 3.00 | **1.13** | 1M\* |
| Gemini 3.1 Flash-Lite (non-reasoning) **← DEFAULT ACTUAL** | 33.5 | n/d | n/d | 0.10 | 0.40 | **0.18** | 1M\* |
| Gemini 3 Pro Preview | 48.4 | n/d | n/d | 2.00 | 12.00 | **4.50** | 1M\* |
| Gemini 3.5 Flash (medium) | 54.8 | 210 | 13.52 | 1.50 | 9.00 | **3.38** | 1M\* |
| Claude Sonnet 4.6 (non-reasoning) | 44.4 | 59 | 1.06 | 3.75 | 15.00 | **6.56** | 200K\* |
| GPT-5.4 (non-reasoning) | 35.4 | 70 | 0.75 | 2.50 | 15.00 | **5.63** | 1M\* |

> **Nota sobre reasoning por defecto (medición real 2026-06-14):**  
> En el dataset Artificial Analysis, `gemini-3-flash` (non-reasoning) puntúa Intelligence Index **35.0** y `gemini-3-flash-reasoning` puntúa **46.4**. Se verificó con llamadas reales que `google/gemini-3-flash-preview` **NO activa el razonamiento por defecto** cuando no se envía el parámetro `reasoning` — devuelve `reasoning_tokens=0` y trabaja en baseline 35.0. Lo mismo aplica a `google/gemini-3.1-flash-lite` (baseline 33.5). Ambos modelos soportan razonamiento on-demand con el parámetro `reasoning` en el payload, pero no lo hacen solos. Ver la sección "Medición real" más abajo para datos completos.  
> **Slug verificado vía la API de OpenRouter (GET /models, 2026-06-14):** `google/gemini-3-flash-preview`, `google/gemini-3.1-flash-lite`, `google/gemini-3.5-flash`, `google/gemini-2.5-flash`, `google/gemini-2.5-pro`. El bare `google/gemini-3-flash` devuelve HTTP 400 'is not a valid model ID'.

---

## Decisión

### Insights en vivo (si se usa OpenRouter)

**`deepseek/deepseek-v4-flash` (non-reasoning)**

Razón: Intelligence Index 36.5 (2.5× el llama actual), TTFT 0.92 s, 120 tok/s, y el blended más bajo de la tabla junto a Gemini Flash-Lite ($0.18/1M). El modo non-reasoning es suficiente para el rolling state en JSON de forma fija; el razonamiento añade latencia sin beneficio neto en este flujo. Contexto 128K* es adecuado para deltas cortos.

> Nota: Groq sigue siendo el **backend recomendado por defecto** para vivo (TTFT 0.64 s, sin latencia de red adicional). OpenRouter con DeepSeek Flash es la opción si se prefiere más inteligencia a costo idéntico.

### Acta + Asistente de reuniones (DEFAULT de `OPENROUTER_MODEL`)

**`google/gemini-3.1-flash-lite`**

- Intelligence Index: **33.5** (non-reasoning; equivalente en la práctica al 35.0 de gemini-3-flash, que tampoco razona por defecto — medición real 2026-06-14)
- Blended: **$0.18/1M** — ~6× más barato que gemini-3-flash-preview ($1.13/1M) con inteligencia idéntica en uso real
- Tiempo real por llamada: **2.6 s**, $0.0006/llamada (prompt ~813 tokens acta real)
- Contexto: **1M\*** — clave para el Asistente de reuniones sin embeddings
- Razonamiento on-demand: soportado (activar con parámetro `reasoning`; ver tabla de medición)

El factor decisivo es el 1M de contexto. Con 128K (como DeepSeek Pro o el llama actual), cargar 3-4 actas completas o una wiki de proyecto puede agotar la ventana. Tras la medición real (2026-06-14) se confirmó que gemini-3-flash-preview **no razona por defecto**, por lo que entregaba su baseline 35.0 al mismo costo que si razonara ($1.13/1M). Gemini 3.1 Flash-Lite ofrece inteligencia equivalente (33.5) a **mitad de costo**, más rápido, con el mismo contexto 1M, y además soporta escalar a razonamiento on-demand en el mismo modelo si una tarea lo requiere.

**Alternativa (sin ventaja real sin razonamiento):** `google/gemini-3-flash-preview` — Intelligence Index baseline 35.0 (non-reasoning, igual que la práctica), $1.13/1M (~6× más caro que flash-lite sin ganancia de inteligencia real). Considerar solo si se activa `reasoning` explícitamente para casos premium (II 46.4, $0.0062/llamada medido).

### Modelo premium (máxima inteligencia, opcional)

**`google/gemini-3.5-flash` (medium)**  
Intelligence Index 54.8, 210 tok/s, $3.38/1M, 1M ctx. Elegir cuando la calidad del acta sea crítica y el TTFT de 13.5 s sea aceptable. Activar manualmente con `OPENROUTER_MODEL=google/gemini-3.5-flash` en `.env`.

### Alternativa barato-listo (si 128K es suficiente)

**`deepseek/deepseek-v4-pro` (reasoning)**  
Intelligence Index 51.5, $0.54/1M — casi tan barato como Groq, inteligencia alta. Descartado como default por el contexto 128K que limita al Asistente de reuniones.

---

## Medición real de razonamiento y costo (2026-06-14)

Llamadas reales a OpenRouter con el prompt de capítulos de una reunión real (~813 tokens de prompt). Costo = tokens reales × precio publicado del modelo.

| Modelo | Reasoning por defecto | Tokens completion / reasoning | Tiempo | Costo real/llamada |
|---|---|---|---|---|
| gemini-3-flash-preview (normal) | No (`reasoning_tokens=0`) | 280 / 0 | 3.8 s | $0.0012 |
| gemini-3.1-flash-lite (normal) | No (`reasoning_tokens=0`) | 265 / 0 | 2.6 s | $0.0006 |
| gemini-3.5-flash | Sí (razona por defecto) | 1 670 / 1 501 | 9.2 s | $0.0162 |
| gemini-3.1-flash-lite + reasoning ON | (forzado) | 883 / 623 | 4.2 s | $0.0015 |
| gemini-3-flash-preview + reasoning ON | (forzado) | 1 940 / 1 638 | 11.9 s | $0.0062 |

### Conclusiones

- `gemini-3-flash-preview` y `gemini-3.1-flash-lite` **no razonan** salvo que se envíe el parámetro `reasoning` en el payload (OpenRouter); por defecto entregan su baseline sin pensar.
- **Flash-lite soporta razonamiento on-demand** y, al razonar, sigue siendo ~4× más barato que gemini-3-flash razonando ($0.0015 vs. $0.0062) → un router futuro puede escalar a razonamiento en el **mismo modelo flash-lite** cuando una tarea lo requiera, sin cambiar de modelo.
- `gemini-3.5-flash` es el único que razona por defecto (sin parámetro explícito), pero es caro ($0.0162/llamada) y lento (9.2 s) → reservado para "acta premium" manual (`OPENROUTER_MODEL=google/gemini-3.5-flash`).
- **Cambio de default confirmado:** usar `gemini-3-flash-preview` sin reasoning era pagar 2× por un baseline 35.0 cuando flash-lite entrega II 33.5 al mismo comportamiento real por la mitad del costo. La decisión de mover el default a `google/gemini-3.1-flash-lite` se basa en esta medición.

---

## Antes vs. después (acta/Asistente de reuniones)

### Cambio principal: Groq llama → OpenRouter Gemini 3 Flash

| Métrica | Antes (Groq llama-3.3-70b) | Ahora (Gemini 3 Flash reasoning) | Cambio |
|---|---|---|---|
| Intelligence Index | 14.5 | **46.4** | **+3.2×** |
| Velocidad generación | 95 tok/s | 195 tok/s | +2.1× |
| TTFT | 0.64 s | 6.04 s | más lento (on-demand, aceptable) |
| Blended $/1M | $0.61 | $1.13 | +1.85× |
| Contexto | 128K | **1M** | **+8×** |

### Cambio secundario: default OpenRouter anterior → nuevo (tras medición real)

| Métrica | Antes (Gemini 2.5 Flash, default anterior) | Intermedio (Gemini 3 Flash, baseline real) | **Ahora (Gemini 3.1 Flash-Lite)** | Cambio vs. anterior |
|---|---|---|---|---|
| Intelligence Index | 20.6 | 35.0 (non-reasoning real) | **33.5** | **+1.6×** |
| Velocidad generación | 194 tok/s | 188 tok/s | n/d | — |
| TTFT | 0.50 s | 0.95 s | 2.6 s/llamada (medido, ~813 tok) | — |
| Blended $/1M | $0.85 | $1.13 | **$0.18** | **−78%** |
| Costo real/llamada (~813 tok prompt) | — | $0.0012 | **$0.0006** | **−50%** |
| Contexto | 1M | 1M | **1M** | igual |

La medición real reveló que el paso "intermedio" (gemini-3-flash-preview) nunca razonaba por defecto: entregaba II 35.0 a $1.13/1M. Gemini 3.1 Flash-Lite da inteligencia equivalente (II 33.5) a $0.18/1M, ahorrando ~$0.95/1M frente al paso intermedio sin pérdida real de calidad. El paso de Gemini 2.5 Flash (20.6) a Flash-Lite (33.5) supone un +1.6× de inteligencia con un **−78% de costo**.

---

## Costo estimado

### Supuestos declarados

| Parámetro | Valor |
|---|---|
| Insights en vivo: llamadas/hora | 45 (cada ~80 s) |
| Insights en vivo: tokens input por llamada | 2 000 |
| Insights en vivo: tokens output por llamada | 600 |
| Acta post-reunión: llamadas | 1 por reunión |
| Acta: tokens input | 12 000 |
| Acta: tokens output | 1 500 |
| Chat Asistente de reuniones: preguntas por reunión | 10 |
| Chat Asistente de reuniones: tokens input por pregunta | 20 000 |
| Chat Asistente de reuniones: tokens output por pregunta | 500 |

Blended simplificado (3:1 input:output): se aplica directamente el $/1M blended del modelo al total de tokens.

### Cálculo por reunión de 1 h

**Insights en vivo (DeepSeek V4 Flash, $0.18/1M blended)**

```
Total tokens = 45 × (2 000 + 600) = 45 × 2 600 = 117 000 tokens
Costo = 117 000 / 1 000 000 × $0.18 = $0.021
```

**Acta (Gemini 3.1 Flash-Lite, $0.18/1M blended)**

```
Total tokens = 12 000 + 1 500 = 13 500 tokens
Costo = 13 500 / 1 000 000 × $0.18 = $0.002
```

**Chat Asistente de reuniones (Gemini 3.1 Flash-Lite, $0.18/1M blended)**

```
Total tokens = 10 × (20 000 + 500) = 10 × 20 500 = 205 000 tokens
Costo = 205 000 / 1 000 000 × $0.18 = $0.037
```

### Tabla resumen

| Flujo | Modelo | Tokens/reunión | Costo/reunión | Costo/mes (20 reuniones) |
|---|---|---|---|---|
| Insights en vivo | DeepSeek V4 Flash ($0.18/1M) | 117 000 | $0.021 | $0.42 |
| Acta | Gemini 3.1 Flash-Lite ($0.18/1M) | 13 500 | $0.002 | $0.05 |
| Chat Asistente de reuniones | Gemini 3.1 Flash-Lite ($0.18/1M) | 205 000 | $0.037 | $0.74 |
| **Total** | — | — | **$0.060** | **$1.21** |

**Comparación con costo actual (todo Groq llama-3.3-70b, $0.61/1M blended)**

```
Flujo vivo:  117 000 / 1M × $0.61 = $0.071
Acta:         13 500 / 1M × $0.61 = $0.008
Asistente:   205 000 / 1M × $0.61 = $0.125
Total/reunión actual: $0.204
Total/mes actual (20 reuniones): $4.08
```

La nueva configuración cuesta **~$0.06/reunión vs. $0.20/reunión** — un ahorro de ~70% (~$57/año con 20 reuniones/mes), con inteligencia 2.3× mayor (II 33.5 vs. 14.5) y contexto 8× más grande. La medición real confirma $0.0006/llamada para el flujo de acta (~813 tokens de prompt), muy por debajo de los supuestos de la tabla.

> **Nota:** el paso previo intermedio (gemini-3-flash-preview, $1.13/1M) habría costado $0.268/reunión — más del cuádruple que el default actual, sin ganancia real de inteligencia al no activar reasoning por defecto.

---

## Alternativas evaluadas

| Modelo | Motivo de descarte | Cuándo reconsiderar |
|---|---|---|
| **Gemini 2.5 Flash-Lite** (12.7, $0.18/1M, 1M ctx) | Intelligence Index 12.7 — por debajo incluso del llama actual. Apto para clasificación mecánica, no para acta/Asistente de reuniones de calidad | Si solo se necesita extracción de campos simples, no análisis |
| **Gemini 2.5 Flash-Lite** (12.7, $0.18/1M, 1M ctx) | Intelligence Index 12.7 — por debajo incluso del llama actual. Apto para clasificación mecánica, no para acta/Asistente de reuniones de calidad. (No confundir con Gemini **3.1** Flash-Lite, el default actual) | Si solo se necesita extracción de campos simples, no análisis |
| **Claude Sonnet 4.6** (44.4, $6.56/1M, 200K ctx) | 36× más caro que el default actual (flash-lite, $0.18/1M); contexto 200K limita al Asistente de reuniones | Si la fiabilidad de tool calling fuera crítica (no aplica aquí) |
| **Gemini 3.5 Flash (medium)** (54.8, $3.38/1M, 1M ctx) | 18× más caro que el default actual (flash-lite); TTFT 13.5 s puede generar esperas largas en acta | Activar manualmente para reuniones donde la calidad del acta sea crítica (`OPENROUTER_MODEL=google/gemini-3.5-flash` en `.env`) |
| **DeepSeek V4 Pro (reasoning)** (51.5, $0.54/1M, 128K ctx) | Contexto 128K: el Asistente de reuniones no puede cargar varias actas a la vez | Si el Asistente se rediseña con embeddings o los casos de uso nunca superan 100K tokens |
| **Gemini 3 Flash Preview** (35.0 baseline, $1.13/1M, 1M ctx) | No razona por defecto (medición real 2026-06-14); entrega II 35.0 a 6× el costo del default actual sin ventaja real | Si se activa reasoning explícito (II 46.4, $0.0062/llamada) para casos premium |
| **Gemini 3 Pro Preview** (48.4, $4.50/1M, 1M ctx) | Preview sin TTFT/velocidad publicados; precio 25× el default actual (flash-lite) | Evaluar cuando el modelo salga de preview con métricas consolidadas |

---

## Cambios en código

### `core/insights.py` — función `_model()`

```python
# Primera iteración (gemini-2.5-flash → gemini-3-flash-preview):
return os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash").strip()
# ↓
return os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview").strip()

# Default actual (tras medición real 2026-06-14 — flash-preview no razona por defecto):
return os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite").strip()
```

### `.env.example` — variable `OPENROUTER_MODEL`

```bash
# Default actual:
OPENROUTER_MODEL=google/gemini-3.1-flash-lite
# Modelo a usar (slug de openrouter.ai/models). Ejemplos:
#   google/gemini-3.1-flash-lite  — DEFAULT: 1M ctx, II 33.5, $0.18/1M, ~$0.0006/llamada (más barato)
#   google/gemini-3-flash-preview — alternativa: II 35.0 baseline (no razona por defecto), $1.13/1M (~6× más caro, sin ventaja real)
#   google/gemini-3.5-flash       — premium: II 54.8, razona por defecto, $3.38/1M
#   deepseek/deepseek-v4-flash    — alternativa barata para insights en vivo: II 36.5, $0.18/1M, 128K ctx
# Verifica el slug vigente en https://openrouter.ai/models
```

### Notas

- El slug exacto de cada modelo debe verificarse en [openrouter.ai/models](https://openrouter.ai/models) — los slugs de Gemini en particular han variado históricamente entre releases.
- `_chat_openrouter` **no envía parámetro `reasoning`** al payload. Medición real (2026-06-14) confirma que tanto `gemini-3-flash-preview` como `gemini-3.1-flash-lite` devuelven `reasoning_tokens=0` por defecto. Activar razonamiento requiere añadir `{"reasoning": {"effort": "high"}}` (u `"low"`/`"medium"`) al payload — knob futuro para modo "acta premium".
- El contexto 1M de Gemini aplica globalmente; en la práctica el costo de tokens en el Asistente de reuniones puede subir si se cargan wikis muy largas — el presupuesto de $0.037/reunión asume 10 preguntas con 20K tokens de input cada una.
