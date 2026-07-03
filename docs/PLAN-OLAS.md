# Plan por olas — Vflow al siguiente nivel (jul 2026)

> Autor: Fable 5 (sesión 2026-07-03, con TODO el contexto de la investigación Tactiq/Fireflies/
> Granola/Fathom y las decisiones de diseño ya aprobadas por Johann). Fuente de detalle:
> `docs/PENDIENTES.md` (secciones Tactiq, Fireflies, UX/UI, panel push→pull, proactivo v2).
> Proceso: `C:\OPS\.claude\skills\orquestar-agentes\SKILL.md` y su variante
> `orquestar-agentes-fable` (§3.5: cada unidad va estampada con dificultad + modelo + porqué).

## Con qué modelo lanzar cada kickoff

**Regla (feedback modelo-esfuerzo-por-tarea, régimen de costo vigente):**

- **Hasta 2026-07-07 (régimen A, Fable en cuota):** kickoff en plan mode con **Fable 5,
  esfuerzo `medium`** (techo `high` si el diseño se complica). La ventana se aprovecha para
  las sesiones de PLAN/DISEÑO (donde Fable rinde más), NO para apurar ejecución: un contrato
  mal diseñado cuesta más que los tokens de Opus después (objeción #6 del debate, aceptada).
- **Desde 2026-07-08 (régimen B, Fable solo API):** kickoff con **Opus 4.8 `high→xhigh`**.
  Escalar a Fable 5 API solo para lo clase-Diamond o si Opus falla tras 2 reintentos.
- El que planea estampa el ruteo por unidad (ya viene estampado abajo); el ejecutor hace
  auto-check: si su modelo es más débil que el estampado, avisa antes de proceder.
- Techo de esfuerzo SIEMPRE `xhigh`, nunca `max`.

## Modo autónomo: Kickoff Orquestador (ejecuta olas sin depender de Johann)

Alternativa a lanzar ola por ola: UNA ventana con Fable orquestando todas las olas
pendientes, con gates explícitos para lo único que requiere atención humana. Límites
físicos asumidos: las olas son secuenciales por dependencia; el paralelismo real es entre
unidades de ARCHIVOS DISJUNTOS (web/server.py es hotspot: sus unidades van en serie, sin
worktrees: el merge del template inline no es viable); recursos compartidos (puerto de
verificación, navegador, DB dev) en serie (§4). Para autonomía real, lanzar la sesión con
permisos amplios (o aceptar ediciones), sabiendo el tradeoff.

### Kickoff Orquestador (copiar y pegar; también sirve para REANUDAR)

```
Eres el ORQUESTADOR AUTÓNOMO del plan de olas de Vflow. Auto-check: declara tu modelo
(esperado Fable 5; si eres más débil, avisa y espera). Lee en orden: PROGRESS.md (si
existe: reanuda desde su Next action y sáltate lo hecho), docs/PLAN-OLAS.md completo,
C:\OPS\.claude\skills\orquestar-agentes-fable\SKILL.md.

Misión: ejecutar las olas pendientes EN ORDEN de dependencias, sin intervención de
Johann salvo los GATES. Trabaja hasta agotar lo ejecutable.

Reglas de ejecución:
1. Scheduler: unidades de archivos disjuntos pueden correr en paralelo (subagentes
   background; isolation worktree SOLO si mutan archivos a la vez). Unidades que tocan
   web/server.py u otro hotspot van EN SERIE. Un cambio multi-archivo = un solo agente.
2. Recursos compartidos en serie: un solo verificador a la vez (Flask standalone en
   puerto 5679, navegador con cache-busting ?v=N, DB dev). Matar el Flask al terminar.
3. Cada unidad: brief autocontenido en frío (§3: objetivo, contexto, contrato de salida
   apretado, fronteras de archivos) + estampa de modelo del plan. Máx 2 reintentos; al
   3º, gate G4.
4. Nada se integra sin verificar (§5): diff real (git diff acotado, como SCRIPT) +
   prueba de flujo en navegador. 1 unidad = 1 commit (sin push).
5. HIGIENE DE CONTEXTO (regla de oro): NUNCA leas tú los archivos grandes (web/server.py,
   core/*.py completos): delega lectura y ejecución; recibe destilados estructurados.
   Reancla desde PROGRESS.md al abrir cada ola. Si tu contexto pasa ~50%: cierra la
   unidad en curso, actualiza PROGRESS.md (plantilla §8, con Next action exacto por
   tarea) y termina tu turno pidiendo a Johann reanudar en ventana nueva con ESTE mismo
   kickoff. Tu memoria es PROGRESS.md, no el hilo.
6. Debate §10.2 por OLA: antes de ejecutar cada ola, somete su plan de unidades al
   ataque de Opus (esfuerzo graduado por riesgo) y reconcilia por escrito en PROGRESS.md
   (Decisiones). Las correcciones del debate ya registradas en PLAN-OLAS no se re-litigan.
7. Coexistencia: puede haber OTRA sesión trabajando en el repo. Antes de cada unidad:
   git status; si hay cambios ajenos sin commitear en un archivo que vas a tocar,
   trabaja ENCIMA (nunca revertir) y commitea SOLO tus archivos.

GATES (lo único que espera a Johann; anótalo en PROGRESS.md sección "PARA JOHANN" con
qué revisar y cómo, y CONTINÚA con trabajo no bloqueado si existe):
- G1 Contratos consumibles por terceros (p. ej. cambiar el MCP): diseñar + debatir + dejar
  el contrato para su ojo antes de codearlo.
- G2 Pruebas físicas que exigen humano: dictar con voz real, reunión real AltGr+R con
  audio, hotkeys contra IDE. Deja el caso de prueba escrito, paso a paso.
- G3 Gusto visual: al cerrar cada ola de UI deja screenshots en docs/screenshots/ y sigue;
  Johann revisa async y pide ajustes después.
- G4 Tercer reintento fallido, supuesto del plan roto, o decisión de producto no prevista.
PROHIBIDO siempre: push a remoto, borrar datos de la DB, leer/tocar .env, dependencias
nuevas sin la política de 30 días, tocar el flujo de dictado sin unidad que lo pida.
```

### Qué NO se puede automatizar (expectativas honestas)

- Micrófono y voz: G2 existe porque un agente no puede hablar ni oír tu setup real.
- El gusto: la UI se verifica funcional en navegador, pero "me convence cómo se ve" es tuyo (G3, async).
- La cuota/modelo: si el orquestador agota contexto o cuota, el handoff por PROGRESS.md
  requiere que TÚ abras la ventana nueva (30 segundos, no revisión).

## Frase de arranque estándar (para Johann, en ventana nueva con plan mode)

```
Lee docs/PLAN-OLAS.md y ejecuta el Kickoff Ola <N>. Sigue sus instrucciones al pie de la
letra, incluido el auto-check de modelo.
```

Aplica a las olas 2-6 (un kickoff por ola). ÚNICA excepción: la Ola 1 tiene DOS kickoffs
(1a: MCP, 1b: visual) por decisión del debate adversarial (objeción #5: la pieza delicada
no comparte corrida con las mecánicas). Se lanzan como "Kickoff Ola 1a" y "Kickoff Ola 1b",
en cualquier orden o en paralelo (archivos disjuntos). Orden entre olas: respetar las
dependencias del resumen (la 2 requiere el CSS de 1b; la 5 requiere 2 y 3).

**¿Orquestar-agentes?** Sí, pero solo donde paga (filtro maestro §0): las olas con 2-3
unidades independientes se paralelizan con ejecutores baratos y verificación antes de
integrar; el núcleo secuencial (contratos, prompts) lo hace el orquestador o un L1. Si el
loop principal es Fable, aplicar la variante `orquestar-agentes-fable` (debate adversarial
con Opus como política por defecto en decisiones irreversibles). Cada ola crea/actualiza su
`PROGRESS.md` (§8) y cada unidad = 1 commit verificable.

---

## Resumen de olas (mi orden de importancia, de mayor a menor)

| Ola | Nombre | Por qué este puesto |
|---|---|---|
| 1 | Palanca agéntica + base visual | MCP = mayor palanca (validado por Tactiq Y Fireflies); CSS transforma la percepción con 1 día. Tras el debate: DOS kickoffs (1a: MCP solo con su debate de contrato; 1b: visual + highlight) |
| 2 | Panel en vivo push→pull | El rediseño ya aprobado; uso diario; prerequisito del proactivo v2 |
| 3 | Conversation intelligence Yo/Ellos | Diferenciador que NADIE tiene local; barato (VAD ya en stack); produce el sensor de silencios que usa la Ola 5 |
| 4 | Patrón Granola | Notas + IA = la profundidad del acta; necesita el panel de la Ola 2 maduro |
| 5 | Proactivo v2 | El de mayor incertidumbre: requiere iterar prompts con reuniones reales; depende de Olas 2 (panel) y 3 (VAD/silencios) |
| 6 | Plataforma y dictado | Conveniencias de alto valor pero sin dependencias urgentes: webhook, pendientes→OPS, mejoras de dictado, navegación |

Dependencias duras: 2 requiere 1(CSS) · 5 requiere 2 y 3 · el resto es secuenciable por valor.

---

## Ola 1 — Palanca agéntica + base visual

**Objetivo:** Vflow consultable por agentes del OPS + el dashboard deja de verse "triste".

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 1.1 | **Servidor MCP local** (stdio): tools `search_meetings` (FTS5 existente), `get_minutes`, `get_transcript`, read-only sobre la SQLite. Conexión con URI `mode=ro` + activar `journal_mode=WAL` y `busy_timeout` en la DB principal (hoy no está configurado: riesgo de "database is locked" con la app corriendo, objeción #4) | Delicada-frontera | Fable 5 `low→med` (rég. B: Opus 4.8 `high`) | Contrato público nuevo multi-módulo; diseño de superficie (qué se expone, paginación, límites). Debate adversarial de 1 ronda sobre el contrato ANTES de codear |
| 1.2 | ~~Jerarquía tipográfica/contraste~~ **HECHA y AMPLIADA (2026-07-03)**: absorbida por el rediseño completo del shell del dashboard (plan propio con debate adversarial: sidebar con etiquetas, vistas por hash, tokens+componentes, metric cards vía /api/stats, badges reales por fuente, command palette Ctrl+K, estados vacíos; absorbe también la 6.4). Commits 450fd77, 280afc0, 424eea6, 643b98c. Nota: /reunion (la página) sigue pendiente de la Ola 2 | — | — | Ejecutada inline por Fable con verificación en navegador real |
| 1.3 | **Highlight AltGr+H** (marca timestamp en vivo; los momentos salen destacados en el acta). CORRECCIÓN del debate (#2): el acta instantánea YA existe (`stop()` en core/meeting.py llama `generate_minutes` incondicionalmente); esta unidad es SOLO el highlight | Estándar | Sonnet 5 `med→high` | Radio contenido (core/hotkey.py, meeting.py, server.py) pero toca hotkeys globales (side cases: probar con IDE abierto, ARMING_DELAY) |

Tras el debate (objeción #5, aceptada): DOS kickoffs en vez de uno. 1a = solo el MCP (la
unidad delicada recibe atención completa, con su propio debate de contrato). 1b = 1.2 + 1.3
en paralelo (archivos disjuntos). Verificación antes de integrar en ambos (diff real +
prueba de flujo real).

### Kickoff Ola 1a — Servidor MCP (copiar y pegar en ventana nueva, plan mode)

```
Lee primero, en este orden:
1. CLAUDE.md del proyecto (arquitectura Vflow)
2. docs/PENDIENTES.md, secciones "Modo Reunión — estado vs Tactiq (jul 2026)" e
   "Investigación Tactiq + Fireflies (jul 2026) — arquitectura y oportunidad"
3. docs/PLAN-OLAS.md, Ola 1 y "Registro del debate adversarial" (al final del plan)
4. C:\OPS\.claude\skills\orquestar-agentes-fable\SKILL.md si eres Fable, o
   C:\OPS\.claude\skills\orquestar-agentes\SKILL.md si eres Opus

Auto-check: declara tu modelo (estampado: Fable 5 low→med hasta 07-07; Opus 4.8 high
después). Si eres más débil, avisa antes de proceder.

Tarea: SOLO la unidad 1.1 (servidor MCP local), 1 commit en windows-variant.
Crea/actualiza PROGRESS.md en la raíz (plantilla skill §8).

ANTES de codear: diseña el contrato de las 3 tools (search_meetings, get_minutes,
get_transcript: parámetros, forma de retorno, límites de tamaño) y sométela a UNA ronda
de debate adversarial (al adversario se le da SOLO la propuesta y la orden de destruirla,
nunca tu rationale). Registra el resultado en PROGRESS.md (Decisiones).

Restricciones ya decididas (no re-litigar):
- Read-only: conexión SQLite con URI mode=ro.
- Sub-paso previo: activar journal_mode=WAL y busy_timeout en la DB principal
  (db/database.py). Hoy NO está configurado y un lector concurrente puede dar
  "database is locked" mientras la app escribe (verificado en debate adversarial).
- stdio local, sin auth de red, reusar db/database.py y el FTS5 existente, respetar
  SAVE_HISTORY.

Verificación: con la app corriendo Y una reunión activa, validar las 3 tools desde un
cliente MCP real (sin locks, sin bloquear la captura). Máx 2 reintentos; al tercero
escala a Johann. No toques: core/, el flujo de dictado, la UI.
```

### Kickoff Ola 1b — Base visual + highlight

> **ACTUALIZACIÓN 2026-07-03:** la unidad 1.2 ya se ejecutó (rediseño completo del shell,
> ver fila 1.2). De este kickoff queda SOLO la unidad 1.3 (Highlight AltGr+H). Al lanzarlo,
> ignora las instrucciones de 1.2.

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md secciones "UX/UI — benchmarks Granola/Fathom/
superwhisper y plan de rediseño (jul 2026)" y "Panel en vivo: rediseño push→pull";
docs/PLAN-OLAS.md Ola 1; PROGRESS.md si existe; la skill orquestar-agentes(-fable) según
tu modelo.

Auto-check de modelo (1.2 estampada Sonnet 5 med; 1.3 Sonnet 5 med→high).

Tarea: unidades 1.2 y 1.3 en paralelo (archivos disjuntos), 2 commits en windows-variant.
Actualiza PROGRESS.md por hitos.

1.2 (CSS jerarquía): base 14px para contenido, white/85 texto principal, white/50 solo
metadatos, acentos Yo=violeta / Ellos=cian, badges por fuente (mic/system/url). Criterio
de hecho: before/after por screenshot de dashboard y /reunion. NO cambiar estructura HTML
ni JS, solo presentación.

1.3 (Highlight AltGr+H): marca timestamp durante reunión activa; los momentos marcados
salen destacados en el acta. OJO (verificado en debate adversarial): el acta YA se genera
sola en stop() (core/meeting.py llama generate_minutes); NO hay que implementarla. Side
cases de hotkeys: ARMING_DELAY, conflictos con atajos de IDE.

Verificación por flujo real (reunión de prueba corta con AltGr+R). No toques:
core/transcriber.py, core/backends/, el MCP.
```

---

## Ola 2 — Panel en vivo push→pull

**Objetivo:** implementar el rediseño aprobado (mockup de sesión 2026-07-03; detalle en
PENDIENTES.md "Panel en vivo: rediseño push→pull").

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 2.1 | UI /reunion modo "En vivo": pestañas En vivo / Preguntar, header con timer + VU por canal, barra de 4 acciones (Highlight, Nota, Pausar, Terminar), transcript con badges Yo/Ellos. AMPLIADA (2026-07-03): estados del pill diferenciados: en reunión el pill usa acento/anillo propio (≠ dictado) + timer mm:ss + actividad de AMBOS canales en el visualizador (hoy solo muestra "Yo": el usuario no puede confirmar que "Ellos" se está capturando); con AUDIO_SOURCE=system, matiz de color/glifo distinto; clic en el pill durante reunión abre /reunion | Estándar | Sonnet 5 `high` | Reorganización de UI existente + ui/pill_widget.py y main.py (estados ya centralizados en set_state y _apply_meeting_viz); verificable visualmente; el contrato de datos ya existe (polling /api/meeting, MEETING.viz_queue) |
| 2.2 | Push mínimo: SOLO pendientes detectados como tarjeta con ✓/✗ + caducidad (~3 min) + persistir el feedback ✓/✗ en DB (alimenta el bucle de mejora de prompts) | Estándar | Sonnet 5 `high` | Lógica acotada sobre el insight stream existente; el esquema de feedback es una columna/tabla nueva pequeña |
| 2.3 | Chat "Esta reunión" EN VIVO. CORRECCIÓN del debate (#3): requiere un `build_context` ALTERNO sobre estado en RAM (`MEETING._segments` + rolling state); el actual solo lee la DB y la reunión activa NO está ahí hasta `stop()`. Es un contrato nuevo, no un toggle | Delicada (contrato con core/assistant.py e insights.py) | Opus 4.8 `high` (rég. A: Fable `low`) | Side cases: reunión sin acta aún, transcript parcial, estado en RAM mutando mientras se responde |

Orden: 2.1 primero (define dónde vive todo), luego 2.2 y 2.3 en paralelo (archivos distintos:
UI ya estable, 2.2 en insights/DB, 2.3 en assistant).

### Kickoff Ola 2

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md sección "Panel en vivo: rediseño push→pull"
(la decisión de diseño con el porqué); docs/PLAN-OLAS.md Ola 2; PROGRESS.md si existe;
la skill orquestar-agentes(-fable) según tu modelo.

Auto-check de modelo (estampado: Opus 4.8 high o Fable 5 medium para dirigir; unidades
según tabla de la ola).

Tarea: implementar el panel en vivo push→pull en /reunion (web/server.py) en 3 unidades
= 3 commits. Actualiza PROGRESS.md por hitos.

Principios NO negociables del diseño (ya debatidos con Johann):
- En vivo la IA es PULL; el ÚNICO push son pendientes detectados (tarjeta ✓/✗ con
  caducidad ~3 min). Temas/propuestas dejan de mostrarse en vivo: van al acta.
- El motor de insights NO se toca: sigue corriendo igual; cambia solo QUÉ se muestra.
- Chips del chat en vivo: "¿Puntos clave hasta ahora?", "¿Qué me falta preguntar?",
  "Pendientes y responsables".
- El feedback ✓/✗ se persiste (es insumo del bucle de mejora de prompts de insights.py).
- 2.3 exige un build_context ALTERNO sobre estado en RAM (MEETING._segments + rolling
  state): el build_context actual de core/assistant.py solo lee la DB y NO sirve para la
  reunión activa (verificado en debate adversarial; no lo descubras a mitad de sesión).

Verificación: reunión de prueba real (AltGr+R, hablar 2-3 min con audio de sistema
sonando) y validar: tabs, pendiente aparece y caduca, ✓/✗ queda en DB, chat responde
sobre lo dicho. No toques el flujo de dictado ni los backends.
```

---

## Ola 3 — Conversation intelligence Yo/Ellos

**Objetivo:** el panel de métricas que Fireflies cobra en Business, local y gratis.
Algoritmo ya bocetado en PENDIENTES.md ("Oportunidad nueva ⭐").

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 3.1 | Motor de métricas EN VIVO por ventana: en cada flush (core/meeting.py), correr `get_speech_timestamps` de silero sobre el audio de la ventana POR CANAL y acumular segmentos con offset → talk-time % Yo/Ellos, talk-to-listen, monólogo más largo, turnos e interrupciones (solape), preguntas por canal; persistir JSON de métricas con la reunión. CORRECCIÓN del debate (#1, #9): los buffers se DESCARTAN tras cada ventana (no existe audio completo post-reunión), por eso el VAD corre en el flush, NO post-hoc; y `apply_vad` de core/vad.py NO sirve (devuelve audio concatenado sin timestamps) | Delicada-frontera | Fable 5 `low→med` (rég. B: Opus 4.8 `high`) | Side cases: merge de gaps, umbrales, solapes, offsets acumulados entre ventanas, volúmenes dispares por canal; definir la verificación ANTES (fixture con proporciones conocidas) |
| 3.2 | UI: dona Yo/Ellos + métricas en la tarjeta de reunión del historial y tab "Stats" del detalle | Mecánica | Sonnet 5 `med` | Cablear datos ya persistidos a UI; verificable visual |

Secuencial: 3.1 define el contrato (JSON de métricas en DB), 3.2 lo consume.

Nota del debate: persistir WAV por canal quedó como opción futura opt-in (habilitaría
soundbites con audio, backlog Fireflies); NO es prerequisito de esta ola gracias al VAD
en el flush. Decisión de disco/retención pendiente si algún día se activa.

### Kickoff Ola 3

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md secciones "Oportunidad nueva ⭐ Panel de
conversation intelligence" (algoritmo bocetado) e "Investigación Tactiq + Fireflies"
(qué métricas ofrece Fireflies y cómo las define: monólogo = ≥90s ininterrumpido,
WPM sano 140-160); docs/PLAN-OLAS.md Ola 3; PROGRESS.md.

Auto-check de modelo (3.1 estampada Fable low→med / Opus high; 3.2 Sonnet med).

Tarea: 2 unidades = 2 commits. El contrato va primero: define el JSON de métricas
(campos, unidades) y persístelo con la reunión ANTES de tocar UI.

Restricciones (corregidas tras debate adversarial, no las re-descubras):
- Los buffers por canal se DESCARTAN tras cada ventana (core/meeting.py, _flush_window):
  NO existe audio completo al terminar la reunión. El VAD corre EN el flush de cada
  ventana y los segmentos se acumulan con el offset de esa ventana.
- Usa get_speech_timestamps de silero directamente (la librería ya está en el stack);
  apply_vad de core/vad.py NO sirve: devuelve audio concatenado sin timestamps.
- Fusiona gaps <0.3s, descarta segmentos <0.2s (constantes configurables). Interrupción =
  segmentos solapados entre canales.

Verificación de 3.1 (OBLIGATORIA antes de UI): crea un fixture de prueba (dos WAVs
sintéticos o grabados con proporciones conocidas de habla) y un test que valide
talk-time ±5%. Sin ese test la unidad NO se integra.
```

---

## Ola 4 — Patrón Granola (notas + IA)

**Objetivo:** "mis notas en vivo + la IA las mejora", trazabilidad y plantillas.
Contexto en PENDIENTES.md sección "UX/UI benchmarks" (patrones 1, 4, 5).

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 4.1 | Campo "mis notas" en el modo En vivo (ya previsto en Ola 2) + fusión post-reunión: prompt que integra notas del usuario + transcript en el acta, destacando lo humano | Delicada (diseño de prompt) | Opus 4.8 `high` (rég. A: Fable `low`) | La calidad del acta fusionada ES el feature; iterar el prompt con transcripts reales de la DB (bucle de mejora ya establecido en insights.py) |
| 4.2 | Lupa de trazabilidad: cada bullet del acta enlaza al segmento del transcript que lo originó (ya hay timestamps por segmento y flash de segmento en /reunion) | Estándar | Sonnet 5 `high` | Requiere que generate_minutes devuelva referencias de segmento (cambio de contrato pequeño en el prompt + UI) |
| 4.3 | Plantillas por tipo de reunión (general, ventas/BANT, 1:1, clase): selector al iniciar; la plantilla moldea el system prompt del acta y los chips del chat | Estándar | Sonnet 5 `high` con prompts revisados por el orquestador | Estructura simple (tabla de plantillas), pero los prompts por tipo los valida el L0/L1 |

### Kickoff Ola 4

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md sección "UX/UI benchmarks Granola/Fathom"
(patrón "tus notas + IA las mejora": las notas del usuario quedan DESTACADAS en el acta
final, la IA añade contexto; ese es el contrato de producto); docs/PLAN-OLAS.md Ola 4;
PROGRESS.md; core/insights.py (generate_minutes y el bucle de mejora de prompts).

Auto-check de modelo (4.1 estampada Opus high / Fable low; 4.2-4.3 Sonnet high).

Tarea: 3 unidades = 3 commits. 4.1 define el contrato del acta fusionada; 4.2 y 4.3
después (4.2 depende del formato de referencias que fije 4.1).

Para 4.1: valida el prompt contra transcripts REALES de la DB (tabla meetings) antes de
integrarlo: regenera actas de reuniones pasadas con notas simuladas y compara. Preferir
CALLAR a inventar (regla ya establecida en los prompts de insights.py).

Verificación: reunión de prueba con 3-4 notas manuales; el acta debe distinguir
visualmente lo que escribió el humano de lo que añadió la IA.
```

---

## Ola 5 — Proactivo v2 (el derecho a interrumpir)

**Objetivo:** las detecciones de PENDIENTES.md "Panel proactivo v2". La ola de mayor
incertidumbre: prompts/heurísticas que se calibran con reuniones reales. Presupuesto de
atención: máx ~1 push cada 5 min salvo pendientes.

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 5.1 | Pregunta sin responder (asimetría de canales) + compromiso adquirido + acuerdo vago, como clases de tarjeta push con caducidad | Delicada-frontera | Fable 5 `med→high` (rég. B: Opus `xhigh`; escalar a Fable API si falla el circuit breaker) | Es la unidad más difícil del plan (objeción #7: re-ruteada al techo del régimen). Heurísticas + prompts con side cases finos; el costo del error es ruido que mata el feature (la lección de la v1) |
| 5.2 | Memoria cruzada en vivo: FTS del rolling state contra actas pasadas → tarjeta "el 12/6 se acordó X" | Delicada | Opus 4.8 `high` (rég. A: Fable `low`) | Reusar core/assistant.py (FTS + retrieval); cuidar falsos positivos con umbral de score |
| 5.3 | Timing y superficies: sugerencias solo en lulls (VAD de Ola 3), badge en el pill (1 palabra), **HUD flotante** (ventana Qt nativa frameless always-on-top que se despliega desde el pill o con AltGr+A: tarjetas de pendientes ✓/✗, confirmación de highlight, botón "me perdí" AltGr+M, mini-input de pregunta a la IA; dos estados de foco: reposo sin foco / input con foco deliberado y Esc lo devuelve), modos Silencioso/Copiloto/Entrenador | Estándar | Sonnet 5 `high` | Cablea patrones ya probados en el código: flags de ventana del pill (pill_widget.py), restauración de foco (clipboard.py), señales Qt QueuedConnection. NO usar QtWebEngine (+150 MB de bundle por 3 tarjetas): widgets nativos. Alimentación en-proceso desde MEETING, sin HTTP |

### Kickoff Ola 5

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md sección "Panel proactivo v2: qué gana el
derecho a interrumpir" (principio, las 6 detecciones, timing, lo que NO hacer);
docs/PLAN-OLAS.md Ola 5; PROGRESS.md; core/insights.py y core/meeting.py.

Auto-check de modelo (5.1 estampada Fable med→high / Opus xhigh: es la unidad más
difícil de todo el plan; 5.2 Opus high; 5.3 Sonnet high).

Tarea: 3 unidades = 3 commits, en orden 5.3 → 5.1 → 5.2 (la infraestructura de
timing/superficies primero para que las detecciones nazcan con el gating puesto).

Principio NO negociable: la barra es "esto cambia lo que el humano hará en los próximos
2 minutos". Toda detección nueva nace con: caducidad, respeto del presupuesto de atención
(~1 push/5 min), y gating por modo (Silencioso/Copiloto/Entrenador).

Calibración (parte de la definición de hecho de 5.1 y 5.2): correr contra ≥3 transcripts
reales de la DB y reportar precisión percibida; los prompts se endurecen con el bucle de
mejora existente. Si una detección no alcanza señal/ruido razonable, se DESACTIVA por
default y se deja tras flag (mejor que ruido: lección de la v1).
```

---

## Ola 6 — Plataforma y dictado

**Objetivo:** cerrar el círculo con el OPS y subir el lado dictado (el otro 50% de la app).

| Unidad | Qué | Dificultad | Ejecutar con | Por qué |
|---|---|---|---|---|
| 6.1 | Webhook genérico saliente al generar acta (POST JSON configurable, patrón Fireflies con firma HMAC) + oferta "pendientes → tareas OPS" (markdown/dead-drop). Manejar `meeting_id=None` (objeción #10: `stop()` puede fallar al persistir y el acta queda solo en RAM): el webhook solo dispara con reunión persistida | Estándar | Opus 4.8 `high` | Sale contenido del equipo local: superficie de seguridad pequeña pero real (URL config, firma, opt-in) |
| 6.2 | Dictado: toggle "ver crudo / Undo AI edit" por transcripción + diccionario que sugiere entrada al detectar corrección manual (source='suggested', bandeja de revisión: NUNCA auto-aplicar) | Estándar | Sonnet 5 `high` | El esquema ya lo prevé (diccionario v2 en PENDIENTES); radio contenido |
| 6.3 | Modos de dictado por app activa: 3 presets (email formal / chat casual / código), reformateo LLM post-dictado opt-in | Estándar | Sonnet 5 `high`, prompts revisados por L0 | Lección superwhisper: 3 presets sensatos, CERO builder de modos |
| 6.4 | ~~Navegación lateral + command palette + estados vacíos~~ **HECHA (2026-07-03)**: absorbida por el rediseño del shell (ver 1.2). La Ola 6 queda con 6.1-6.3 | — | — | — |

### Kickoff Ola 6

```
Lee primero: CLAUDE.md; docs/PENDIENTES.md (secciones Tactiq/Fireflies para el webhook,
"Diccionario v2", "UX/UI benchmarks" patrones 6-9); docs/PLAN-OLAS.md Ola 6; PROGRESS.md.

Auto-check de modelo (6.1 Opus high; resto Sonnet).

Tarea: 4 unidades = 4 commits, independientes (paralelizables por pares: 6.1+6.2,
6.3+6.4, archivos disjuntos).

Restricciones: 6.1 es opt-in y apagado por default (regla de privacidad del proyecto:
en local nada sale a internet sin decisión explícita); la URL del webhook se guarda como
config, el secreto de firma cifrado DPAPI como la API key. 6.2: sugerir NUNCA auto-aplicar.
6.3: exactamente 3 presets, sin UI de creación de modos. Verificación por flujo real.
```

---

## Hotfix fuera de olas (no espera al plan)

> **HECHO (2026-07-03, sesión Fable):** implementado en core/hotkey.py y verificado con
> 5 casos simulados (script de eventos sintéticos): acordes no arrancan ni cortan, taps
> limpios sí, modo 1 sin regresión. Pendiente solo la prueba manual de Johann en uso real.

**Shift-stop por acorde (bug reportado 2026-07-03):** en manos libres (modo 2), CUALQUIER
pulsación de Shift detiene la grabación (core/hotkey.py:197-201), incluidos los acordes
(Shift+letra para mayúscula, Shift+Enter): el dictado se corta a mitad de frase. El mismo
contador ingenuo puede además ARRANCAR grabación por triple-tap falso al teclear 3
mayúsculas rápidas. Fix: calificar el tap como "tap limpio": una pulsación de Shift solo
cuenta (para stop Y para el triple-tap) si ninguna otra tecla se presiona mientras Shift
está abajo; la acción se decide al SOLTAR Shift (retraso imperceptible). Radio: solo
core/hotkey.py. Dificultad: Estándar (side cases de teclado). Ejecutar con: Sonnet 5 `high`
o inline. Verificación: dictar manos libres y (a) escribir Shift+A sin que corte, (b) tap
limpio de Shift corta, (c) teclear "Ana María Pérez" rápido sin que arranque grabación.

## Reglas transversales (aplican a TODAS las olas)

1. Branch de trabajo: `windows-variant`. 1 unidad = 1 commit revisable y revertible.
2. `PROGRESS.md` único en la raíz, actualizado por hitos (plantilla skill §8). Cada kickoff
   lo lee al arrancar y lo deja al día al cerrar.
3. Verificación antes de integrar SIEMPRE; si no hay test automático, definirlo antes de
   delegar (§5). Máx 2 reintentos, luego escalar a Johann.
4. Nada nuevo sale a internet por default; secretos cifrados DPAPI; nunca leer .env.
5. Dependencias nuevas: política de 30 días en PyPI + regenerar requirements.lock.
6. Al cerrar cada ola: actualizar CLAUDE.md del proyecto (qué existe ahora), PENDIENTES.md
   (mover lo hecho a "ya implementado") y guardar resumen en Engram.

---

## Registro del debate adversarial (2026-07-03, protocolo §10.3 de orquestar-agentes-fable)

Fable propuso este plan; Opus 4.8 lo atacó con acceso al código real. Veredicto de Opus:
APROBAR CON CAMBIOS. Respuesta de Fable a cada objeción (ninguna ignorada):

| # | Objeción (resumen) | Severidad | Respuesta | Acción aplicada |
|---|---|---|---|---|
| 1 | Ola 3 inviable: los buffers de audio por canal se descartan en cada ventana; no hay insumo para VAD post-hoc | alta | ACEPTADA | Algoritmo reescrito: VAD en el flush de cada ventana (get_speech_timestamps por canal, acumulando con offset), sin persistir audio |
| 2 | El "acta instantánea" ya existe (stop() ya llama generate_minutes) | alta | ACEPTADA | Unidad 1.3 reducida a solo AltGr+H; estampa rebajada a Sonnet med→high |
| 3 | Chat en vivo (2.3) no puede reusar build_context (solo lee DB; la reunión activa vive en RAM) | alta | ACEPTADA | Unidad y kickoff reescritos: build_context alterno sobre MEETING._segments + rolling state, declarado como contrato nuevo |
| 4 | Concurrencia SQLite MCP vs app (sin WAL ni busy_timeout) | media | ACEPTADA | 1.1 ahora exige mode=ro + WAL + busy_timeout como sub-paso previo |
| 5 | MCP + CSS + highlights en un solo kickoff diluye la atención de la pieza delicada | media | ACEPTADA | Ola 1 partida en kickoffs 1a (MCP solo) y 1b (visual) |
| 6 | Presión artificial "arrancar esta semana" por la ventana Fable compromete el diseño del contrato | media | MITIGADA | La ventana se usa para sesiones de plan/diseño (donde Fable rinde más), no para apurar ejecución; el MCP toma el tiempo que necesite. Si la ventana cierra, Opus xhigh diseña (régimen B) |
| 7 | 5.1 (la unidad más difícil) sub-ruteada a Fable med | media | ACEPTADA | Re-estampada Fable med→high / Opus xhigh + escalada a Fable API tras circuit breaker |
| 8 | Kickoffs citan títulos de sección inexactos (falla el grep en frío) | baja | ACEPTADA | Títulos corregidos a los exactos de PENDIENTES.md |
| 9 | apply_vad no devuelve timestamps (devuelve audio concatenado); "reusar core/vad.py" era engañoso | media | ACEPTADA | Kickoff Ola 3 corregido: usar get_speech_timestamps directamente |
| 10 | meeting_id puede ser None si stop() falla al persistir; webhook/export sin manejar ese caso | baja | ACEPTADA | 6.1 dispara solo con reunión persistida (caso None documentado) |

Reconciliación: 9 aceptadas, 1 mitigada, 0 refutadas. Lección registrada: el plan v1
planificaba tres unidades sobre supuestos de la investigación de mercado sin verificar
contra el código (el patrón de fallo que el debate §10.2 existe para atrapar). El plan
queda APROBADO con estos cambios aplicados.

Enmienda post-debate (2026-07-03, Johann): la unidad 5.3 se amplió con el HUD flotante
(ventana Qt nativa desplegable desde el pill). Cambio de UI reversible dentro de una
unidad ya existente: no re-gatilla debate (proporcionalidad §0/§10.2); el kickoff de la
Ola 5 lo hereda tal cual.
