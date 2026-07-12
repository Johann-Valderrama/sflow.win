# PLAN MEJORAS Vflow — 2026-07-06 (derivado de AUDITORIA-FABLE-2026-07-06.md)

> Formato: skill `plan-por-olas-autonomo` (compone `orquestar-agentes` §0-§8).
> Fuente de los hallazgos: `AUDITORIA-FABLE-2026-07-06.md` (mismo directorio). Este plan solo
> incluye lo que vale la pena ejecutar; lo dudoso quedó como gate o como pregunta abierta del informe.
> Branch de trabajo: `windows-variant`. Política de integración: **(A) commits locales, sin push**
> (repo personal, patrón ya usado en las Olas 1-7). 1 unidad = 1 commit revisable.
>
---

## ALCANCE — este documento es la ÚNICA fuente de verdad; no hay que unir pedazos

Este plan consolida TODO lo pendiente de Vflow al 2026-07-06, para que no tengas que abrir
`PROGRESS.md` ni `PLAN-OLAS.md` ni `FASE3_SPEC.md`. Se reconcilió el inventario completo de planes
del repo contra el código real. Mapa único de qué existe y dónde quedó:

| Origen | Estado | Dónde vive ahora en ESTE plan |
|---|---|---|
| Auditoría Fable 2026-07-06 (bugs críticos) | Nuevo, sin ejecutar | **Ola 0** (corre solo) |
| Auditoría Fable 2026-07-06 (robustez, quick wins, escalabilidad, mantenibilidad) | Nuevo, sin ejecutar | **Olas 1, 2, 3** (corren solos) |
| Auditoría Fable 2026-07-06 (partir el monolito web) | Nuevo, sin ejecutar | **Ola 4** (🙋 decisión de alcance) |
| Auditoría Fable 2026-07-06 (features de producto) | Nuevo, opcional | **Ola 5** (🙋 eliges cuáles) |
| `docs/FASE3_SPEC.md` (2 features) | **Escrito, NUNCA ejecutado** | **Ola 6** (🙋 sí/no + re-validar el spec) |
| `docs/PLAN-OLAS.md` olas 1-7 | Ejecutado completo | Nada que hacer (historia) |
| `docs/PLAN-OLAS.md` recortes G4: **interrupciones v2** y **briefing v1.1** | Decididos como fuera-de-v1 | **Decisiones abiertas** (tabla abajo; 🙋 tú decides si los quieres) |
| `docs/PENDIENTES.md` | Backlog vivo (lo hecho tachado) | Lo que valía plan ya está en las olas; el resto sigue en PENDIENTES como backlog (nada se pierde) |
| `PRP.md` | COMPLETED | Historia |

**Decisiones abiertas heredadas** (no son ejecutables por defecto; requieren tu sí explícito, igual
que las olas 4-6). Las traigo aquí para que estén en el MISMO documento y no tengas que buscarlas:

| Decisión | Qué es | Costo si dices que sí | Cómo activarla |
|---|---|---|---|
| Interrupciones v2 (métricas) | Detectar solapes/interrupciones entre canales Yo/Ellos; hoy imposible sin un eje temporal común (el loopback se salta silencios) | Anotar el timestamp de pared de cada buffer del loopback + recalcular; unidad nueva media | Dilo al orquestador o escríbelo en PROGRESS.md; se planifica como ola nueva |
| Briefing OPS v1.1 (tarjetas "🧭 Contexto") | Inyectar el briefing OPS en el insight stream para tarjetas proactivas de contexto (hoy el briefing solo entra al chat pull) | Recalibrar las detecciones 5.1 (≥12 ventanas midiendo falsos + / −) como gate bloqueante | Dilo al orquestador; se planifica con la recalibración como parte de la unidad |

> Regla del documento: si algún día quieres algo que NO está aquí, primero míralo en `docs/PENDIENTES.md`
> (backlog vivo). Todo lo que valía la pena ejecutar YA está en las olas de abajo.

---

## 🚦 DECISIONES QUE JOHANN RESPONDE PRIMERO

> **Para el agente que abra este plan:** antes de ejecutar cualquier ola que dependa de una de
> estas decisiones, verifica si Johann ya las respondió (en PROGRESS.md sección PARA JOHANN o en
> el chat). Si una decisión que tu ola necesita está SIN responder, PARA y preséntasela con sus
> opciones tal cual están escritas aquí — no la interpretes por él. Las Olas 0-3 NO dependen de
> nada de esto: pueden correr sin esperar. Las Olas 4, 5 y 6 sí esperan las decisiones marcadas.
>
> **Para Johann:** responde con número + letra (ej. `D1: A · D2: B · D4: la 1 y la 3`). Si dudas,
> las opciones marcadas *(recomendada)* forman un plan sensato de una: `D1:A D2:A D3:A D5:ninguna
> D6:A D7:A D8:A` y en D4 eliges lo que te suene. Cada decisión se puede cambiar después.

**D1 — ¿Para quién es Vflow?** *(desbloquea la Ola 4)*
Define cuánto reordenamos el código por dentro.
- **A** *(recomendada)*: solo para mí, uso diario → solo limpieza mínima.
- **B**: quiero venderlo o compartirlo en 6-12 meses → reordenamiento completo (más trabajo, código a prueba de futuro).
- **C**: no sé aún → hacemos lo mínimo y lo decides más tarde.

**D2 — ¿Cuánto duran tus reuniones más largas?** *(ajusta la prioridad de la Ola 3)*
- **A** *(recomendada)*: normalmente 1-2 horas → el arreglo del panel es higiene, prioridad normal.
- **B**: a veces jornadas de 4-8 horas → subir prioridad, el panel actual se vuelve pesado.

**D3 — Tus reuniones guardadas, ¿para siempre o se borran solas?** *(desbloquea la Ola 3.2)*
- **A** *(recomendada)*: que yo elija cuántos meses guardar, apagado por defecto.
- **B**: para siempre, son mi memoria.
- **C**: bórralas solas pasados unos meses (privacidad y espacio).

**D4 — ¿Qué capacidades nuevas quieres?** *(desbloquea la Ola 5; elige varias o ninguna)*
- **1**: marcar solos los momentos importantes de la reunión, sin que pulses nada.
- **2**: que la IA sepa tu nombre y rol para actas más precisas.
- **3**: chatear sobre VARIAS reuniones a la vez y redactar emails/informes desde ellas.
- **4**: que los compromisos de la reunión salgan como tareas para tu sistema OPS.

**D5 — Dos cosas que quedaron a medias hace meses, ¿las quieres?** *(desbloquea la Ola 6)*
- **1**: silenciar la música/audio del PC automáticamente cuando dictas.
- **2**: cambiar los atajos de teclado desde el panel.
- Elige: **ambas** / **solo la 1** / **solo la 2** / **ninguna** *(recomendada: las archivamos y ya no estorban)*.

**D6 — En vivo, ¿el copiloto conecta la reunión con tus proyectos?** *(briefing v1.1)*
Te soltaría tarjetas "esto se relaciona con tu proyecto X" mientras hablas.
- **A** *(recomendada)*: no por ahora → ya funciona cuando lo preguntas en el chat; en vivo puede meter ruido.
- **B**: sí, aunque cueste afinarlo.
- **C**: no sé → para después.

**D7 — ¿Medir interrupciones (quién pisa a quién al hablar)?** *(métrica que quedó fuera)*
- **A** *(recomendada)*: no → hoy no se puede medir bien, saldría impreciso.
- **B**: sí, aunque sea aproximado.

**D8 — ¿Empaquetar Vflow como app instalable (.exe)?** *(build pospuesto)*
- **A** *(recomendada)*: no, sigo en modo desarrollo.
- **B**: sí, lo quiero en otra máquina o para compartir.
- **C**: más adelante.

---

## COMO EMPEZAR (esto es lo único que necesitas; tú solo tienes este documento)

Ruteo por régimen de costos:
- **Hasta 2026-07-12 ~mediodía (régimen A, Fable en cuota):** **director/orquestador = Fable 5
  (esfuerzo high, techo xhigh)** — se aprovecha la ventana en cuota (amortizar antes que API);
  **debate adversarial = Opus 4.8 high** (el adversario distinto de modelo da diversidad);
  **ejecutores = Sonnet 5 (estándar) y Haiku 4.5 (mecánico)**.
- **Desde 2026-07-12 mediodía (régimen B, Fable solo API):** **director = Opus 4.8 (techo xhigh)**;
  debate = Opus 4.8 high; ejecutores = Sonnet 5 / Haiku 4.5; Fable solo por API para clase diamante.
- Este plan no estampa ninguna unidad diamante; si un debate eleva una, se justifica en la unidad.
- El ejecutor hace auto-check: si su modelo es más débil que el mínimo del régimen vigente, avisa.

| Prioridad | Qué quiero | Cuándo | Frase EXACTA (copy-paste en ventana nueva) | Modelo · esfuerzo |
|---|---|---|---|---|
| 0 | **Correcciones críticas (Ola 0): el fuego primero** — race de ciclo de vida de reunión + pérdida de ~60s de dictado + guards de auto-repeat | LO ANTES POSIBLE (corrompen datos del usuario) | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 0. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · xhigh |
| 1 | Todo el plan en autónomo (recomendado; incluye la Ola 0 primero) | Cuando tengas 1-2 h de máquina disponible | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Orquestador Autónomo del final del documento. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · xhigh |
| 2 | Solo los quick wins (Ola 1) | Tras la Ola 0 | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 1. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · high |
| 3 | Suite de tests en verde (Ola 2) | Después de Ola 1 (o en paralelo, archivos disjuntos) | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 2. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · high |
| 4 | Panel en vivo sin techo (Ola 3) | Después de Olas 1-2 | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 3. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · high |
| 5 | Partir el monolito web (Ola 4, LA palanca) | Tras responder **D1** (arriba) | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 4. Sigue sus instrucciones al pie de la letra, incluido el auto-check de modelo.` | Opus 4.8 · xhigh |
| 6 | Features de producto elegidas | Tras responder **D4** (arriba) | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 5 SOLO con las unidades que indico: <lo que elegiste en D4>.` | Opus 4.8 · xhigh |
| 7 | Ejecutar la FASE3 heredada (Ola 6) | Tras responder **D5**, si elegiste alguna | `Lee C:\OPS\_VelOS\proyectos\Sflow.Win\PLAN-MEJORAS-2026-07-06.md y ejecuta el Kickoff Ola 6 SOLO con las features que indico: <lo que elegiste en D5>.` | Opus 4.8 · high |

Las decisiones (🙋) ya no son pasos sueltos: viven todas en el bloque **DECISIONES** de arriba
(D1-D8). Respóndelas una vez y el resto fluye. Los únicos gates que quedan durante la ejecución
son G2/G3 async (probar con la app real y decir si te convence lo visual), que las olas dejan
anotados en PROGRESS.md sin bloquear lo demás.

> **Qué significa "ejecuto este plan y ya" (lee esto):** la fila 1 (Orquestador Autónomo) corre
> SOLA las Olas 0→1→2→3 — los bugs críticos, la robustez, los tests en verde y el panel sin techo —
> sin que hagas nada salvo abrir la ventana. Se DETIENE y te pregunta solo en 3 puntos que son
> decisiones tuyas, no ejecución: partir el monolito (Ola 4), elegir features (Ola 5) y la FASE3
> heredada (Ola 6). Ahí respondes una frase y sigue. No tienes que unir pedazos ni recordar el orden:
> el orquestador lee este documento, respeta las dependencias y deja cada gate anotado en PROGRESS.md
> con la pregunta exacta. Tu única memoria entre ventanas es PROGRESS.md, que el propio orquestador
> mantiene al día.

---

## Resumen de olas, orden y dependencias

| Ola | Nombre | Por qué este orden | Dependencia de EJECUCIÓN | Orden de MERGE |
|---|---|---|---|---|
| 0 | Correcciones críticas (pasada Fable) | Corrompen datos del usuario (reunión cruzada, minuto de dictado perdido); van ANTES que todo | Ninguna | 1º |
| 1 | Quick wins de robustez y privacidad | Máximo valor por sesión; riesgo bajo; independientes | Ola 0 mergeada (1.1 y F1/F10 tocan core/meeting.py y core/proactive.py: la concurrencia se resuelve junta) | 2º |
| 2 | Suite de verificación en verde | Baseline verde ANTES de las olas grandes: sin ella, "0 regresiones" es una afirmación débil | Ninguna (archivos disjuntos de Ola 1 salvo detalles) | 2º |
| 3 | Escalabilidad del panel en vivo | Toca el hotspot web/server.py: conviene ANTES de la Ola 4 para no re-trabajar sobre el monolito ya partido (el diff es más chico en el formato actual) | Ola 1 mergeada (1.3/1.4 tocan web/server.py) | 3º |
| 4 | Partir el monolito web/server.py | La palanca de mantenibilidad; va después para que las olas 1-3 no naveguen un árbol a medio migrar | 🙋 decisión **D1** + Olas 1-3 mergeadas | 4º |
| 5 | Producto (opt-in por unidad) | Se construye sobre el árbol ya saneado; cada unidad es opcional | 🙋 decisión **D4**; 5.3 conviene tras Ola 4 | 6º |
| 6 | FASE3 heredada (plan escrito nunca ejecutado, opt-in) | Independiente del resto (archivos disjuntos: hotkey/audio, no toca reuniones); su spec exige re-validación por estar desactualizado | 🙋 decisión **D5** | En cualquier momento tras Ola 1 (comparte main.py y web/server.py: serializar con lo que esté corriendo) |

Hotspots que serializan (regla: unidades que los tocan van EN SERIE, nunca en paralelo):
`web/server.py`, `core/meeting.py`, `main.py`. Recursos únicos: puerto del dashboard, navegador de
verificación, DB dev.

---

## Ola 0 — Correcciones críticas (la pasada Fable; el fuego primero)

Todas nacen de la 2ª pasada de caza directa (Fable), verificadas con refutador fresco. Van antes que
cualquier mejora porque corrompen datos reales del usuario. Evidencia completa: informe, sección
"Hallazgos 2ª pasada (Fable)".

> **Por qué esto es urgente y no cosmético (léelo antes de posponerlo):** si usas Vflow a diario con
> el backend `claude-cli` y a veces re-pulsas AltGr+R cuando el pill tarda en "procesando", es
> PROBABLE que ya te haya pasado una reunión que no se grabó, un acta cruzada o rara, o un minuto de
> dictado perdido — sin que supieras por qué (los tres fallan en silencio, sin error visible). La
> Ola 0 cierra exactamente esas secuencias. No añade features: recupera confianza en que lo que
> capturas se guarda de verdad.

| Unidad | Qué (hallazgo) | Dificultad | Ejecutar con | Por qué (el riesgo) |
|---|---|---|---|---|
| 0.1 | **Serializar el ciclo de vida de la reunión (F1)**: ambas rutas de start (`main.py` `_on_meeting_toggle` y `web/server.py` `/api/meeting/start`) rechazan arrancar si hay un stop en curso (consultar `_meeting_stopping` / un estado `_stopping` de la sesión); y los daemons de insight/consolidación llevan un **token de generación** que descarta su merge si la reunión ya cambió. Objetivo: es IMPOSIBLE que `stop(A)` toque el estado o las fuentes de audio de una reunión B | Delicada | Opus 4.8 · xhigh dirige + Opus 4.8 · high ataca con el código real | Es el bug más grave del informe (corrupción de datos + leak de micrófono, prob. media). Tocar el ciclo de vida concurrente sin un modelo mental completo puede introducir un deadlock o una reunión que no arranca nunca; el debate adversarial es obligatorio ANTES de codear |
| 0.2 | **No perder chunks en el ensamblado del dictado (F2)**: `_transcribe_final` rastrea y joina (con timeout de gracia) los `_chunk_worker` en vuelo antes de ensamblar `_chunk_results`; si un worker excede el timeout, se registra el hueco explícitamente en vez de pegar texto incompleto en silencio | Delicada | Sonnet 5 · high, dirigida de cerca | Hot path del dictado (la feature principal); un join mal puesto cuelga el pegado. Tests sintéticos de carrera obligatorios (worker lento vs tramo final rápido) |
| 0.3 | **Guards de auto-repeat en AltGr+T y AltGr+R (F10)** + tapar el `MEETING.stop()` reentrante: replicar el patrón `_h_held/_a_held/_m_held` para T y R (hoy ausente), de modo que mantener la tecla no encadene toggles ni múltiples stop() concurrentes | Estándar | Sonnet 5 · high | Toca core/hotkey.py (hot path de todos los modos); un guard mal puesto rompería el toggle legítimo. Complementa 0.1: elimina uno de los disparadores de F1 |
| 0.4 | **Fixes de la cola de URL y el webhook (F3, F8, F9)**: `item_id=None` al inicio de cada iteración del worker (no marcar error al item anterior); clave de idempotencia en el insert de `transcriptions` para no duplicar tras crash; `allow_redirects=False` en el POST del webhook | Estándar | Sonnet 5 · med | Tres fixes acotados y de bajo riesgo; se agrupan por afinidad (fronteras externas) y para no fragmentar en commits triviales |

Orden dentro de la ola: 0.1 primero (el más grave y el que define el modelo de concurrencia), luego
0.2, 0.3 y 0.4 (0.3 y 0.4 tocan archivos que 0.1 puede haber movido: en serie con 0.1, pueden ir
seguidas). Todas en `windows-variant`, 1 unidad = 1 commit.

### Kickoff Ola 0 (copy-paste)

```
Eres el director de la Ola 0 (correcciones críticas) del plan de mejoras de Vflow. Auto-check:
declara tu modelo (esperado Opus 4.8 con techo xhigh; si eres más débil, avisa y espera). Lee
en orden: PROGRESS.md (si hay Next action de este plan, reanuda), PLAN-MEJORAS-2026-07-06.md
sección "Ola 0", AUDITORIA-FABLE-2026-07-06.md secciones "Hallazgos 2ª pasada (Fable)" y "Side
cases nuevos" (los disparadores concretos de F1/F2/F3/F8/F9/F10), CLAUDE.md del repo secciones
"Critical Implementation Details" (threading Qt) y "12/13/15" (ciclo de reunión, MCP, proactivo).
Skill: C:\OPS\.claude\skills\orquestar-agentes\SKILL.md.

OBLIGATORIO antes de tocar 0.1: debate adversarial (Opus 4.8 high con el código real de
core/meeting.py start/stop/_run_insight_update/_run_consolidation y las dos rutas de start)
sobre el diseño de la serialización y el token de generación; reconciliar por escrito en
PROGRESS.md. El riesgo del fix es introducir un deadlock o una reunión que no arranca: el
diseño se debate antes de codear.

Ejecuta 0.1 → 0.2 → 0.3 → 0.4 EN SERIE (0.1, 0.2, 0.3 tocan core/meeting.py, main.py,
core/hotkey.py; 0.4 toca web/server.py y core/webhook.py). Restricciones: no cambiar el
comportamiento observable de una reunión NORMAL (una sola a la vez) — los fixes solo cierran
las secuencias patológicas; fail-open del webhook y de la captura se mantienen; SAVE_HISTORY
se respeta. Verificación por unidad ANTES de commitear: suite pytest del venv del repo + tests
sintéticos de carrera nuevos (0.1: start durante stop no cruza estado; 0.2: worker lento no se
pierde; 0.3: auto-repeat no encadena toggles). Las pruebas que exijan tu audio físico o dos
AltGr+R reales van como G2 a PROGRESS.md PARA JOHANN. 1 unidad = 1 commit. PROHIBIDO: push,
borrar datos, leer .env, dependencias nuevas.
```

---

## Ola 1 — Quick wins de robustez y privacidad

| Unidad | Qué (evidencia en el informe) | Dificultad | Ejecutar con | Por qué (el riesgo) |
|---|---|---|---|---|
| 1.1 | Concurrencia proactiva: `threading.Lock` interno en `ProactiveGate` (core/proactive.py) envolviendo enqueue/pop_deliverable/_purge_expired/reset, y el par should_push+mark_pushed como sección crítica única (nuevo método atómico `try_push(kind)`); además `_last_error` de core/insights.py por tarea (live/batch) en vez de global único. Tests de carrera sintéticos | Estándar | Sonnet 5 · high | Concurrencia sutil: el fix es chico pero fácil de dejar un deadlock (el tick Qt llama cada 1s); NO tomar el lock durante trabajo largo, solo mutaciones |
| 1.2 | Restos en disco: TTL/limpieza de `last_failed_recording.wav` (borrarlo tras la siguiente transcripción exitosa y al arrancar si tiene más de 24 h) + notificación de tray cuando `_init_db` recupera una DB corrupta (hoy solo logger.warning) | Estándar | Sonnet 5 · med | Privacidad real (voz en claro) pero cambio acotado; cuidar no borrar el WAV que el usuario aún necesita para debug inmediato |
| 1.3 | Validar rutas configurables en `/api/settings` (web/server.py): `PENDING_EXPORT_DIR` exige directorio existente y rechaza rutas bajo directorios del sistema (Windows, Program Files, Startup); `OPS_BRIEFING_PATH` exige extensión .md. Error claro en la UI | Estándar | Sonnet 5 · med | Toca el hotspot web/server.py y el contrato de settings; validación demasiado estricta rompería el flujo legítimo (carpetas de red del OPS deben seguir pasando) |
| 1.4 | Fuente única de contratos duplicados: `_SILENCE_RMS` importado de config (borrar la copia de core/proactive.py:35) y `CARD_STYLES` definido UNA vez en Python e inyectado al JS del template como JSON (hoy hay dict en ui/hud_widget.py:73 y objeto JS en web/server.py:3146) | Estándar | Sonnet 5 · med | Toca web/server.py (serializa con 1.3) y el HUD; verificar visualmente que las tarjetas conservan estilo en web Y HUD |
| 1.5 | Mecánicas de higiene: (a) el repair de items huérfanos de `_url_queue_worker` pasa por un método nuevo `url_queue_repair_orphans()` de db/database.py en vez del sqlite3.connect crudo de web/server.py:67; (b) `GroqBackend._get_client()` recrea el cliente si `GROQ_API_KEY` cambió (comparar key actual vs la del cliente cacheado) | Mecánica | Haiku 4.5 · med (alternativa: Sonnet 5 low) | Cambios pequeños y bien especificados; el orquestador verifica el diff |
| 1.6 | Aplicar `dictionary.apply_replacements` también en la ruta de AUDIO de `transcribe_url` (core/url_transcribe.py: hoy solo la ruta de subtítulos lo hace, :730 vs :606-626), tras el ensamblado final de chunks | Estándar | Sonnet 5 · med | El chunking con carryover hace fácil aplicar el diccionario dos veces o sobre texto parcial; aplicarlo UNA vez al texto final |
| 1.7 | Cierre atómico de generación en el dictado: en main.py, la comparación `gen != self._generation` y la escritura del resultado del chunk ocurren juntas BAJO `_chunk_state_lock` (hoy el check va antes del lock, :609-616) | Estándar | Sonnet 5 · high | Hot path del dictado con 3 hilos; un error aquí degrada la feature principal. Tests sintéticos de generación obligatorios |
| 1.8 | Robustez de la ruta URL (F5, F6, F7): deduplicar el solape de ~2s en el empalme de chunks (hoy el texto se repite cada ~240s); un chunk que falla NO tira el trabajo previo (retry por chunk o resultado parcial con marcador de hueco); serializar `_clean_crypt32_argtypes` con un lock (hoy dos descargas concurrentes se pisan los argtypes del singleton crypt32) | Estándar | Sonnet 5 · high | La ruta URL es el motor más grande sin tests; el fix de crypt32 toca un contrato con core/secrets.py (DPAPI): verificar que ambos sigan funcionando |
| 1.9 | Contratos del acta y del presupuesto (F12, F13): gatear `momentos_destacados` por el parámetro `highlights` real (como se hace con las notas) para cerrar el hueco anti-alucinación, y tolerar string→lista; corregir `_truncate_transcript_to_budget` para que `allowance<=0` devuelva el recorte mínimo o "" (hoy devuelve el transcript COMPLETO, desbordando el contexto justo cuando más hay que truncar) | Estándar | Sonnet 5 · med | Ambos son huecos de contrato del LLM; el de presupuesto puede vaciar un acta con backend endpoint. Verificar con acta sintética grande |
| 1.10 | Reformateo de dictado con timeout REAL (F11): sustituir el `with ThreadPoolExecutor` (cuyo shutdown espera) por un executor con `shutdown(wait=False, cancel_futures=True)` para que el timeout de 8s sea duro como promete CLAUDE.md | Mecánica | Haiku 4.5 · med | Cambio pequeño y localizado; el orquestador verifica que el pegado no se cuelga con backend lento simulado |

### Kickoff Ola 1 (copy-paste)

```
Eres el director de la Ola 1 del plan de mejoras de Vflow. Auto-check: declara tu modelo.
Director esperado: Opus 4.8 (régimen B); si eres más débil, avisa y espera confirmación.
Lee en orden: (1) C:\OPS\_VelOS\proyectos\Sflow.Win\PROGRESS.md (si hay un "Next action"
de este plan, reanuda desde ahí), (2) PLAN-MEJORAS-2026-07-06.md sección "Ola 1" completa,
(3) AUDITORIA-FABLE-2026-07-06.md secciones "Tabla priorizada" y "Side cases" (contexto de
cada fix), (4) CLAUDE.md del repo secciones "Critical Implementation Details" y "15. Proactivo
v2". Skill de mecánica: C:\OPS\.claude\skills\orquestar-agentes\SKILL.md.
Antes de ejecutar: debate adversarial de la ola (otro Opus 4.8 high ataca el plan de las 10
unidades CONTRA el código real; reconciliar por escrito en PROGRESS.md).
Ejecuta las unidades con los modelos estampados en la tabla. EN SERIE las que tocan
web/server.py (1.3, 1.4), main.py (1.7, 1.10) y core/url_transcribe.py (1.8); el resto puede
paralelizarse con subagentes background de archivos disjuntos. PRECONDICIÓN: la Ola 0 ya
resolvió el ciclo de vida de reunión (F1) y los guards de auto-repeat (F10); NO re-toques esa
lógica. Restricciones no re-litigables: no rediseñar ProactiveGate
(solo añadir lock/atomicidad), no cambiar el shape de /api/settings, fail-open del briefing
y del VAD se mantienen. Verificación por unidad ANTES de commitear: suite pytest del repo
(venv del proyecto) + prueba de flujo específica; para 1.4, verificación visual de tarjetas
en web y HUD por estado computado (feedback OPS verificar-ui-como-el-usuario). 1 unidad =
1 commit. Actualiza PROGRESS.md al cerrar cada unidad (plantilla §8). Gates: pruebas físicas
que exijan hotkeys/audio real quedan como G2 en PARA JOHANN. PROHIBIDO: push, borrar datos,
leer .env, dependencias nuevas.
```

---

## Ola 2 — Suite de verificación en verde

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 2.1 | Suite verde: arreglar los 10 fallos preexistentes (mocks de Groq incompatibles con Python 3.14, confirmados con stash en la Ola 1 histórica). Criterio de éxito: `pytest tests/` = 0 fallos en el venv del repo | Estándar | Sonnet 5 · high | Los mocks rotos pueden ocultar regresiones reales; la tentación de "xfail y seguir" está prohibida salvo justificación escrita por test |
| 2.2 | Triage de los 8 tests huérfanos de la raíz (test_assistant.py, test_assistant_retrieval.py, test_chapters.py, test_dual_capture.py, test_fts_search.py, test_insights_task_split.py, test_openrouter_backend.py, test_reasoning_router.py; test_loopback.py se queda: es script de diagnóstico documentado): mover a tests/ los que aporten, borrar los obsoletos (con justificación por archivo en el commit), y documentar en CLAUDE.md cómo se corre TODA la suite | Estándar | Sonnet 5 · med | Decisiones de criterio (qué es obsoleto); borrar cobertura viva sería un daño silencioso |
| 2.3 | Cobertura mínima nueva: tests para core/secrets.py (roundtrip DPAPI cifrar/descifrar, migración de key en claro) y para las funciones puras de core/url_transcribe.py (detect_platform, parser VTT con dedup de cues, mapeo error_kind→HTTP) | Estándar | Sonnet 5 · med | Módulos hoy con 0 tests; secrets custodia las keys y url_transcribe es el motor más grande sin red de seguridad |

### Kickoff Ola 2 (copy-paste)

```
Eres el director de la Ola 2 del plan de mejoras de Vflow. Auto-check: declara tu modelo
(esperado Opus 4.8; si eres más débil, avisa y espera). Lee: PROGRESS.md,
PLAN-MEJORAS-2026-07-06.md sección "Ola 2", y CLAUDE.md del repo (sección Quick Start para
el venv). Debate adversarial breve de la ola (Opus 4.8 high) centrado en: ¿qué tests
huérfanos protegen invariantes que la suite de tests/ NO cubre? Ejecuta 2.1 → 2.2 → 2.3 en
serie (2.1 define el baseline). Regla dura: ningún test se borra ni se marca xfail sin
justificación escrita en el mensaje de commit. Criterio de cierre de la ola: pytest tests/
en verde total y CLAUDE.md documenta el comando canónico de la suite. 1 unidad = 1 commit.
Actualiza PROGRESS.md por unidad. PROHIBIDO: push, borrar datos, leer .env, dependencias
nuevas (si un fix exige actualizar un mock por versión de lib, usar lo ya instalado en el venv).
```

---

## Ola 3 — Escalabilidad del panel en vivo

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 3.1 | Polling incremental de reunión: `GET /api/meeting` acepta `since=<índice>` y devuelve solo segmentos nuevos + estado liviano (timer, levels, tarjetas); `core/meeting.py` mantiene `_segments` ordenado incrementalmente (hoy sorted() completo por llamada, :342-349); el JS unifica los DOS pollers actuales (2000ms en :2123 y 1500ms en :3479) en uno solo que pide incremental. Compatibilidad: sin `since`, la respuesta actual completa se mantiene (HUD y clientes viejos no se rompen) | Delicada | Sonnet 5 · high, dirigida de cerca por el orquestador | Toca el hotspot web/server.py + JS de dos vistas + el contrato de un endpoint vivo; una regresión aquí rompe la experiencia central de reunión. Verificación en navegador obligatoria con reunión sintética larga |
| 3.2 | Retención opcional de reuniones: `MEETING_RETENTION_DAYS` (default 0 = conservar siempre), purga al arrancar como la de dictados (db/database.py prune análogo, incluyendo FTS), setting en el panel Ajustes con advertencia clara | Estándar | Sonnet 5 · med | Borrado de datos del usuario: la única operación destructiva del plan; default apagado y test de que 0 = no borra NADA |

### Kickoff Ola 3 (copy-paste)

```
Eres el director de la Ola 3 del plan de mejoras de Vflow. Auto-check: declara tu modelo
(esperado Opus 4.8; si eres más débil, avisa y espera). Lee: PROGRESS.md,
PLAN-MEJORAS-2026-07-06.md sección "Ola 3", AUDITORIA-FABLE-2026-07-06.md hallazgo #3 (la
evidencia con líneas exactas), y CLAUDE.md del repo sección "10. Dashboard Shell" (subsección
"Panel en vivo push→pull"). Debate adversarial obligatorio de 3.1 (Opus 4.8 high, con el
código real delante): atacar el contrato since/compatibilidad y el unificado de pollers.
Restricciones no re-litigables: el shape actual SIN parámetro since no cambia (compatibilidad
HUD/vistas), no se introduce websocket ni SSE en esta ola (sobre-ingeniería para localhost),
la purga 3.2 con default 0 no borra nada. Ejecuta 3.1 y 3.2 EN SERIE (ambas tocan
web/server.py). Verificación 3.1: navegador con reunión sintética de cientos de segmentos,
medir payload por poll antes/después, pasada de superficie completa de /reunion y el panel
embebido (verificar-ui-como-el-usuario). Verificación 3.2: test de que default 0 no borra y
de que N días borra fila + FTS. 1 unidad = 1 commit. Gates G2 (prueba con reunión real) a
PROGRESS.md PARA JOHANN. PROHIBIDO: push, borrar datos reales, leer .env, dependencias nuevas.
```

---

## Ola 4 — Partir el monolito web/server.py (la palanca)

🙋 **D1 RESPONDIDA (2026-07-12): D1:B → alcance COMPLETO** (4.1 + 4.2 + 4.3 + abrir debate 4.4).

> **CONSTRAINT DE DISEÑO NO NEGOCIABLE (visión agéntica de Johann, 2026-07-12):** Vflow debe ser
> **autosuficiente por sí sola PERO conectable por un agente/sistema para llevarla al 200%** ("oídos
> para un agente", mercado agéntico; foso = integración con el cerebro de OPS vía API/MCP). Esto
> CONVIERTE la Ola 4 de "limpieza" en **la palanca que habilita el producto**: el criterio de éxito
> del reorden NO es solo "misma UI antes/después", sino que **cada feature exponga un contrato de
> servicio limpio que consuman por igual la UI, el MCP y una futura API de agentes** (regla OPS
> `era-agentica.md` + `arquitectura.md` feature-first: interfaz pública mínima por módulo). El debate
> 4.0 debe incorporar este criterio; 4.2 (blueprints) y 4.4 (encarpetar core/) se juzgan por si dejan
> esos contratos limpios, no solo por mover archivos. Unidad nueva **4.5** (abajo) materializa la
> superficie agéntica una vez existan los contratos. Ver memorias privadas
> `[[vflow-vision-macrosistema-ops]]` y `[[vflow-arquitectura-agent-ready]]`.

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 4.0 | Debate de diseño de la ola (obligatorio, antes de tocar código): plan de extracción por vistas, criterio de equivalencia (HTML servido comparable antes/después), impacto en vflow.spec/PyInstaller (templates y static como datas del bundle), orden de commits | Delicada (diseño) | Opus 4.8 · xhigh dirige + Opus 4.8 · high ataca | El costo de un mal corte aquí se paga en TODAS las olas futuras; el debate es más barato que un refactor a medias |
| 4.1 | Extraer el frontend inline a `web/templates/` (Jinja2) y `web/static/` SIN cambiar comportamiento: HTML_TEMPLATE (~2.770 líneas) y MEETING_PAGE (~875 líneas) salen de web/server.py; render_template; los tokens/design system a CSS propio; vflow.spec actualizado (aunque el build siga pospuesto, no dejarlo roto a sabiendas) | Delicada | Sonnet 5 · high por vista (subagentes en serie sobre web/), director verifica cada vista | Riesgo de romper la UI entera por un placeholder/escape mal migrado; verificación UI completa POR VISTA (estados computados + overlays trío oculto/abre/cierra) |
| 4.2 | Blueprints Flask por feature: dictados/historial, reunión, diccionario, cola URL, ajustes+keys, meetings API; web/server.py queda como app factory + registro (shim delgado). Los handlers NO cambian de lógica, solo de casa | Delicada | Sonnet 5 · high | Mover 112 rutas sin alterar CSRF before_request global ni el arranque del worker; un olvido deja un endpoint fuera de la protección |
| 4.3 | Catálogo único de config: inventario de los ~90 os.getenv fuera de config.py; los de lectura ÚNICA migran a constantes de config.py; los de lectura PEREZOSA deliberada (flags que se releen sin reiniciar) migran a helpers `config.get_*()` documentados que preservan esa semántica. CLAUDE.md apunta al catálogo | Delicada | Opus 4.8 · high dirige el inventario y clasifica; Haiku 4.5 ejecuta los lotes mecánicos | El riesgo NO es el edit (mecánico) sino clasificar mal una variable perezosa como estática: eso cambia comportamiento en caliente (kill-switches, backends) |
| 4.4 | 🙋 GATE decisión: ¿encarpetar core/ por features (meetings/, dictation/, llm/)? Alto blast radius. **Con D1:B + visión agéntica, el valor ya está justificado** (Vflow va a crecer a móvil + ser consumida por agentes): recomendación = SÍ, pero como ola nueva con su propio debate tras 4.1-4.3. Sigue necesitando tu OK explícito antes de ejecutar | (decisión) | 🙋 Johann | Deja los módulos con fronteras limpias que la capa agéntica (4.5) necesita |
| 4.5 | **Superficie agéntica de Vflow (agent-ready, era-agentica.md).** Materializa "oídos para un agente": (a) auditar qué capacidades hoy solo viven tras la UI HTTP (iniciar/parar reunión, dictado, nota, highlight, diccionario, cola URL, consultar memoria) y exponerlas por contratos limpios; (b) extender el MCP server (hoy read-only de reuniones) a las capacidades que un agente externo (OPS/Levy) necesite, con permisos acotados; (c) decidir API REST documentada vs MCP por caso de uso (MCP para consumo agéntico local tipo OPS; API para integración remota/móvil futura) + **CLI** junto al MCP (patrón Plaud MCP+CLI, investigación 2026-07-12: acceso por línea de comandos para devs/agentes, costo mínimo sobre los mismos contratos); (d) datos estructurados/`llms.txt`/`.well-known` si aplica al posicionamiento. **Criterio de diseño (investigación, Apéndice C): el contrato central NO es CRUD de transcripts (eso ya es table stakes — Plaud/Granola/Fireflies/Otter tienen MCP) sino el "PAQUETE DE CONTEXTO consolidado" — la capa de memoria-de-trabajo que la categoría no ha resuelto.** Autosuficiencia intacta: nada de esto es requerido para que Vflow funcione sola. GATE G-agent: el CONTRATO de qué se expone y con qué permisos se debate y espera tu ojo ANTES de codear (superficie consumible por terceros, se fosiliza). **NOTA 2026-07-12: el diseño del contrato se adelantó con Fable — ver `docs/CONTRATO-MACROSISTEMA.md` (diseño debatido; la implementación sigue viniendo DESPUÉS de 4.1-4.4)** | Delicada (contrato + seguridad) | Diseño: HECHO con Fable 5 (2026-07-12) → Sonnet 5 · high (código, tras el reorden) | Es la pieza que convierte a Vflow en sensor del macrosistema; depende de que 4.1-4.4 dejen contratos limpios (por eso la implementación va DESPUÉS del reorden — construir la capa agéntica sobre el monolito sería deuda) |

### Kickoff Ola 4 (copy-paste)

```
Eres el director de la Ola 4 del plan de mejoras de Vflow. Auto-check: declara tu modelo
(esperado Opus 4.8 con techo xhigh; si eres más débil, avisa y espera). PRECONDICIÓN: busca
la respuesta de Johann a la decisión D1 (bloque DECISIONES del plan) en PROGRESS.md PARA
JOHANN o en el chat → D1:A/C = alcance 4.1+4.3; D1:B = 4.1+4.2+4.3. Si D1 está sin responder,
PARA y preséntala con sus opciones tal cual. Lee: PROGRESS.md,
PLAN-MEJORAS-2026-07-06.md sección "Ola 4", AUDITORIA-FABLE-2026-07-06.md hallazgos #1 y #8,
CLAUDE.md del repo secciones "7. Bundle vs Dev Mode" y "10. Dashboard Shell", y la regla
C:\OPS\.claude\rules\arquitectura.md (regla 8, entrypoints como shims). Ejecuta 4.0 (debate
con Opus 4.8 high y código real) y registra la reconciliación en PROGRESS.md ANTES de tocar
código. Luego las unidades aprobadas EN SERIE (todas tocan web/server.py). Verificación por
vista extraída: dashboard servido en un puerto propio, pasada de superficie completa
(screenshot/estado computado de CADA vista + shell + overlays con trío oculto/abre/cierra,
feedback verificar-ui-como-el-usuario), suite pytest verde (baseline de la Ola 2), y para 4.3
prueba explícita de que los flags perezosos (PROACTIVE_MODE, kill-switches
PROACTIVE_DETECT_*, TRANSCRIPTION_BACKEND) siguen releyéndose sin reiniciar. Al ~50% de tu
ventana: cierra la unidad en curso, actualiza PROGRESS.md (Next action exacto) y pide ventana
nueva con ESTE kickoff. 1 unidad = 1 commit (4.1 puede ser 1 commit por vista si el debate lo
aprueba). PROHIBIDO: push, borrar datos, leer .env, dependencias nuevas, y ejecutar 4.4 sin
gate explícito de Johann.
```

---

## Ola 5 — Producto (opt-in: NINGUNA unidad corre sin tu elección explícita)

🙋 **Depende de la decisión D4 (bloque DECISIONES, arriba).** Mapa D4 → unidad: opción 1 =
5.1 (auto-highlights) · opción 2 = 5.2 (contexto personal) · opción 3 = 5.3 (chat cross-reunión
+ entregables) · opción 4 = 5.4 (pendientes → OPS). Recomendación de orden por ROI/esfuerzo:
5.2 primero (mínima), luego 5.4 y 5.1, y 5.3 al final. Sin D4 respondida, las unidades 5.1-5.4 NO corren.
**Excepción — 5.5 (fallback simétrico online↔local) YA está elegida por Johann (2026-07-12) y NO
depende de D4:** puede correr sola, tras su debate previo; toca core/transcriber.py (no reunión ni URL
en v1), así que se serializa con cualquier otra unidad que toque ese archivo.

| Unidad | Qué (detalle en el informe, sección "Oportunidades de producto") | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 5.1 | Marcado AUTOMÁTICO de momentos clave: el insight stream marca candidatos a highlight como segunda intención del MISMO update_state (patrón ya probado por las detecciones de la Ola 5 histórica, cero cuota LLM extra); se persisten en highlights_json marcados como `auto`, distintos de los manuales AltGr+H (que siempre mandan y nunca se pisan); render diferenciado en acta/dashboard/MCP. Nota: el plan original proponía aquí "acta instantánea al colgar", retirada en la 2ª pasada porque el acta YA se genera sola en stop() (core/meeting.py:609) | Delicada (calidad de señal) | Sonnet 5 · high, con calibración contra ≥3 transcripts reales de la DB dirigida por el orquestador | La lección del proactivo v1: una detección ruidosa mata el feature; si la señal/ruido no convence en la calibración, se deja tras flag apagado por default (mismo criterio que la Ola 5 histórica) |
| 5.2 | Contexto personal en prompts: setting (nombre, rol, dominio) inyectado en los system prompts de insights/acta/chat; "Yo" pasa a ser el nombre real en pendientes con responsable | Mecánica | Haiku 4.5 · med | Cambio de prompt acotado; el orquestador valida con una reunión sintética que los pendientes atribuyen bien |
| 5.3 | Chat de memoria CROSS-reunión + entregables: seleccionar N reuniones (o "todas las de un tema" vía FTS) y chatear sobre el conjunto; 2-3 plantillas de entregable (email de seguimiento, informe). Presupuesto de contexto por backend reusa insights.budget_chars. **Criterio de diseño (investigación 2026-07-12): los incumbentes paran en transcript+resumen+push a CRM — nuestro cierre diferencial es el entregable ACCIONADO hacia el OS del usuario (el email/informe sale listo hacia OPS vía el contrato de 5.4), no el chat en sí** | Delicada | Opus 4.8 · high diseña prompts y presupuesto multi-acta + Sonnet 5 · high ejecuta; debate previo obligatorio | Presupuesto multi-reunión es fácil de reventar (N actas > ventana del backend endpoint 18KB); el debate define el recorte ANTES de codear |
| 5.4 | Pendientes → OPS: los compromisos de una reunión se exportan como tareas consumibles por el OPS (formato markdown de tarea acordado + carpeta observada o tool MCP nueva de escritura acotada). GATE G1: el CONTRATO (formato + ubicación + qué escribe quién) se diseña, se debate y espera tu ojo ANTES de codear. **NOTA 2026-07-12: el contrato se adelantó con Fable — ver `docs/CONTRATO-MACROSISTEMA.md`; si Johann ya dio su ojo (G1) ahí, el ejecutor va directo al código contra ese contrato.** Es además el EXPERIMENTO DE VALIDACIÓN de la tesis macrosistema (investigación 2026-07-12): tratar su resultado como señal de producto, no solo feature | Delicada (contrato) + estándar (código) | Contrato: HECHO con Fable 5 (2026-07-12, gate G1 a Johann) → Sonnet 5 · med (código tras la aprobación) | Contrato consumible por terceros (Levy/OPS): un formato mal elegido se fosiliza; por eso G1 |
| 5.5 | **Fallback simétrico de transcripción online↔local** (pedido de Johann 2026-07-12, YA elegido — corre sin depender de D4). Hoy `GROQ_FALLBACK` cubre SOLO local-primario→Groq (core/transcriber.py:177,308-313). Falta el ESPEJO: con backend primario = Groq (online), si la transcripción falla por red (Connection error/timeout) cae AUTOMÁTICAMENTE al modelo local; en el siguiente dictado re-intenta Groq primero (re-probe por evento, patrón del circuit breaker de core/insights.py `_breaker` con cooldown). Unificar bajo un solo concepto de "fallback automático" que opera en la dirección que corresponda según el backend primario. Cero costo en tokens (Whisper local CTranslate2). **Condición de diseño (la resuelve el debate):** el modelo local DEBE estar descargado para Groq→local; si no lo está, avisar (tray/pill) en vez de fallar mudo — NO auto-descargar en el hot-path del dictado. Alcance v1: ruta de DICTADO (transcribe/translate); reunión y URL quedan fuera salvo que el debate diga lo contrario. | Estándar (simétrico a lógica existente) | Sonnet 5 · high, debate previo Opus 4.8 (dirección del fallback + guard de modelo no descargado + qué cuenta como "fallo de red" vs error real) | Resiliencia = la herramienta siempre funciona sin internet; refuerza el núcleo local/privado. Bajo riesgo: espeja `GROQ_FALLBACK` que ya existe y está probado |
| 5.6 | **Notas híbridas estilo Granola** (de la investigación de mercado 2026-07-12, Apéndice C — validada por el producto de culto de la categoría, $1.5B/churn~0). Las notas rápidas 📝 que el usuario toma EN VIVO (hoy: literales + contexto IA en el acta, unidad 4.1 histórica) ganan un modo "enriquecer": al cerrar la reunión, el LLM convierte los apuntes esqueléticos del usuario en notas COMPLETAS respaldadas por el transcript — el apunte del humano manda (estructura/énfasis), el transcript rellena. Regla dura heredada: el texto LITERAL del usuario NUNCA se pierde (se conserva como hoy en `nota`; lo enriquecido va en campo nuevo, p. ej. `nota_expandida`), protegido por post-proceso determinista como las notas actuales. Render en acta/dashboard/export con toggle literal↔expandida. Opt-in por reunión o setting. | Delicada (calidad de señal + contrato del acta) | Sonnet 5 · high ejecuta, debate previo Opus 4.8 (prompt + presupuesto: reusa insights.budget_chars; validar contra ≥2 reuniones reales) | Es LA feature por la que la gente ama a Granola y encaja exacto en nuestro pipeline de notas ya existente; diferencial local: lo hacemos sin bot y sin nube obligatoria |
| 5.7 | **Panel de privacidad verificable** (de la investigación 2026-07-12, Apéndice C — convierte la queja #1 contra Plaud, "promete privacidad pero procesa en nube", en PRUEBA del posicionamiento local-first). Registro de EGRESOS de datos: cada vez que algo sale de la máquina (audio a Groq, prompt a claude-cli/anthropic/openrouter, POST del webhook, briefing en fallback cloud), se anota evento {timestamp, destino, tipo de dato (audio/texto/acta), tamaño, backend} en una tabla local (migración idempotente; NUNCA el contenido, solo metadatos — coherente con la regla del briefing). Vista "Privacidad" en el dashboard: timeline de egresos + resumen "hoy salieron X de tu máquina / todo se procesó local". Kill-switch ya existente por diseño: con backend local + sin webhook, el panel muestra cero egresos — esa pantalla ES el argumento de venta. | Estándar (instrumentación transversal + una vista) | Sonnet 5 · high (tocará los 4-5 puntos de egreso: transcriber/insights/webhook — serializar con lo que toque esos archivos) | Nadie en la categoría lo tiene; costo bajo porque los puntos de egreso son conocidos y finitos; refuerza el núcleo del producto y el pitch |

### Kickoff Ola 5 (copy-paste; añade tu lista de unidades al final de la frase)

```
Eres el director de la Ola 5 del plan de mejoras de Vflow. Auto-check: declara tu modelo
(esperado Opus 4.8; si eres más débil, avisa y espera). PRECONDICIÓN dura: Johann debe haber
respondido la decisión D4 (bloque DECISIONES del plan) — mapa opción→unidad: 1=5.1, 2=5.2,
3=5.3, 4=5.4 —; si D4 está sin responder, PARA y preséntala con sus opciones tal cual. Lee:
PROGRESS.md, PLAN-MEJORAS-2026-07-06.md
sección "Ola 5", AUDITORIA-FABLE-2026-07-06.md sección "Oportunidades de producto", CLAUDE.md
del repo secciones 13-17 (contratos de reunión/insights/asistente). Debate adversarial por
unidad delicada (5.3, 5.4) con Opus 4.8 high antes de codear; 5.4 además tiene gate G1: el
contrato diseñado + debatido se deja en PROGRESS.md PARA JOHANN y NO se codea hasta su ojo.
Restricciones: cero llamadas LLM nuevas en 5.1 (los candidatos a highlight viajan como
segunda intención del update_state existente, patrón de las detecciones; los highlights
manuales AltGr+H siempre mandan y se distinguen de los auto), el briefing OPS y sus gates
de privacidad no se tocan, SAVE_HISTORY=false se respeta en todo. Verificación por unidad:
suite verde + flujo real (para 5.1 calibración obligatoria contra ≥3 transcripts reales de
la DB reportando precisión percibida: si no convence, flag apagado por default; para 5.3
presupuesto probado con actas grandes sintéticas). 1 unidad = 1 commit. PROHIBIDO: push,
borrar datos, leer .env, dependencias nuevas.
```

---

## Ola 6 — FASE3 heredada (plan escrito nunca ejecutado; opt-in por feature)

🙋 **Depende de la decisión D5 (bloque DECISIONES, arriba).** Mapa D5 → unidad: opción 1 =
6.1 (silenciar audio al dictar) · opción 2 = 6.2 (hotkeys configurables). `docs/FASE3_SPEC.md`
define estas dos features, nunca implementadas (verificado: no existen `core/audio_session.py`
ni `core/hotkey_config.py`, no hay pycaw). El spec es ANTERIOR al giro hacia reuniones y está
desactualizado (cita "AltGr+Space" que luego fue AltGr+T, la ruta vieja y un web/server.py
pre-rediseño). Si D5 = ninguna, archivar el spec (a docs/archivo/ con nota) para que ningún
agente futuro lo ejecute por error.

| Unidad | Qué (detalle completo en docs/FASE3_SPEC.md, a RE-VALIDAR) | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 6.1 | Feature A: silenciar el audio del sistema mientras se dicta (pycaw/WASAPI, toggle opt-in en Ajustes, restaurar SIEMPRE al terminar). El propio spec marca el riesgo ALTO: COMErrors con dispositivos virtuales (VB-Cable, Voicemeeter), cambios de dispositivo a mitad de dictado; todo fail-open (si el mute falla, el dictado sigue) | Delicada | Sonnet 5 · high, debate previo Opus 4.8 high | Dependencia nueva (pycaw: aplica la política de 30 días + regenerar lock) + interacción con AUDIO_SOURCE=system (NUNCA mutear lo que se está capturando en una reunión: exclusión mutua explícita) |
| 6.2 | Feature B: hotkeys configurables desde el dashboard (core/hotkey_config.py + JSON + UI en Ajustes), hoy hardcodeados en core/hotkey.py | Delicada | Sonnet 5 · high, debate previo Opus 4.8 high | core/hotkey.py creció mucho desde el spec (modos 1-4 + AltGr+H/A/M/R + anti auto-repeat + arming): el mapa del spec ya no corresponde; el debate decide si se hace completo o una versión mínima (remapear solo los AltGr+letra) |

Regla dura de la ola: el spec NO se sigue a ciegas. Primera tarea del director: re-validar
cada sección del spec contra el código actual y registrar en PROGRESS.md qué sigue vigente y
qué cambió; el debate adversarial ataca ESA reconciliación, no el spec original.

### Kickoff Ola 6 (copy-paste; añade qué features van: A, B o ambas)

```
Eres el director de la Ola 6 (FASE3 heredada) del plan de mejoras de Vflow. Auto-check:
declara tu modelo (esperado Opus 4.8; si eres más débil, avisa y espera). PRECONDICIÓN dura:
Johann debe haber respondido la decisión D5 (bloque DECISIONES del plan) — opción 1 = 6.1
mute al dictar, opción 2 = 6.2 hotkeys configurables; si D5 está sin responder, PARA y
preséntala con sus opciones tal cual. Lee: PROGRESS.md, PLAN-MEJORAS-2026-07-06.md sección "Ola 6",
docs/FASE3_SPEC.md COMPLETO (es el spec original, DESACTUALIZADO a sabiendas), CLAUDE.md del
repo secciones "Hotkeys" y "Audio", y core/hotkey.py + core/recorder.py reales. Primera
tarea: reconciliación escrita spec-vs-código-actual en PROGRESS.md (qué sigue vigente, qué
cambió, qué side cases nuevos aparecieron: AltGr+H/A/M/R, AUDIO_SOURCE=system, reunión
activa). Debate adversarial (Opus 4.8 high) sobre ESA reconciliación antes de codear.
Restricciones: mute (6.1) es opt-in apagado por default, fail-open total, y con exclusión
mutua explícita con la captura de reunión (jamás mutear la fuente que se graba); pycaw pasa
la política de dependencias (30 días en PyPI + requirements.in + lock regenerado con hashes).
Verificación por flujo real; los casos que exijan tu audio físico quedan como G2 en PARA
JOHANN. 1 unidad = 1 commit. PROHIBIDO: push, borrar datos, leer .env.
```

---

## Kickoff Orquestador Autónomo (todas las olas pendientes en una ventana)

```
Eres el ORQUESTADOR AUTÓNOMO del plan de mejoras de Vflow (Sflow.Win). Auto-check: declara tu
modelo. Director válido según el régimen de la cabecera del plan: hasta 2026-07-12 ~mediodía
Fable 5 (en cuota) dirige; después Opus 4.8. Si eres Fable o Opus, procede; si eres más débil
(Sonnet/Haiku como director), avisa y espera. Corres DESATENDIDO (Johann duerme): no le
preguntes nada que no sea un GATE; ante duda no bloqueante, elige la opción más conservadora,
anótala en PROGRESS.md y sigue. Lee en orden: C:\OPS\_VelOS\proyectos\Sflow.Win\PROGRESS.md (si existe
un Next action de ESTE plan, reanuda desde ahí), PLAN-MEJORAS-2026-07-06.md completo,
AUDITORIA-FABLE-2026-07-06.md (el diagnóstico del que nace todo), y la skill
C:\OPS\.claude\skills\orquestar-agentes\SKILL.md. Las reglas de este kickoff aplican por
encima del gatillo más laxo de la skill.

OJO con el estado de PROGRESS.md: hoy refleja el PLAN-OLAS histórico (olas 1-7 COMPLETAS, otro
plan ya cerrado). ESO NO ES tu trabajo. Tu trabajo activo es ESTE documento (PLAN-MEJORAS,
olas 0-6). Al arrancar, crea en PROGRESS.md una sección nueva "PLAN-MEJORAS-2026-07-06 (activo)"
y lleva ahí tu handoff; no borres ni toques el historial del PLAN-OLAS. Garantía de que nada se
queda fuera: este plan es la puerta única — su nota de cabecera y la sección "Planes escritos no
ejecutados o parciales" del informe listan TODOS los documentos del repo (PLAN-OLAS, PENDIENTES,
FASE3_SPEC, PRP) y su estado; lo que no está en una ola de este plan está PARQUEADO en su
documento dueño a propósito, no perdido. Si en cualquier momento dudas si algo quedó fuera,
vuelve a esa sección del informe: es el inventario maestro.

Misión: ejecutar las olas 0 → 1 → 2 → 3 en ese orden sin intervención humana salvo GATES. La
Ola 0 (correcciones críticas: corrompen datos del usuario) va PRIMERO y su unidad 0.1 exige
debate adversarial antes de codear. Las Olas 4/5/6 dependen de decisiones de Johann del bloque
DECISIONES del plan (D1 → Ola 4, D4 → Ola 5, D5 → Ola 6): busca sus respuestas en PROGRESS.md
PARA JOHANN o en el chat; si la decisión que una ola necesita está SIN responder, NO la
interpretes — deja esa ola anotada como gate con la decisión pendiente (copia sus opciones tal
cual del bloque DECISIONES) y sigue con lo demás. Termina el run con lo ejecutable agotado. Las
decisiones D6 (briefing v1.1 / tarjetas 🧭 Contexto) y D7 (interrupciones v2) NO tienen ola en
este plan: si Johann las responde que SÍ, eso abre una unidad nueva con su propio debate — NO
las ejecutes por tu cuenta, solo anótalas como trabajo futuro.

Reglas:
1. Scheduler: paralelo SOLO entre unidades de archivos disjuntos (subagentes background).
   Unidades que tocan web/server.py, core/meeting.py o main.py van EN SERIE.
2. Recursos únicos en serie: puerto de verificación, navegador, DB dev. El verificador
   levanta su PROPIO server fresco DESPUÉS de sus ediciones; nunca reuses un server stale.
3. Brief en frío por unidad + estampa del plan (clase → modelo · esfuerzo). Ejecutores:
   Sonnet 5 estándar/delicada, Haiku 4.5 mecánica. Fable por API SOLO si un debate eleva una
   unidad a diamante y lo justifica por escrito. Máx 2 reintentos; al 3º, gate G4.
4. Nada se integra sin verificar: diff real + prueba de flujo + suite pytest del venv del
   repo. 1 unidad = 1 commit local (política A: sin push). Cola de merge en PROGRESS.md.
5. Higiene de contexto del padre: nunca leas tú los archivos grandes; delega y recibe
   destilados. Al ~50% de tu ventana: cierra la unidad en curso, actualiza PROGRESS.md
   (plantilla §8, Next action exacto) y pide reanudar en ventana nueva con ESTE kickoff.
6. Debate adversarial POR OLA antes de ejecutarla (Opus 4.8 high ataca con el código real
   delante; reconciliación por escrito en PROGRESS.md). Este plan NO ha sido debatido aún:
   el debate de cada ola es parte del run, no opcional.
7. Coexistencia: git status antes de cada unidad; cambios ajenos sin commitear se respetan.
   Si hubiera un segundo escritor concurrente, aislar por worktree a nivel de sesión.
8. Techo del run: si la suma de reintentos + debates te lleva a más de ~2 olas por ventana o
   percibes gasto desviado, PARA, actualiza PROGRESS.md y reporta en vez de seguir quemando.

GATES (a PROGRESS.md sección "PARA JOHANN"; continúa con lo no bloqueado):
- G1: contrato de la unidad 5.4 (pendientes → OPS).
- G2: pruebas físicas (hotkeys, audio real, reunión real) que dejen las olas 1 y 3.
- G3: gusto visual tras 1.4 y toda la Ola 4 (revisión async).
- G4: tercer reintento, supuesto roto del plan, o decisión de producto no prevista.
- G5: bloqueo por sistema externo que no controlas.
PROHIBIDO siempre: push a cualquier remoto, borrar datos reales, leer/tocar .env,
dependencias nuevas fuera de la política del proyecto (requirements.in + lock con hashes,
paquetes con más de 30 días en PyPI).
```

---

## Registro del debate adversarial del plan

PENDIENTE por diseño: este plan lo escribió el auditor (Fable) en la misma sesión del
diagnóstico. La política del kickoff (regla 6) exige que CADA ola pase su debate adversarial
(Opus 4.8 high, con código real) antes de ejecutarse, y la reconciliación se registra en
PROGRESS.md. El debate de la Ola 4 (unidad 4.0) es además una unidad explícita del plan.
