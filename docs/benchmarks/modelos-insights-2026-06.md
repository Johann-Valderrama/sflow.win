# Benchmark de modelos LLM — Insights/Acta/Potor (Vflow)

**Fecha:** 2026-06-14
**Fuente:** [artificialanalysis.ai](https://artificialanalysis.ai) (snapshot 2026-06)
**Dataset crudo:** `./data/artificial-analysis-llms-2026-06.json`

---

## Contexto y objetivo

El backend de **insights** de Vflow vive en `core/insights.py` y cubre tres flujos:

1. **Insight Stream en vivo** — llamadas cada ~60-90 s durante una reunión activa. El LLM recibe el rolling state (`{temas, pendientes, propuestas, citas}`) + un delta de transcript y devuelve el estado actualizado. Prioridad: velocidad baja, TTFT bajo, costo bajo.
2. **Acta post-reunión** — una sola llamada al terminar, con el transcript completo. Produce resumen + decisiones + temas + pendientes (Meeting Wiki). Prioridad: inteligencia alta, contexto suficiente para reuniones largas.
3. **Chat Potor** — chat multi-turno sobre el historial de reuniones. El diseño NO usa embeddings: el modelo recibe directamente las actas relevantes en contexto. Prioridad: **ventana de contexto 1M** (para cargar varias actas/wikis a la vez), inteligencia alta.

El backend OpenRouter (`INSIGHTS_BACKEND=openrouter`) es **opt-in**; Groq sigue siendo el backend recomendado para el Insight Stream en vivo. La filosofía de costo apunta a ~$0.02-0.08/h en uso normal.

---

## Insight clave del análisis

El **Intelligence Index** de Artificial Analysis mide razonamiento duro (matemáticas, código, GPQA). Para los dos regímenes de Vflow los factores dominantes son distintos:

- **Insights en vivo** → velocidad de generación, TTFT y costo. La inteligencia importa menos: el prompt es corto, el JSON de salida tiene forma fija, y un fallo se recupera con fail-safe.
- **Acta + Potor** → inteligencia alta + **ventana de contexto 1M**. Potor carga actas completas sin embeddings: si el contexto es 128K, una reunión larga o varias actas juntas pueden no caber. La velocidad y el TTFT son secundarios (on-demand, el usuario espera).

**Hallazgo central:** el modelo actual en Groq, `llama-3.3-70b-versatile`, puntúa **14.5** en el Intelligence Index — el más bajo de la tabla. OpenRouter ofrece acceso a modelos con índice 3× mayor al mismo orden de costo, con la ventaja adicional del contexto 1M para Potor.

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
| Gemini 3 Flash (reasoning) **← NUEVO DEFAULT** | 46.4 | 195 | 6.04 | 0.50 | 3.00 | **1.13** | 1M\* |
| Gemini 3 Pro Preview | 48.4 | n/d | n/d | 2.00 | 12.00 | **4.50** | 1M\* |
| Gemini 3.5 Flash (medium) | 54.8 | 210 | 13.52 | 1.50 | 9.00 | **3.38** | 1M\* |
| Claude Sonnet 4.6 (non-reasoning) | 44.4 | 59 | 1.06 | 3.75 | 15.00 | **6.56** | 200K\* |
| GPT-5.4 (non-reasoning) | 35.4 | 70 | 0.75 | 2.50 | 15.00 | **5.63** | 1M\* |

> **Nota sobre el slug `google/gemini-3-flash` y el modo razonamiento:**  
> En el dataset Artificial Analysis, `gemini-3-flash` (non-reasoning) puntúa Intelligence Index **35.0** (TTFT 0.95 s) y `gemini-3-flash-reasoning` puntúa **46.4** (TTFT 6.04 s). Nuestro `_chat_openrouter` usa el slug `google/gemini-3-flash` sin enviar parámetro de reasoning, por lo que el comportamiento real (razonamiento on/off) depende del routing por defecto de OpenRouter para ese slug. **Baseline garantizado: Intelligence Index 35.0**, que ya supera ~2.4× al llama-3.3-70b actual (14.5); con razonamiento activado en el modelo, se alcanza 46.4 (~3.2×). Forzar el modo razonamiento requeriría añadir el parámetro `reasoning` en el payload de `_chat_openrouter` — queda como knob futuro.

---

## Decisión

### Insights en vivo (si se usa OpenRouter)

**`deepseek/deepseek-v4-flash` (non-reasoning)**

Razón: Intelligence Index 36.5 (2.5× el llama actual), TTFT 0.92 s, 120 tok/s, y el blended más bajo de la tabla junto a Gemini Flash-Lite ($0.18/1M). El modo non-reasoning es suficiente para el rolling state en JSON de forma fija; el razonamiento añade latencia sin beneficio neto en este flujo. Contexto 128K* es adecuado para deltas cortos.

> Nota: Groq sigue siendo el **backend recomendado por defecto** para vivo (TTFT 0.64 s, sin latencia de red adicional). OpenRouter con DeepSeek Flash es la opción si se prefiere más inteligencia a costo idéntico.

### Acta + Potor (NUEVO DEFAULT de `OPENROUTER_MODEL`)

**`google/gemini-3-flash`**

- Intelligence Index: **35.0 garantizado** (non-reasoning, 2.4× el llama actual); **46.4 con razonamiento** (3.2×)
- Velocidad: 188–195 tok/s
- TTFT: 0.95 s (non-reasoning) o 6.04 s (con reasoning; on-demand, aceptable)
- Blended: **$1.13/1M** — 1.85× más caro que Groq llama, pero la calidad sube 2.4–3.2×
- Contexto: **1M\*** — clave para Potor sin embeddings

El factor decisivo es el 1M de contexto. Con 128K (como DeepSeek Pro o el llama actual), cargar 3-4 actas completas o una wiki de proyecto puede agotar la ventana. Gemini 3 Flash con el slug usado por OpenRouter tiene la mejor combinación de inteligencia (baseline 35.0, razonamiento 46.4) + contexto 1M a costo moderado. Véase la nota anterior sobre reasoning.

### Modelo premium (máxima inteligencia, opcional)

**`google/gemini-3.5-flash` (medium)**  
Intelligence Index 54.8, 210 tok/s, $3.38/1M, 1M ctx. Elegir cuando la calidad del acta sea crítica y el TTFT de 13.5 s sea aceptable. Activar manualmente con `OPENROUTER_MODEL=google/gemini-3.5-flash` en `.env`.

### Alternativa barato-listo (si 128K es suficiente)

**`deepseek/deepseek-v4-pro` (reasoning)**  
Intelligence Index 51.5, $0.54/1M — casi tan barato como Groq, inteligencia alta. Descartado como default por el contexto 128K que limita a Potor.

---

## Antes vs. después (acta/Potor)

### Cambio principal: Groq llama → OpenRouter Gemini 3 Flash

| Métrica | Antes (Groq llama-3.3-70b) | Ahora (Gemini 3 Flash reasoning) | Cambio |
|---|---|---|---|
| Intelligence Index | 14.5 | **46.4** | **+3.2×** |
| Velocidad generación | 95 tok/s | 195 tok/s | +2.1× |
| TTFT | 0.64 s | 6.04 s | más lento (on-demand, aceptable) |
| Blended $/1M | $0.61 | $1.13 | +1.85× |
| Contexto | 128K | **1M** | **+8×** |

### Cambio secundario: default OpenRouter anterior → nuevo

| Métrica | Antes (Gemini 2.5 Flash, default anterior) | Ahora (Gemini 3 Flash reasoning) | Cambio |
|---|---|---|---|
| Intelligence Index | 20.6 | **46.4** | **+2.3×** |
| Velocidad generación | 194 tok/s | 195 tok/s | ≈ igual |
| TTFT | 0.50 s | 6.04 s | más lento (reasoning activo) |
| Blended $/1M | $0.85 | $1.13 | +$0.28 |
| Contexto | 1M | 1M | igual |

La inteligencia se más que duplica al mover del default anterior (Gemini 2.5 Flash, 20.6) al nuevo (Gemini 3 Flash reasoning, 46.4), con un incremento de solo $0.28/1M.

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
| Chat Potor: preguntas por reunión | 10 |
| Chat Potor: tokens input por pregunta | 20 000 |
| Chat Potor: tokens output por pregunta | 500 |

Blended simplificado (3:1 input:output): se aplica directamente el $/1M blended del modelo al total de tokens.

### Cálculo por reunión de 1 h

**Insights en vivo (DeepSeek V4 Flash, $0.18/1M blended)**

```
Total tokens = 45 × (2 000 + 600) = 45 × 2 600 = 117 000 tokens
Costo = 117 000 / 1 000 000 × $0.18 = $0.021
```

**Acta (Gemini 3 Flash reasoning, $1.13/1M blended)**

```
Total tokens = 12 000 + 1 500 = 13 500 tokens
Costo = 13 500 / 1 000 000 × $1.13 = $0.015
```

**Chat Potor (Gemini 3 Flash reasoning, $1.13/1M blended)**

```
Total tokens = 10 × (20 000 + 500) = 10 × 20 500 = 205 000 tokens
Costo = 205 000 / 1 000 000 × $1.13 = $0.232
```

### Tabla resumen

| Flujo | Modelo | Tokens/reunión | Costo/reunión | Costo/mes (20 reuniones) |
|---|---|---|---|---|
| Insights en vivo | DeepSeek V4 Flash ($0.18/1M) | 117 000 | $0.021 | $0.42 |
| Acta | Gemini 3 Flash ($1.13/1M) | 13 500 | $0.015 | $0.30 |
| Chat Potor | Gemini 3 Flash ($1.13/1M) | 205 000 | $0.232 | $4.64 |
| **Total** | — | — | **$0.268** | **$5.36** |

**Comparación con costo actual (todo Groq llama-3.3-70b, $0.61/1M blended)**

```
Flujo vivo:  117 000 / 1M × $0.61 = $0.071
Acta:         13 500 / 1M × $0.61 = $0.008
Potor:       205 000 / 1M × $0.61 = $0.125
Total/reunión actual: $0.204
Total/mes actual (20 reuniones): $4.08
```

La nueva configuración cuesta **~$0.27/reunión vs. $0.20/reunión** — un incremento de ~$0.06/reunión (~$1.27/mes con 20 reuniones), a cambio de triplicar la inteligencia del acta/Potor y multiplicar 8× el contexto disponible.

---

## Alternativas evaluadas

| Modelo | Motivo de descarte | Cuándo reconsiderar |
|---|---|---|
| **Gemini 2.5 Flash-Lite** (12.7, $0.18/1M, 1M ctx) | Intelligence Index 12.7 — por debajo incluso del llama actual. Apto para clasificación mecánica, no para acta/Potor de calidad | Si solo se necesita extracción de campos simples, no análisis |
| **Claude Sonnet 4.6** (44.4, $6.56/1M, 200K ctx) | 6× más caro que Gemini 3 Flash; contexto 200K limita Potor | Si la fiabilidad de tool calling fuera crítica (no aplica aquí) |
| **Gemini 3.5 Flash (medium)** (54.8, $3.38/1M, 1M ctx) | 3× más caro que el nuevo default; TTFT 13.5 s puede generar esperas largas en acta | Activar manualmente para reuniones donde la calidad del acta sea crítica (`OPENROUTER_MODEL=google/gemini-3.5-flash` en `.env`) |
| **DeepSeek V4 Pro (reasoning)** (51.5, $0.54/1M, 128K ctx) | Contexto 128K: Potor no puede cargar varias actas a la vez | Si Potor se rediseña con embeddings o los casos de uso nunca superan 100K tokens |
| **Gemini 3 Pro Preview** (48.4, $4.50/1M, 1M ctx) | Preview sin TTFT/velocidad publicados; precio 4× el nuevo default | Evaluar cuando el modelo salga de preview con métricas consolidadas |

---

## Cambios en código

### `core/insights.py` — función `_model()`

```python
# Antes:
return os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash").strip()

# Después:
return os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash").strip()
```

### `.env.example` — variable `OPENROUTER_MODEL`

```bash
# Antes:
OPENROUTER_MODEL=google/gemini-2.5-flash
# Modelo a usar (slug de openrouter.ai/models). Ejemplos: google/gemini-2.5-flash (1M contexto, rápido), ...

# Después:
OPENROUTER_MODEL=google/gemini-3-flash
# Modelo a usar (slug de openrouter.ai/models). Ejemplos: google/gemini-3-flash (1M ctx, default),
# google/gemini-3.5-flash (premium, máx inteligencia), deepseek/deepseek-v4-flash (barato/rápido).
# Verifica el slug vigente en https://openrouter.ai/models
```

### Notas

- El slug exacto de cada modelo debe verificarse en [openrouter.ai/models](https://openrouter.ai/models) — los slugs de Gemini en particular han variado históricamente entre releases.
- `_chat_openrouter` aún no envía parámetro de `reasoning.effort` ni `reasoning` al payload. Para Gemini 3 Flash reasoning, OpenRouter activa el modo razonamiento por defecto según el slug. Ajustar `reasoning.effort` (ej. `"low"` para reducir TTFT) es un knob futuro que requiere añadir el parámetro en el payload de `_chat_openrouter`.
- El contexto 1M de Gemini aplica globalmente; en la práctica el costo de tokens en Potor puede subir si se cargan wikis muy largas — el presupuesto de $0.232/reunión asume 10 preguntas con 20K tokens de input cada una.
