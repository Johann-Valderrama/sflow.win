# Privacidad y datos de los backends LLM

**Fecha:** 2026-06-14 · **Estado:** referencia (verificada con fuentes oficiales)

Para decidir **a dónde mandar las transcripciones de reuniones** (especialmente con datos de cliente / escenario empresa-OPS). Resume quién **entrena** y quién **retiene** datos por API.

---

## Regla general

**Las APIs de PAGO por defecto NO entrenan con tus datos.** La trampa son las **apps de consumidor** (ChatGPT Free/Plus, Claude.ai), que SÍ entrenan por defecto salvo opt-out. La API es un contrato distinto.

---

## Por proveedor

| Proveedor (API de pago) | ¿Entrena por defecto? | Retención por defecto | ZDR (Zero Data Retention) |
|---|---|---|---|
| **Groq** | No (prohibido por contrato) | Sin retención de inferencia; ≤30 días solo para abuso | Activable libremente en la consola |
| **OpenAI API** | No (salvo opt-in) | 30 días (abuse monitoring) | Enterprise, con aprobación |
| **Anthropic API** | No (sin permiso expreso) | 7 días (desde sep-2025) | Enterprise, vía contrato |
| **Google Gemini API (pago)** | No (tier de pago) | 30 días (abuse) | Proyectos aprobados |
| **OpenRouter** | Él mismo no loguea por defecto | Solo metadata (billing) | Por proveedor del panel (ver abajo) |

**Notas:**
- **Groq** es el mejor en la nube para este caso: contrato anti-entrenamiento explícito + sin retención de inferencia por defecto. Fuentes: `console.groq.com/docs/your-data`, Services Agreement.
- **Anthropic**: retención más corta (7 días). Excepción reportada: algunos modelos nuevos pueden requerir 30 días y no ser ZDR-elegibles — verificar al elegir modelo.
- **Google**: la API **gratuita** (AI Studio) SÍ entrena por defecto; solo el tier de pago está exento.
- Consumidor (NO usar para datos sensibles): ChatGPT Free/Plus y Claude.ai cambiaron a entrenar por defecto.

---

## OpenRouter (el caso especial)

Es un **agregador/router**: OpenRouter en sí **no loguea** prompts por defecto (salvo que actives "Data Discount Logging" por el 1%), PERO **reenvía tu contenido al proveedor subyacente** que sirve el modelo, y por defecto **puede rutear a proveedores que entrenan** salvo que actives los controles.

**Settings de cuenta a activar (Settings → Privacy) para minimizar exposición:**
- ✅ Desactivar **"Paid endpoints that may train on request data"** (clave si usas modelos de pago).
- ✅ Desactivar **"Free endpoints that may train on request data"** (solo afecta a modelos `:free`).
- ✅ Activar **ZDR** por proveedor (Anthropic/OpenAI/Google/Non-frontier) si quieres retención cero.
- ✅ NO activar "Free endpoints that may publish prompts" ni el descuento del 1%.
- Por request: `"provider": {"zdr": true}`. Verificar proveedores ZDR en `GET /api/v1/endpoints/zdr`.

**Riesgo residual:** si OpenRouter tiene desactualizada la política de un endpoint, el filtro puede fallar en silencio.

**Fusion específicamente** (ver `benchmarks/fusion-ensemble-por-bandas.md`): reenvía a **varios proveedores en paralelo** → ZDR improbable que aplique consistente → asumir peor caso con datos de cliente.

---

## Ranking de privacidad (más → menos privado)

1. 🟢 **Local sin internet** (LM Studio / backend `endpoint` local) — nada sale del dispositivo.
2. 🟢 **Groq** — no entrena (contrato), sin retención de inferencia, ZDR libre.
3. 🟡 **API directa de proveedor** (OpenAI / Anthropic / Google de pago) — no entrenan, retención 7-30 días de abuso.
4. 🟠 **OpenRouter sin configurar** — puede rutear a proveedores que entrenan; con los settings de arriba se aproxima al nivel 3, pero con riesgo residual.

---

## Aplicado a Vflow

- **Análisis EN VIVO** (frecuente, lo más sensible): Groq o **local**. Nunca un agregador sin configurar.
- **Acta + Asistente** (on-demand): OpenRouter es aceptable **solo** con los settings configurados y para reuniones **no sensibles**. Recuerda: el backend es **por tarea** (`INSIGHTS_BACKEND_LIVE` / `INSIGHTS_BACKEND_BATCH`).
- **Reuniones con datos de cliente**: evitar OpenRouter (sobre todo Fusion) o exigir endpoint ZDR-verificado; preferir **local** o Groq. Encaja con la ambición empresa/OPS (datos de cliente no deben salir a terceros sin garantía).

Config real de Johann en OpenRouter (jun-2026): paid-train OFF ✅, free-train OFF ✅, ZDR toggles OFF, publish OFF ✅, descuento OFF ✅ → correcta para uso de pago (no entrenan).

---

## Fuentes

- [Groq — Your Data](https://console.groq.com/docs/your-data) · [Services Agreement](https://console.groq.com/docs/legal/services-agreement)
- [OpenAI — Your data in the platform](https://developers.openai.com/api/docs/guides/your-data)
- [Anthropic — API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)
- [Google Gemini — Zero data retention](https://ai.google.dev/gemini-api/docs/zdr)
- [OpenRouter — Data Collection](https://openrouter.ai/docs/guides/privacy/data-collection) · [ZDR](https://openrouter.ai/docs/guides/features/zdr)
