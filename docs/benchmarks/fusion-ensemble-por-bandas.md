# Estrategia de modelos: Fusion y ensemble por bandas de costo

**Fecha:** 2026-06-14
**Estado:** referencia / parqueado (no implementado). Para decidir cuando la validación en reuniones reales muestre si la calidad con modelos chicos es suficiente.

---

## Resumen ejecutivo

OpenRouter publicó un estudio ("Fusion beats Frontier") donde **un panel de varios modelos baratos + un paso de síntesis supera a un modelo frontier individual**. La enseñanza reutilizable no es "usar Fusion", sino el **principio**: la técnica «N baratos + juez > 1 frontier» **escala por bandas de costo**, porque el lift viene del **paso de síntesis (estructural)**, no de mezclar arquitecturas caras.

**Para Vflow hoy: NO vale la pena** (costo 4-5×, choca con la filosofía de centavos/reunión; flash-lite + router de razonamiento ya da casi-frontier). Se reconsidera solo si la validación muestra que el acta/asistente con modelos chicos se queda corta — y ahí se prueba primero **verificación cruzada barata**, no el panel de research.

---

## 1. OpenRouter Fusion (el ejemplo concreto)

**Qué es** (slug `openrouter/fusion`): NO es un modelo ni un router que elige uno. Es un **pipeline ensemble + síntesis server-side**:
1. Despacha el prompt a un panel de N modelos en paralelo (cada uno con web search/fetch/bash).
2. Un "judge" produce un JSON estructurado: `{consenso, contradicciones, gaps de cobertura, insights únicos, puntos ciegos}`.
3. Un modelo "outer" redacta la respuesta final usando ese análisis.

Se invoca como un modelo normal, como tool (`openrouter:fusion`) o como plugin. Params: `analysis_models` (1-8), `model` (judge), `reasoning` (forwardeable), `max_tool_calls`. Presets: **Quality** (Opus+GPT+Gemini-Pro) y **Budget** (Gemini-3-Flash+Kimi-K2.6+DeepSeek-V4-Pro). Contexto ~128k.

**Resultados del estudio** (benchmark DRACO de Perplexity AI — 100 tareas de deep-research, rubrics validadas por 26 expertos; independiente de OpenRouter, pero **la corrida la hizo OpenRouter sobre su propio sistema, sin réplica independiente**):

| Configuración | DRACO score |
|---|---|
| Fable 5 + GPT-5.5 (fused) | **69.0%** |
| Opus 4.8 + GPT-5.5 + Gemini 3.1 Pro (fused) | 68.3% |
| Budget panel (Flash + Kimi + DeepSeek-V4-Pro, fused) | **64.7%** |
| Fable 5 (solo) | 65.3% |
| DeepSeek V4 Pro (solo) | 60.3% |
| GPT-5.5 (solo) | 60.0% |
| Opus 4.8 (solo) | 58.8% |

**Hallazgo clave:** Opus×Opus fusionado consigo mismo (65.5%) **supera** a Opus solo (58.8%), +6.7 → **el paso de síntesis aporta valor por sí mismo**, no solo combinar arquitecturas distintas.

**Costo/latencia:** Fusion no tiene precio propio; el costo real = suma de todas las llamadas (panel + judge) ≈ **4-5× una completion sola**; latencia **2-3× mayor**. El "Budget a la mitad del costo" es mitad del panel Quality, no de un modelo individual.

**Caveat de marketing:** salvo los scores de DRACO, casi todo son claims del vendedor sin réplica independiente. El pipeline tiene además una ventaja sistémica (varias web-searches en paralelo) que infla la comparación en tareas de research.

---

## 2. El principio generalizado: ensemble por bandas

El estudio es **un solo ejemplo** (gama alta barata fusionada vs frontier alta). La enseñanza reutilizable:

> La técnica «**N modelos baratos + juez > 1 modelo frontier**» **escala por bandas de costo**:
> - alta-barata vs frontier-alta → ✅ **probado** por el estudio
> - media vs media-frontier → hipótesis
> - baja vs baja-frontier → hipótesis
>
> Es razonable porque **el lift es estructural** (viene del paso de síntesis — lo prueba Opus×Opus +6.7 — no de mezclar arquitecturas), así que no está atado al tope de la escala.

**El reframe de la decisión:** en vez de *"¿qué modelo individual uso?"*, preguntar:

> **"En mi banda de costo, ¿me conviene 1 frontier o un panel de N baratos de esa misma banda?"**

---

## 3. Condiciones para que aplique (lo que lo vuelve accionable)

1. **Piso de competencia.** La síntesis NO puede fabricar una corrección que ningún modelo del panel tuvo (garbage in / garbage out). Escala hacia abajo solo **hasta un umbral**; por debajo, panel + juez puede no superar a un único modelo decente. (El panel "budget" del estudio no es bajo: DeepSeek-V4-Pro tiene Intelligence Index ~51.)
2. **El juez debe discriminar bien.** Necesita reconocer lo bueno de lo malo del panel. Buena noticia: **juzgar suele ser más fácil que generar**, así que un juez barato decente es plausible — pero es lo primero a validar al bajar de banda.
3. **El lift depende de la TAREA.** Máximo en research/cobertura (cada modelo aporta conocimiento parcial distinto); **menor en extracción sobre contexto fijo** (p.ej. generar el **acta** de Vflow: todos los modelos ven el mismo transcript, no hay "cobertura" que combinar). Para extracción, el mecanismo útil es la **verificación cruzada** (B caza errores/omisiones de A), no el panel de research.
4. **Comparación justa = cost-equalized.** No es "N baratos vs 1 frontier" a secas, sino: *"por costo X en mi banda, ¿gana N-baratos+juez al **mejor modelo individual** que compro con X?"*. Es **testeable barato por tarea** con una tanda de evals sobre las mismas entradas.

---

## 4. Implicaciones para Vflow

- **Integrar Fusion sería trivial** (solo `OPENROUTER_MODEL=openrouter/fusion`, ya hablamos OpenRouter). Pero:
  - **Batch (acta + Asistente):** encaje técnico sí, pero costo 4-5× choca con centavos/reunión, y las web-tools del panel son **ruido** para nuestro contexto interno (no necesitamos búsqueda web). El router de razonamiento actual ya da casi-frontier por mucho menos.
  - **Vivo:** NO (latencia 2-3× lo descarta).
- **Fusión casera, cuando haga falta** (orden recomendado por costo/complejidad):
  - **(B) Verificación cruzada** — modelo A genera acta → modelo B lista gaps/inconsistencias → A re-pasa. Barata, auditable, **ataca el mecanismo correcto para extracción**. Primer experimento si el acta se queda corta.
  - **(A) Ensemble serial** — 2 modelos baratos + juez que sintetiza (~3× costo de modelos baratos).
  - **(C) Fusion con panel barato custom** — `analysis_models` con modelos baratos; cero código propio pero opaco y con web-tools de ruido.

**Regla práctica:** la decisión se valida con una tanda de evals sobre las mismas entradas (acta sobre el mismo transcript), no en teoría. Hoy parqueado; reconsiderar tras validar en reuniones reales.

---

## Fuentes

- [OpenRouter Blog — Surpassing Frontier Performance with Fusion](https://openrouter.ai/blog/announcements/fusion-beats-frontier/)
- [OpenRouter Docs — Fusion Router](https://openrouter.ai/docs/guides/routing/routers/fusion-router)
- [OpenRouter — Fusion model page](https://openrouter.ai/openrouter/fusion)
- [DRACO dataset (Perplexity AI, HuggingFace)](https://huggingface.co/datasets/perplexity-ai/draco)
- [DRACO paper (arXiv 2602.11685)](https://arxiv.org/pdf/2602.11685v1)

Decisiones de modelo/costo de Vflow (backend por tarea, flash-lite, router de razonamiento): ver [`modelos-insights-2026-06.md`](./modelos-insights-2026-06.md).
