# PROGRESS — Plan por olas Vflow   (branch: windows-variant | últ. checkpoint: 2026-07-12)

## Objetivo / contexto
- Ejecutar las olas de docs/PLAN-OLAS.md. Este archivo es el handoff reanudable (plantilla orquestar-agentes §8).
- Kickoff activo: **ORQUESTADOR AUTÓNOMO** (PLAN-OLAS "Modo autónomo"), director **Opus 4.8** (régimen A: auto-check pasa, Opus es el piso; nota de costo dada a Johann). **Olas 1-7 COMPLETAS (todo el plan ejecutable agotado).** Solo quedan gates humanos G2/G3 en "PARA JOHANN" (pruebas físicas y gusto visual, async).

---

# PLAN-MEJORAS-2026-07-06 (activo)

> Plan nuevo: `PLAN-MEJORAS-2026-07-06.md` (derivado de `AUDITORIA-FABLE-2026-07-06.md`). Kickoff:
> Orquestador Autónomo. Director de este run: **Fable 5** (régimen A vigente al arranque 2026-07-12
> mañana; auto-check pasa). Skill aplicada: `orquestar-agentes-fable` (§10: Fable dirige, Opus 4.8
> ataca cada ola antes de ejecutarla). Misión: Olas 0→1→2→3 sin intervención; Olas 4/5/6 gateadas
> por decisiones D1/D4/D5 (SIN responder al arranque — ver PARA JOHANN abajo). Política A: commits
> locales, sin push. 1 unidad = 1 commit.

## PARA JOHANN (PLAN-MEJORAS)
- **🙋 DECISIÓN 5.6/5.7 — Dos features nuevas de la investigación de mercado, especificadas en el
  plan pero que NO elegiste en D4** (por eso no corren solas): **5.6** = tus notas rápidas de reunión
  se expanden solas con lo dicho en el transcript al cerrar (estilo Granola; tu texto literal nunca
  se pierde). **5.7** = panel "Privacidad" que muestra qué datos salieron de tu máquina y cuándo
  (con backend local muestra cero egresos — el argumento de venta).
  - **A (recomendada)**: ambas → se ejecutan como cola de la Ola 5 en ventana nueva (validadas por
    el producto de culto de la categoría + pitch de privacidad; costo medio).
  - **B**: solo una (di cuál).
  - **C**: ninguna por ahora → quedan en backlog, nada se pierde.
  Responde "5.6/5.7: A/B(cuál)/C".
- **G2 Ola 5 — Prueba física de las 5 features nuevas** (10 min, con la app reiniciada tras
  estos commits): (1) Ajustes → Identidad: pon tu nombre, cierra una reunión corta con un
  compromiso tuyo → el pendiente del acta debería decir tu nombre, no "Yo". (2) Ajustes →
  carpeta de pendientes = `C:\OPS\_inbox-vflow\` → cierra una reunión CON pendientes → aparece
  `vflow-pendientes-...md` (YAML arriba, checklist abajo); una reunión SIN pendientes no debe
  crear archivo. (3) En una reunión real, NO pulses AltGr+H: al abrirla luego en /reunion,
  la sección "⭐ Momentos" puede traer entradas con badge "auto" — juzga si te parecen momentos
  reales (si molestan: Ajustes/env `AUTO_HIGHLIGHTS_ENABLED=false`); el acta (momentos
  destacados) debe seguir mostrando SOLO los tuyos manuales. (4) /reunion → historial →
  selecciona 2-3 reuniones → "Chatear con N reuniones" → prueba el chip "Email de seguimiento"
  → Copiar y "Exportar a OPS" (debe caer en `_inbox-vflow\entregables\`). (5) Con backend groq:
  Ajustes → activa "Respaldo local sin internet" (requiere modelo local descargado), corta el
  wifi y dicta → debería pegar texto igual (aviso "transcribiendo con el modelo local") y al
  volver el wifi, el siguiente dictado (tras ~2 min) vuelve solo a Groq. G3 visual: mirada
  rápida a la sección Identidad, "⭐ Momentos" y el overlay del chat multi (los screenshots de
  la verificación automática no estuvieron disponibles en este entorno; el estado computado sí
  se verificó).
- **🙋 DECISIÓN 4.4 — ¿Reordenamos también core/ por features?** (la web ya quedó partida en
  la Ola 4; core/ sigue siendo archivos sueltos: meeting.py, insights.py, transcriber.py...).
  Reordenarlo deja fronteras limpias para la capa de agentes (4.5) y el crecimiento a móvil,
  pero toca MUCHOS archivos a la vez (riesgo alto, se haría como ola propia con su debate).
  - **A (recomendada)**: sí, como ola nueva DESPUÉS de las Olas 5 y 6 → primero valor de
    producto, el reorden profundo al final con todo verde.
  - **B**: sí, ANTES de la Ola 5 → fronteras limpias ya, pero retrasa las features elegidas.
  - **C**: no por ahora → 4.5 se implementa igual (el contrato ya existe), solo que sobre la
    estructura actual de core/.
  Responde "4.4: A/B/C".
- **G2 Ola 4 — Prueba física del reorden web** (3 min, con la app reiniciada tras estos
  commits): (1) abre el dashboard → todo debe verse y funcionar EXACTAMENTE igual que antes
  (la UI servida es byte-idéntica por diseño; si notas CUALQUIER diferencia visual, repórtala:
  sería un bug del reorden); (2) /reunion igual; (3) cambia un ajuste en Ajustes (p.ej. fuente
  de audio) y verifica que aplica sin reiniciar (los flags en caliente siguen vivos — hay test,
  pero el ojo real confirma). G3 visual: MOOT (cero cambio de bytes en la UI).
- **G2 Ola 3 (nueva) — Prueba física del polling incremental + retención** (5-10 min, con la app
  reiniciada tras estos commits): (1) inicia una reunión real (AltGr+R) y ten abierto /reunion un
  rato largo → el transcript debe crecer con fluidez y sin saltos/duplicados (por dentro ahora
  solo viajan los segmentos nuevos por poll, ~97% menos datos); (2) termina esa reunión y arranca
  OTRA sin recargar la página → el transcript debe LIMPIARSE solo y mostrar únicamente la reunión
  nueva (es el caso que el diseño protege con el token de generación); (3) el panel embebido del
  dashboard debe seguir comportándose igual que siempre; (4) retención: NO actives
  MEETING_RETENTION_DAYS si quieres conservar todo (default 0 = para siempre); si algún día lo
  activas en Ajustes → Reuniones, recuerda que borra actas y transcripts definitivamente y aplica
  al reiniciar la app.
- **G2 Ola 0 — VERIFICADO 2026-07-12** (Johann + orquestador con la app viva): (1) re-pulsar AltGr+R
  durante el guardado → notificación "Guardando la reunión anterior…" + ninguna reunión rota ✓;
  (2) AltGr+R/T sostenido → sin ráfaga de beeps (confirmado por log: ciclos deliberados, no
  auto-repeat) ✓; (3) POST /api/meeting/start durante el stop → HTTP 409 con mensaje claro
  (log 08:53:34, probe automático) ✓. FALTA solo (4) cola URL con crash → la corre el orquestador
  (cierra/reabre la app).
- **G2 Ola 1 — VERIFICADO 2026-07-12**: (1) validación de rutas en /api/settings → C:\Windows,
  ruta relativa y briefing sin .md rechazados con 400 y mensaje claro (probe contra app viva) ✓;
  (2) TTL/borrado del WAV fallido → fallo real sin red escribió last_failed_recording.wav
  (log 09:04:10), dictado exitoso posterior lo borró (log 09:10:53 "eliminado tras dictado
  exitoso") ✓; hotkeys y dictado corto sin anomalías ✓. Pendiente async opcional: (3) ver
  tarjetas proactivas ✓/✗ en una reunión real de trabajo (no bloquea).
- **NUEVA UNIDAD pedida por Johann (2026-07-12): fallback simétrico de transcripción online↔local.**
  Hoy `GROQ_FALLBACK` cubre SOLO local-primario→Groq (transcriber.py:177,308-313). Falta el espejo:
  con backend primario = Groq (online), si no hay internet la transcripción cae AUTOMÁTICAMENTE al
  modelo local, y en el siguiente dictado vuelve a intentar Groq primero (re-probe por evento, patrón
  del circuit breaker de insights.py `_breaker`). Cero costo en tokens (Whisper local CTranslate2).
  Condición de diseño a resolver en el debate: el modelo local DEBE estar descargado para el fallback
  Groq→local; si no lo está, avisar en vez de fallar mudo (no auto-descargar en el hot-path). Decisión
  de Johann: **AGREGADA a la Ola 5 (producto) como unidad 5.5** (ver abajo). Esfuerzo bajo-medio,
  simétrico a la lógica ya existente.
- **Decisiones RESPONDIDAS por Johann (2026-07-12): D1:B · D4: las 4 · D5: ambas.**
  - **D1:B → Ola 4 alcance COMPLETO** (4.1 extraer frontend + 4.2 blueprints Flask + 4.3 catálogo
    config + abrir debate 4.4 encarpetar core/ por features). Contexto ampliado (NO es solo "vender en
    6-12m"): Johann quiere Vflow como pieza de un MACROSISTEMA vinculado al cerebro de OPS, con roadmap
    tipo Plaud.ai — (1) refinar la versión de escritorio (ahora), (2) app móvil, (3) hardware grabador
    dedicado (ODM China) como último paso. El foso NO es el hardware (commodity) sino la integración
    con OPS. Visión completa guardada en memoria privada (engram), NO en este repo (puede ser
    compartido; estrategia de negocio/monetización va a lo privado). Implicación para Ola 4: el reorden
    feature-first ahora tiene justificación fuerte (va a crecer a móvil + integrarse), no es limpieza
    cosmética → priorizar la partición limpia.
  - **D4: las 4 → Ola 5 corre 5.1 + 5.2 + 5.3 + 5.4** (+ 5.5 fallback ya elegida). Orden por ROI/
    esfuerzo del plan: 5.2 (contexto personal, mínima) → 5.4 (pendientes→OPS, GATE G1 contrato) →
    5.1 (auto-highlights) → 5.3 (chat cross-reunión) → 5.5 (fallback). OJO 5.4 es la PRIMERA prueba
    real de la tesis del macrosistema (Vflow como sensor del OPS): tratarla como validación, no solo
    feature.
  - **D5: ambas → Ola 6 corre 6.1 (mute al dictar, pycaw) + 6.2 (hotkeys configurables)**, con
    re-validación del spec desactualizado como primera tarea (regla dura de la ola).
  - **Investigación estratégica EJECUTADA (2026-07-12, alcance A/acotada):** informe completo en
    `C:\OPS\_VelOS\cerebro-investigacion\inbox\2026-07-12_investigacion-vflow-mercado-captura-ia.md`
    (pendiente ingesta formal por cerebro-manager; anotado en _LOG de la biblioteca). Conclusiones
    que afectan el plan: hueco vacante = local-first + sin taxímetro de minutos + combo dictado/
    reuniones/memoria personal; MCP es table stakes (Plaud/Granola/Fireflies/Otter ya lo tienen) →
    el foso se reformula como SER el cerebro (contexto acumulado de OPS), no la conexión; hardware
    propio aún más "diamante" de lo pensado (wearables consolidados: Limitless→Meta, Bee→Amazon);
    5.4 = experimento de validación de toda la tesis; riesgo a vigilar: Wispr Flow (~$2B, master
    plan second-brain) y Plaud Team convergiendo al mismo terreno. El **Apéndice C** del informe
    lista qué copiar/mejorar que NO está en ruta (notas híbridas estilo Granola, panel de
    privacidad verificable, CLI junto al MCP de 4.5, sync a Obsidian/Notion del cliente, pricing
    sin minutos + opción lifetime; y 2 ajustes de diseño: el contrato de 4.5 como "paquete de
    contexto consolidado" y el cierre de 5.3 como entregable accionado) — **TAREA para el
    orquestador al llegar a las Olas 4/5**: transferir esos ítems a docs/PENDIENTES.md o a
    unidades, y llevar el Apéndice C al debate de diseño de 4.0/4.5. D2/D3/D6/D7/D8 sin responder
    (no bloquean el run).

## En curso (PLAN-DICTADO, plan NUEVO del 2026-07-31)
- [ ] **Plan nuevo y paralelo al PLAN-MEJORAS**: `docs/PLAN-DICTADO-2026-07-31.md` (commit 9bc8c99).
  8 olas, nacidas de auditar el upstream `daniel-carreon/sflow` desde OPS. Debate adversarial ya
  hecho y reconciliado dentro del propio documento (APROBAR CON CAMBIOS, 4 objeciones ALTAS).
  - **Next action:** ventana nueva con `Lee docs/PLAN-DICTADO-2026-07-31.md y ejecuta el Kickoff
    Ola 0.` La Ola 0 va primero siempre (fija el orden canónico de las pasadas de texto; sin ella
    las Olas 1 y 4 se pisan). Modelo: `Fable.H` u `Opus.H`, indistinto. Después de la 0, ejecutables
    sin gate: Ola 1, luego Ola 4 (depende de `1b`, NO en paralelo), y Olas 2 y 7 cuando se quiera.
  - **Bloqueadas por gate humano:** Olas 3 y 5 esperan **G1** (cómo se copia Transform sobre
    selección: previsualizar, lista negra, o no hacerlo). Ola 6 espera **G2** (Hub nativo Qt vs
    `QWebEngineView` vs dejarlo en el navegador). Los dos gates están escritos con opciones dentro
    del plan; ninguno bloquea las otras cinco olas.
  - Los 3 commits de este frente (`f3faf67`, `0eb1264`, `9bc8c99`) están LOCALES, **sin push**.

## En curso (PLAN-MEJORAS)
- [ ] Ola 6 — FASE3 heredada (D5: ambas → 6.1 mute al dictar + 6.2 hotkeys configurables).
  PENDIENTE de ventana nueva.
  - Next action: **ventana nueva con el Kickoff Ola 6** (o el Orquestador Autónomo, que la
    retoma). Régimen: dirige Fable (permanente en la suscripción) u Opus, indistinto.
    Primera tarea DURA de esa ventana: re-validar docs/FASE3_SPEC.md contra el código actual
    (el spec está desactualizado a sabiendas) y registrar la reconciliación aquí; el debate
    adversarial ataca ESA reconciliación, no el spec original. 6.1 exige pycaw (política de
    dependencias: 30 días en PyPI + requirements.in + lock regenerado con hashes). Tras Ola 6:
    si Johann responde 5.6/5.7 (pregunta en PARA JOHANN) se abre su cola como unidades nuevas;
    4.4 (encarpetar core/) y 4.5 (superficie agéntica MCP+CLI, contrato ya aprobado) esperan
    la decisión 4.4 de Johann.

## Completado (PLAN-MEJORAS, cont.)
- [x] **Token local de sesion para el dashboard** (2026-07-31, director @fable-5 desde OPS,
  ejecuto @opus-5, verifico @sonnet-5 read-only; commit f3faf67; suite 766 -> 794 pass)
  - Origen: NO salio del backlog. Salio de auditar `daniel-carreon/sflow` (el upstream de este
    fork) en OPS ese mismo dia. De las 5 fallas que se le encontraron, esta era la unica
    heredada aca: `_csrf_check` exime `GET/HEAD/OPTIONS` a proposito, asi que las LECTURAS de
    `/api/transcriptions` y `/api/meetings` estaban abiertas a cualquier proceso local. Pesa mas
    que en el repo original porque alli exponia dictados sueltos y aca expone transcripts y
    actas de reuniones con otras personas. Informe:
    `C:\OPS\repositorios-terceros\auditorias\sflow-AUDITORIA.md`.
  - Que: `core/localauth.py` (token de 32 bytes en `dashboard_token.txt` dentro de `APP_DATA_DIR`,
    cache bajo lock con la I/O fuera del lock, `compare_digest`) + `_auth_check` en `web/state.py`
    registrado despues de `_csrf_check`. `/static/*` exento; `/` y `/reunion` canjean `?t=` por
    cookie HttpOnly SameSite=Strict y redirigen sin query string; el resto exige cookie o
    cabecera `X-Vflow-Token`. `DASHBOARD_AUTH_ENABLED` en `ENV_CATALOG` (default `true`, lazy).
  - Dos divergencias DELIBERADAS de la convencion del repo, ambas por el mismo motivo (aqui el
    lado seguro del fallo es el contrario al habitual): es **fail-CLOSED** al reves que
    `ops_briefing.py` (si el token no se puede leer ni crear, se DENIEGA), y el killswitch apaga
    **solo** con el literal `false`, no con el `== "true"` de siempre, para que basura en el
    `.env` deje la proteccion encendida en vez de apagarla.
  - Limite conocido y ACEPTADO, no es deuda: un proceso corriendo como el mismo usuario de
    Windows tambien puede leer el archivo del token o la SQLite directamente. Esto sube el liston
    de "curl trivial" a "hay que leer un archivo". Johann lo decidio con ese tradeoff a la vista.
  - MCP no afectado: `mcp_server/` lee SQLite en `mode=ro` por stdio, nunca por HTTP (verificado).
  - Verificado: 794 passed (766 previos intactos + 28 nuevos). La suite corre con el guard
    APAGADO desde `tests/conftest.py` (29 archivos usan el `test_client` sin token) y
    `test_dashboard_auth.py` lo reenciende con monkeypatch function-scoped. Verificacion
    INDEPENDIENTE read-only con lente de bypass: las 51 rutas vivas de los 8 blueprints
    golpeadas sin credenciales (401 en las 51), path traversal contra `/static` con socket crudo
    contra el servidor real y no solo el test client, fuzz de 14 valores del killswitch, y
    mutacion de `is_enabled`/`verify` para probar que los tests nuevos DETECTAN un guard roto en
    vez de pasar por casualidad. PASS sin hallazgos.
- [x] **OLA 5 COMPLETA** (2026-07-12 tarde, ventana 3, director @fable-5, debate @opus-4.8,
  ejecutores 5×@sonnet-5; 6 commits, suite 636 → 766 pass / 0 fail). Debate adversarial de la
  ola previo (APROBAR CON CAMBIOS, 16 objeciones, todas reconciliadas — ver Decisiones).
  Unidades (orden ejecutado 5.2 → 5.4 → 5.1 → 5.3 → 5.5):
  - 5.2 contexto personal (953b545): USER_NAME/ROLE/DOMAIN lazy + user_identity_line() con
    coletilla anti-atribución; gate silent en vivo (mismo criterio que el briefing); UI
    "Identidad" en Ajustes con nota de privacidad. +15 tests.
  - 5.4 pendientes → OPS (7de1fe7): contrato de tarea v1 (CONTRATO-MACROSISTEMA Parte A) —
    YAML schema_version 1, gate ≥1 pendiente (O7), create-only con instalacion+hash8 (O3),
    machine_id persistido, guard SAVE_HISTORY pineado. +14 tests (3 viejos reemplazados).
  - 5.1 auto-highlights (e692433): momentos_out en update_state (cero LLM extra), max_tokens
    live 1200→1800 + guardián de no-truncado, source auto/manual en highlights_json con
    retrocompat, invariante F12 pineada (acta solo manuales), dedup 2s/5s, visor con badge.
    Calibración real: 4 reuniones, 15 ventanas, 3 candidatos grounded, 0 spam → default true.
    +34 tests.
  - 5.3 chat cross-reunión + entregables (9a9e6e4): answer_multi (cap 12 por recencia,
    actas-nunca-transcripts, presupuesto budget_chars, exclusiones declaradas, citar
    fecha+reunión), 3 plantillas, endpoints chat-multi y deliverable-export (subcarpeta
    entregables/, prefijo vflow-entregable-), UI selección+overlay verificada por estado
    computado en navegador. +36 tests.
  - hotfix (dce56e7): tests de stop() escribían machine_id.txt en la raíz del repo con
    PENDING_EXPORT_DIR seteado en el entorno dev → gate O7 antes de get_machine_id() +
    fixture de aislamiento (PENDING_EXPORT_DIR/WEBHOOK_ENABLED/APP_DATA_DIR) + gitignore.
  - 5.5 fallback online↔local (433a8f4): TRANSCRIPTION_FALLBACK (indep. de GROQ_FALLBACK) +
    _is_network_error + breaker 120s + warmup al abrir + seek(0); scope SOLO dictado vía
    kwarg net_fallback (reunión/URL intactas, pineado); notificación tray sin Qt en core/;
    checkbox visible solo con backend groq (verificado computed-state en navegador). +36 tests.
  - INCIDENTE cazado y cerrado durante 5.5 (commit a9eead4 del ejecutor, auditado por el
    director): los tests de settings persistían al `.env` REAL del usuario vía
    web.state._set_env_key (dev: APP_DATA_DIR = raíz) — quedaron rutas tmp en
    PENDING_EXPORT_DIR/OPS_BRIEFING_PATH y una retención destructiva
    MEETING_RETENTION_DAYS=30 que Johann nunca configuró. Saneado (unset de las 5 claves) +
    conftest de sesión que redirige _ENV_PATH y APP_DATA_DIR a tmp. VERIFICADO sin pérdida:
    reunión más vieja 2026-06-16 (26d < 30d) y transcripciones desde 2026-03-16 intactas —
    la retención contaminada nunca purgó nada. Desviación de proceso anotada: el ejecutor
    commiteó pese al brief (contenido correcto; el candado de "solo el director integra"
    se refuerza en el próximo brief).
  - Cierre: CLAUDE.md actualizado (sección 18 nueva, dead-drop v1 en sección 16, env vars
    nuevas); 5.6/5.7 como pregunta opt-in en PARA JOHANN; screenshots de navegador no
    disponibles en el entorno del run (timeout de la tool) → la pasada visual queda en G2/G3.

## Completado (PLAN-MEJORAS, cont. 2)
- [x] **OLA 4 COMPLETA en su parte ejecutable** (2026-07-12, ventana 2, director @fable-5,
  ejecutores 3×@sonnet-5; 5 commits, suite 617 → 636 pass / 0 fail; 4.4 queda como GATE de
  Johann y 4.5 espera el reorden — ver PARA JOHANN).
  - 4.1 extracción del frontend (3 commits: eca600a plumbing WEB_TEMPLATES_DIR+template_folder+
    spec datas · 7c04e4b dashboard.html · a9f09db reunion.html + tests guardián + CLAUDE.md).
    Método del debate respetado: volcado del VALOR renderizado (no el fuente), inyecciones como
    variables de contexto `{{ mt_js|safe }}`/`{{ template_options_html|safe }}`/`{{ chips_json|safe }}`,
    sin `{% raw %}`. Ambas páginas BYTE-IDÉNTICAS a los goldens pre-cambio (180.198/57.719 chars);
    verificación en navegador ENTRE páginas (computed-state, router 6 vistas, trío paleta,
    dropdown 4 plantillas, chips, acentos, consola limpia). web/server.py 5086 → 1460 líneas.
    +8 tests guardián (sin `{{`/`{%` sueltos, `${...}` intacto, acentos por substring, chips
    sobre la línea real de asignación).
  - 4.2 blueprints (commit b886e2f): web/server.py 1460 → 93 líneas (create_app() + CSRF único
    a nivel app + shim que re-exporta _db/MEETING/PROACTIVE/_validate_*/_process_next_url_item/
    _assistant para la suite); web/state.py dueño único de singletons/helpers (266 líneas);
    web/blueprints/ 8 features, 50 rutas exactas sin url_prefix. CSRF verificado en vivo
    (Origin evil→403 — probe server-side; fetch del navegador NO puede falsificar Origin).
    url_for sin usos en el repo (endpoint names sin impacto). Desviación aceptada: cadencia
    por-blueprint → lote con doble suite + equivalencia + smoke (relocaciones verbatim).
  - 4.3 catálogo config (commit f5734fb): ENV_CATALOG con 67 vars (11 static / 56 lazy, 14
    kill-switches, 3 dynamic vía _DETECTION_FLAGS) + ENV_KNOWN_DIVERGENCES (4 benignas API-keys
    + bug CLAUDE_CLI_MODEL_* pineado por test SIN corregir) + tests/test_env_catalog.py (11
    tests: sincronía AST bidireccional + hot-reload de TRANSCRIPTION_BACKEND/AUDIO_SOURCE/
    SAVE_HISTORY). 15 constantes muertas borradas de config.py (las 12 de import + GROQ_API_KEY/
    AUDIO_SOURCE con limpieza del import de main.py:67 + WHISPER_LANGUAGE literal-trampa), cada
    una con grep triple previo. CLAUDE.md apunta al catálogo; PENDIENTES +2 ítems de higiene
    (LOCAL_WHISPER_MODEL sin recarga en caliente; defaults CLAUDE_CLI_MODEL_* inconsistentes).
  - Hallazgos menores para follow-up: comentario stale "web/server.py" dentro de
    _MT_INCREMENTAL_JS (vive en web/state.py; corregirlo cambia bytes servidos — hacerlo junto
    a la próxima edición real del template); worker de url_queue con TranscriptionDB propia
    (PREEXISTENTE, no regresión).
  - **4.0 (debate de diseño) COMPLETA** — ver Decisiones + `docs/OLA4-DISENO-EXTRACCION.md`
    (sección "RECONCILIACIÓN DEL DEBATE" = spec a ejecutar). Reconocimiento hecho (4 destilados:
    50 rutas reales no 112, templates ya bajo Jinja, 14 constantes config muertas, impacto
    PyInstaller cubierto). Apéndice C transferido a docs/PENDIENTES.md (4 ítems nuevos, ninguno
    bloquea). Ventana cerrada aquí por regla 8 (una ola grande/ventana; el debate + recon
    consumió la ventana) — la EJECUCIÓN de código (4.1→4.2→4.3) va en ventana nueva con contexto
    fresco (el reorden es delicado y el debate advirtió de errores silenciosos; no arrancar
    extracción de 3600 líneas sobre contexto cargado, regla 5).
  - (El "Next action: ejecutar 4.1→4.3" de la ventana 1 quedó CUMPLIDO en la ventana 2 —
    ver la entrada consolidada de arriba. El spec ejecutado fue el de la RECONCILIACIÓN,
    al pie de la letra, sin re-debate.)

## Completado (PLAN-MEJORAS)
- [x] **OLA 3 COMPLETA** (2026-07-12, 2 commits, suite final 617 pass / 0 fail). Debate
  adversarial previo (Opus 4.8, APROBAR CON CAMBIOS, 8/8 objeciones aceptadas — 2 BLOCKERs:
  cursor sin token de generación y prune sin limpieza FTS; ver Decisiones). Unidades: 3.1
  polling incremental ?since=N con gen de invalidación, helper JS único inyectado en ambos
  documentos, payload por poll 29.122→916 bytes (−96,9%), verificado en navegador real —
  la pasada de navegador cazó un ReferenceError en /reunion que los tests no veían, fix +
  test guardián en reintento 1 (c32f7cc) · 3.2 retención opcional MEETING_RETENTION_DAYS
  default 0 = conservar siempre, prune FTS-antes-que-filas, boot-only, advertencia UI,
  test crítico "0 no borra nada" (eb934bd). Hallazgo colateral anotado en docs/PENDIENTES.md
  (HISTORY_RETENTION_DAYS sin validador). Gate físico G2 Ola 3 en PARA JOHANN.
- [x] **OLA 2 COMPLETA** (2026-07-12, 3 commits, suite 441/10 → 588 pass / 0 fail). Debate
  adversarial previo (Opus 4.8, APROBAR CON CAMBIOS, 9/9 objeciones aceptadas — ver Decisiones;
  corrigió el diagnóstico "mocks 3.14" → drift arquitectónico, y evitó el verde falso de mover
  scripts __main__ sin def test_). Unidades: 2.1 suite verde, 10 fallos reparados sin debilitar
  contratos (94f32a1) · 2.2 triage de huérfanos: 5 reescritos a pytest hermético (+83 tests,
  2 bugs de hermeticidad de suite cazados de paso), 2 movidos tal cual, dual_capture/loopback
  quedan como diagnósticos HW, pytest.ini testpaths=tests + CLAUDE.md sección Testing (30deb3d)
  · 2.3 cobertura nueva: secrets DPAPI roundtrip + detect_platform + _parse_vtt_to_text +
  error_kind→HTTP vía test client, +54 tests, cero producción tocada (e97f431). Comando
  canónico documentado: venv\Scripts\python.exe -m pytest
- [x] **OLA 1 COMPLETA** (2026-07-12, 8 commits, 0 regresiones; suite final 441 pass / 10
  preexistentes). Debate adversarial previo (Opus 4.8, APROBAR CON CAMBIOS: 1.6 eliminada por
  premisa falsa, 1.4 recortada a _SILENCE_RMS y fusionada con 1.1 — ver Decisiones). Unidades:
  1.7 cierre atómico de generación (c843800) · 1.10 timeout real del reformateo (f7ff4eb) ·
  1.9 gateo momentos_destacados + fail-safe presupuesto (ff0aecb) · 1.8 robustez ruta URL: dedup
  solape ≥4 tokens, retry+marcador por chunk, refcount crypt32 (b52596c) · 1.2 TTL WAV fallido +
  aviso DB recuperada (1198206) · 1.3 validación de rutas en settings (35c2897) · 1.1+1.4 lock +
  try_push atómico en ProactiveGate, _last_error por tarea, _SILENCE_RMS de config (a17fae1) ·
  1.5 repair huérfanos vía DAO + Groq recrea cliente al cambiar key (e3735a2). Gate físico G2
  Ola 1 en PARA JOHANN. G3 de 1.4 quedó MOOT (sin cambio visual).
- [x] **OLA 0 COMPLETA** (2026-07-12, 4 commits, 0 regresiones; suite final 368 pass / 10 fallos
  preexistentes de mocks Groq/Python 3.14). Debate adversarial previo (Opus 4.8, APROBAR CON
  CAMBIOS — ver Decisiones). Gate físico G2 Ola 0 en PARA JOHANN.
- [x] 0.4 Cola URL idempotente + webhook sin redirects (F3/F8/F9)  (@sonnet-5 ejecutó, @fable-5
  verificó diff+suite, 2026-07-12, commit 2cd2066). item_id=None por iteración; source_queue_id +
  índice único parcial + IntegrityError→done; allow_redirects=False con 3xx terminal. 10 tests nuevos.
- [x] 0.3 Guards de auto-repeat AltGr+R/T (F10)  (@sonnet-5 ejecutó, @fable-5 verificó, 2026-07-12,
  commit 2595991). Patrón _h_held replicado; toggle legítimo intacto; 6 tests nuevos.
- [x] 0.2 No perder chunks en vuelo del dictado largo (F2)  (@sonnet-5 ejecutó, @fable-5 verificó,
  2026-07-12, commit 11743a1). Registro _chunk_threads + join con gracia 12s + abort por gen +
  detección por worker vivo + tray. 6 tests nuevos.
- [x] 0.1 Serialización ciclo de vida reunión + token de generación (F1)  (@sonnet-5 ejecutó,
  @fable-5 dirigió/verificó diff+suite, @opus-4.8 debatió, 2026-07-12, commit 2e568a6). start()
  rechaza durante stop en curso (_stopping, finally-safe), _session_gen descarta merges/detecciones/
  memoria-cruzada de daemons obsoletos, toggle con feedback tray, /api/meeting/start → 409.
  6 tests nuevos de carrera. Suite 346 pass / 10 preexistentes, 0 regresiones.

## Decisiones (PLAN-MEJORAS, append-only)
- 2026-07-12 **Debate adversarial Ola 5** (Fable propone, Opus 4.8 ataca con código real post-Ola 4;
  veredicto APROBAR CON CAMBIOS, 16 objeciones). Reconciliación (respuesta a CADA una):
  · A1 (ALTA, 5.1) "render diferenciado en acta" irrealizable por passthrough (momentos_destacados sin
  identidad por-ítem, _normalize_momentos gatea por highlights manuales) → ACEPTADA: los auto-highlights
  NO tocan momentos_destacados ni el acta; van a store propio `_auto_highlights` y se persisten en
  highlights_json con `source:"auto"` (manuales `source:"manual"`; entradas viejas sin source = manual);
  render con badge solo en dashboard/HUD + visibles vía highlights_json en MCP.
  · A2 (MED, 5.1) highlights_json sin campo source → ACEPTADA: se añade source con retrocompat.
  · A3 (ALTA, 5.1) tercera intención en update_state con max_tokens=1200 trunca JSON y congela el panel →
  ACEPTADA: max_tokens live sube a 1800, candidatos cap a 2 por ventana con salida corta {t, razon},
  test guardián de no-truncado; se mantiene en update_state (consolidate perdería granularidad de t).
  · A4 (ALTA, 5.1) anexar autos a self._highlights debilita el gate anti-alucinación F12 → ACEPTADA:
  los autos JAMÁS entran a self._highlights ni al gate F12; test que lo verifica.
  · B1 (MED, 5.4) export_pendientes actual viola O3/O7/naming del contrato → ACEPTADA: se REESCRIBE
  (create-only "x", gate ≥1 pendiente, naming instalacion+hash8), no "evolución suave".
  · B2 (BAJA, 5.4) machine_id no existe → ACEPTADA: se crea (persistido en dir de datos de la app).
  · B3 (MED, 5.4) guard meeting_id None sostiene SAVE_HISTORY → ACEPTADA: guard se preserva + test.
  · C1 (ALTA, 5.3) FTS 200 + N sin límite desborda presupuesto → ACEPTADA: cap duro 12 actas por
  recencia ANTES de cargar, truncado declarado en la respuesta.
  · C2 (5.3) CSRF hereda por before_request global → sin objeción (verificado por el adversario).
  · C3 (MED, 5.3) entregables al mismo dir que el inbox de tareas confunde al consumidor → ACEPTADA:
  subcarpeta `entregables/` + prefijo `vflow-entregable-` (nunca `vflow-pendientes-*`).
  · D1 (ALTA, 5.5) Transcriber.transcribe/translate es COMPARTIDO con reunión/URL → ACEPTADA: kwarg
  `net_fallback=False` (patrón return_raw); solo el dictado en main.py pasa True.
  · D2 (MED, 5.5) buffer posicionado tras fallo de red → ACEPTADA: seek(0) en el path de respaldo.
  · D3 (MED, 5.5) unificar flags rompe guard-tests de ENV_CATALOG → ACEPTADA: NO se unifica;
  TRANSCRIPTION_FALLBACK var nueva independiente, GROQ_FALLBACK intacta, relación documentada.
  · D4 (BAJA, 5.5) doble latencia (timeout red + warmup CTranslate2) → MITIGADA: al abrir el breaker
  se dispara warmup del modelo local en hilo daemon fire-and-forget; 1er dictado post-fallo paga
  warmup, siguientes no; notificación tray "transcrito localmente (sin internet)".
  · E1 (MED, 5.2) USER_* entra a artefactos persistidos/MCP sin gate silent → ACEPTADA parcial: la
  inyección EN VIVO (update_state) respeta el mismo gate silent que el briefing 7.1; acta y chat la
  llevan siempre (es el propósito de la feature: atribución); nota de privacidad en Ajustes.
  · E2 (BAJA, 5.2) identidad en prompt anti-callar induce atribución inventada → MITIGADA: la línea
  inyectada instruye "usa el nombre SOLO donde hoy dirías Yo; no atribuyas sin evidencia".
  · Encaje 5.6/5.7: acuerdo — NO corren en este run (sin elección explícita de Johann); quedan como
  pregunta opt-in abajo en PARA JOHANN.
  · Desviación de estampa: 5.2 sube de Haiku 4.5 med a Sonnet 5 med (el gate silent E1 + fraseo E2
  añaden juicio; regla §2.0 "ante la duda, sube"). : gatillo regla 6 del kickoff : @fable-5 + @opus-4.8
- 2026-07-12 **Debate adversarial Ola 4 / unidad 4.0** (director Opus 4.8 propone el diseño de extracción,
  adversario Opus 4.8 ataca con código real; veredicto APROBAR CON CAMBIOS; 7 objeciones, todas aceptadas —
  2 no-op/BAJA). Diseño FINAL reconciliado en **`docs/OLA4-DISENO-EXTRACCION.md`** (sección "RECONCILIACIÓN
  DEL DEBATE" = el spec a ejecutar). Cambios que forzó el debate: (O1 ALTA) los templates son triple-quoted
  NO-raw con 66 sitios `\\u`/`\\n`/regex → la extracción DEBE volcar el VALOR renderizado de la string a
  `.html`, NO copiar el fuente (si no, UI con acentos/emojis literales); verificación añade assert de acento
  por textContent · (O2 ALTA) ~8 tests importan `_db`/`MEETING`/`_validate_*`/`_process_next_url_item` de
  `web.server` → `web/server.py` re-exporta esos símbolos + `app=create_app()`, y se corre la suite ENTRE
  cada blueprint · (O3 ALTA→MED) R2 estaba MAL: `GROQ_API_KEY` y `AUDIO_SOURCE` SÍ se importan en `main.py:67`
  (sin uso en cuerpo) → borrar cualquiera de las 14 exige grep previo, y esas 2 exigen tocar main.py; 4.3 no es
  1 archivo · (O4 MED) `{% raw %}` descartado por miscount silencioso → los 3 puntos de inyección van como
  variables de contexto `{{ ...|safe }}` (incl. `mt_js`, sin `{% include %}`) + test guardián de `{{`/`{%`
  sueltos · (O5 MED) catálogo 4.3 opción A + test de sincronía que falla si un default diverge o hay un
  `os.getenv` no catalogado (sin el test, A es doc que envejece); opción B —churnear 48 call-sites— descartada
  (rompería hot-reload) · (O6 MED) "HTML idéntico" NO es diff-verificable (whitespace Jinja) → oráculo =
  DOM/computed-state/substring, byte-diff PROHIBIDO · (O7 BAJA) `TestHelperPresentInBothDocuments` ya asserta
  sobre HTML renderizado → NO se toca (no-op). No-problemas confirmados: sin import circular (solo disciplina
  de singleton), CSRF es un único before_request sin handlers ocultos, PyInstaller bajo riesgo y cubierto.
  Unidad más peligrosa = 4.2 (mueve la propiedad de los globals que la suite importa). : gatillo regla 6 del
  kickoff (debate por ola obligatorio) : @opus-4.8 (dir) + @opus-4.8 (adversario)
- 2026-07-12 Debate adversarial Ola 3 (Fable propone el plan literal, Opus 4.8 ataca con código real;
  veredicto: APROBAR CON CAMBIOS; 8 objeciones, 8 ACEPTADAS). O1 (BLOCKER) `since` sin token de
  generación pierde la reunión nueva (A deja since=50, B resetea _segments → cliente nunca ve B) →
  el modo incremental devuelve `gen` (=_session_gen de la Ola 0.1); cliente resetea since=0 y limpia
  DOM si gen cambia o total<since · O2 (BLOCKER) copiar prune_older_than tal cual deja huérfano
  meetings_fts (FTS mantenido a mano, sin triggers; el patrón correcto es el de meeting_delete:
  borrar fts por rowid ANTES del DELETE) · O3 NO fundir loadMeeting/loadLive (renderizan DOM distinto
  con ciclos de vida distintos): comparten helper de fetch+cursor, cada una conserva su render · O4
  el slice incremental va EN EL HANDLER (web/server.py), nunca dentro de transcript_segments() —
  snapshot()/chat en vivo y test_assistant_live fijan la lista completa · O5 el sorted() de
  transcript_segments SE CONSERVA (append-only estable, n~cientos, coste despreciable; el premio real
  es payload JSON, no CPU — recorte del alcance del plan "ordenado incrementalmente") · O6 cursor =
  índice de lista (nº de segmentos en mano), NO timestamp (mic y sys comparten t idéntico) NI
  segment_count de status() (dos snapshots) · O7 validar since: type=int, clamp [0,len], ausente →
  respuesta completa byte-idéntica · O8 3.2 reusa el patrón boot de main.py:502-508 con
  MEETING_RETENTION_DAYS (boot-only como HISTORY; la UI advierte "aplica al reiniciar"; guard
  days<=0 = no-op ya existente en el patrón). Dato clave del adversario: el HUD Qt NO consume
  /api/meeting (lee MEETING in-proc) — intocable. : gatillo regla 6 del kickoff : @fable-5 + @opus-4.8
- 2026-07-12 Debate adversarial Ola 2 (Fable propone el plan literal, Opus 4.8 high ataca con código real;
  veredicto: APROBAR CON CAMBIOS; 9 objeciones, 9 ACEPTADAS, 0 refutadas). O1 (ALTA) el diagnóstico "10
  fallos = mocks Groq incompatibles con Python 3.14" es FALSO: son drift arquitectónico pre-Olas (commit
  c70a617 movió Groq a core/backends/groq_backend.py:16; transcriber ya no lo importa) → fix = retarget
  del patch a core.backends.groq_backend.Groq, y el fix DEBILITANTE (mockear _get_backend o
  backend.transcribe) queda PROHIBIDO porque saltaría el filtro de alucinaciones que
  test_transcribe_filters_hallucination valida · O2 (ALTA) 2 fallos son drift de firma del recorder
  (_callback hoy toma 1 arg, recorder.py:261) · O3 test_hands_free_stop_on_shift_tap es test stale (la
  parada vive en _on_release, hotkey.py:327-330; NO es regresión de la Ola 0.3) → añadir _on_release ·
  O4 _alt_gr_space_mode renombrado a _alt_gr_t_mode · O5 (ALTA) "mover" los huérfanos = verde falso: 4 de
  los 6 útiles son scripts __main__ con 0 def test_ (pytest colectaría 0 tests) → el verbo de 2.2 es
  REESCRIBIR a pytest, no git mv; solo test_openrouter_backend.py (5 pass) y test_reasoning_router.py
  (18 pass) se mueven tal cual; test_dual_capture.py queda como script de diagnóstico HW (clase
  test_loopback) · O6 test_chapters.py ERRORA bajo pytest (manipula stdout) → reescribir sin eso ·
  O7 el mapeo error_kind→HTTP NO vive en url_transcribe: es dict inline del endpoint (web/server.py:
  4884-4890) → se testea vía Flask test client con transcribe_url monkeypatcheado (conservador: sin
  tocar código de producción en una ola de tests) · O8 la migración de key en claro vive en config.py
  import-time, no en secrets.py → 2.3 secrets = solo roundtrip encrypt/decrypt (testeable no-interactivo
  en Windows) · O9 no existe pytest.ini/testpaths: pytest a secas desde la raíz colecta los huérfanos y
  rompe → 2.2 añade [pytest] testpaths=tests y documenta el comando canónico. Confirmado por el
  adversario: 1.8 NO cubrió detect_platform ni _parse_vtt_to_text (2.3 es aditivo real; el dedup de cues
  VTT es DISTINTO del dedup de solape de chunks de 1.8). : gatillo regla 6 del kickoff : @fable-5 + @opus-4.8
- 2026-07-12 Johann respondió D1:B · D4:las 4 · D5:ambas (ver PARA JOHANN) → Olas 4/5/6 desbloqueadas.
  Este run mantiene el techo de ~2 olas/ventana (regla 8): esta ventana = Olas 2 y 3; la 4 (alcance
  COMPLETO 4.1+4.2+4.3+debate 4.4), la 5 (orden 5.2→5.4[G1]→5.1→5.3→5.5) y la 6 (re-validar spec
  primero) van en ventanas nuevas con sus kickoffs. : decisiones de Johann, no interpretadas : @fable-5
- 2026-07-12 Arranque del run: D1/D4/D5 sin responder → Olas 4/5/6 quedan como gates; ejecutable de
  este run = Olas 0→1→2→3 en ese orden. : regla del kickoff (no interpretar decisiones de Johann) : @fable-5
- 2026-07-12 Debate adversarial Ola 0 (Fable propone, Opus 4.8 high ataca con código real; veredicto:
  APROBAR CON CAMBIOS). O1 (ALTA) snapshot+null-out en Fase 1 de stop() rompe el drenaje (_chunk_loop/
  _transcribe_worker leen self.*, no locals; null-out = AttributeError o flush final perdido → pérdida de
  los últimos ~4 min del acta) → ACEPTADA: Alt A — Fase 1 solo `_active=False; _stopping=True`, drenaje
  sobre self.* como hoy, Fase 3 finally `_stopping=False`, reset en start() donde ya vive · O2 el check
  `gen==_session_gen AND _active` mata el update de insights legítimo del propio cierre → ACEPTADA: check
  solo por gen · O3 callbacks ligados-a-gen validan caso imposible bajo el guard → ACEPTADA: eliminados ·
  O4 detección de chunk perdido por hueco de índice = falsos positivos con chunks silenciosos (`if text:`
  main.py:612) → ACEPTADA: detección por worker VIVO tras join · O5 gracia 45s = techo de latencia del
  pegado → ACEPTADA: gracia ~12s (timeout API Groq 10s) · O6 dictado nuevo durante join → abortar join sin
  alarmar si gen!=_generation → ACEPTADA · O7 3xx webhook con allow_redirects=False debe ser fallo TERMINAL
  sin reintentos → ACEPTADA · O8 IntegrityError específico → set_done (no set_error genérico) → ACEPTADA ·
  O9 rechazo de start durante stop largo puede parecer cuelgue → MITIGADA: beep + tray inequívoco + 409 con
  mensaje claro. : gatillo §10.2 obligatorio por ola : @fable-5 + @opus-4.8
- 2026-07-12 Debate adversarial Ola 1 (Fable propone las 10 unidades del plan, Opus 4.8 high ataca con
  código real; veredicto: APROBAR CON CAMBIOS). O1 (ALTA) **1.6 ELIMINADA — premisa falsa**: la ruta de
  audio de transcribe_url YA aplica el diccionario vía Transcriber.transcribe (transcriber.py:221); la
  asimetría que vio la auditoría es que los subtítulos NO pasan por Transcriber (por eso aplican explícito);
  aplicar de nuevo sería no-op o doble aplicación dañina con reglas encadenadas → se degrada a test
  documental dentro de 1.8 · O2 **1.4 recortada**: CARD_STYLES son dos representaciones de vista distintas
  (Qt hex/emoji vs clases Tailwind; solo comparten label) — single-source forzaría una capa de mapeo peor
  que la "duplicación"; queda solo _SILENCE_RMS→config y se FUSIONA con 1.1 (mismo archivo; G3 de 1.4 MOOT)
  · O3 try_push con Lock no reentrante deadlockea si llama should_push/mark_pushed → ACEPTADA: helpers
  internos sin lock, un solo acquire, presupuesto no se consume si el dedup impide encolar · O4 dedup de
  solape exacto no muerde y fuzzy se come repeticiones legítimas → ACEPTADA: match conservador ≥4 tokens
  normalizados sufijo/prefijo, sin match no se toca · O5 shutdown(wait=False) fuga hilos non-daemon que
  bloquean el exit → ACEPTADA: 1.10 usa threading.Thread(daemon=True) + espera 8s · O6 isdir() duro rompe
  shares UNC intermitentes → ACEPTADA: existencia solo para rutas locales, UNC pasa por formato, blacklist
  normalizada sin atrapar %APPDATA%\Vflow · O7 drift de líneas post-Ola-0 (consumidor de last_error en
  meeting.py:1084) → ACEPTADA: last_error(task), consumidor a "live" · O8 borrar el WAV en cualquier éxito
  mata el WAV que el usuario iba a recuperar → ACEPTADA: solo el éxito de DICTADO borra, más TTL 24h al
  arrancar · O9 1.7 compatible con el join de Ola 0 (verificado) → sin cambio · O10 1.9 válida tal cual.
  Orden de ejecución por clusters: batch1 paralelo {1.9 insights · 1.8 url_transcribe · 1.10
  dictation_modes · 1.7 inline main.py} → batch2 paralelo {1.1+1.4 proactive/insights/meeting · 1.2
  main+db · 1.3 web/server} → batch3 {1.5 db+web/server+groq_backend}. : gatillo §10.2 : @fable-5 + @opus-4.8
- 2026-07-12 Debate adversarial del CONTRATO MACROSISTEMA (diseño anticipado de 5.4 + núcleo de 4.5,
  ventana Fable; Fable propuso, Opus 4.8 atacó con código real; veredicto APROBAR CON CAMBIOS, 11
  objeciones TODAS aceptadas/mitigadas). Contrato final + registro completo del debate:
  **`docs/CONTRATO-MACROSISTEMA.md`** (fuente de verdad; las unidades 5.4 y 4.5 se implementan
  CONTRA ese spec, tras el reorden). Cambios clave que forzó el debate:
  id por hash por pendiente + gate ≥1 pendiente; due/transcript_offset en vez del t ambiguo y sin
  enum de responsable (no existe en el schema real); create-only con machine_id+hash (meeting_id no
  es clave global); get_related_context (nombre honesto, retrieval) en vez de "consolidado" — la
  consolidación real es unidad v2; list_pending_actions FUERA de v1 (no existe señal de cierre);
  módulo puro core/context_pack.py como prerequisito de MCP+CLI; señal de cobertura FTS en cada
  respuesta; advertencia explícita read-only≠confidencial. **Gates G1 y G-agent APROBADOS por
  Johann en chat (2026-07-12): G1 con buzón nuevo `C:\OPS\_inbox-vflow\` (creado, con _LEEME.md
  para el consumidor); G-agent alcance v1 completo. Las unidades 5.4 y 4.5 ya NO tienen gate:
  se implementan directo contra docs/CONTRATO-MACROSISTEMA.md tras el reorden.** : gatillo §10.2
  + gates G1/G-agent resueltos : @fable-5 + @opus-4.8 + Johann
- **G2 — Prueba física de 1.3 Highlight AltGr+H** (5 min): (1) inicia una reunión real con AltGr+R y audio sonando; (2) pulsa AltGr+H dos o tres veces en momentos distintos → debes oír un beep agudo y ver la notificación "✓ Momento destacado (mm:ss)"; (3) MANTÉN AltGr+H apretado 2s → debe registrar UN solo highlight (anti auto-repeat); (4) pulsa AltGr+H SIN reunión activa → no debe pasar nada; (5) termina la reunión y revisa que el acta (dashboard → Reunión → historial) tenga la sección "⭐ Momentos destacados" con contexto correcto; (6) con el IDE abierto, verifica que Ctrl+Alt+H de tu IDE no dispare nada raro (AltGr ≡ Ctrl+Alt físico en Windows; la app distingue AltGr real). Nota: en layouts ES/LatAm H no es dead-key (verificado en debate).
- **G3 — Gusto visual**: la sección "⭐ Momentos destacados" en el acta (vista en vivo e historial) sigue los tokens del rediseño; revísala async cuando pruebes G2 y pide ajustes si no convence.
- **G2 Ola 2 — Prueba física del panel en vivo** (10 min, requiere reiniciar Vflow): (1) AltGr+R con audio del sistema sonando → en /reunion: pestañas En vivo/Preguntar, timer corriendo, VU "Yo" se mueve al hablar y VU "Ellos" con el audio del PC; (2) el pill debe mostrar anillo ámbar + timer mm:ss + punto cian cuando "Ellos" suena; clic corto en el pill abre /reunion; (3) botón ⏸: el timer se congela y al reanudar NO cuenta el tiempo pausado; (4) botón ⭐ y una nota 📝 → al terminar, el acta trae momentos destacados y la reunión guarda notes_json; (5) espera un pendiente detectado → tarjeta con ✓/✗, dale ✓, no debe reaparecer y caduca ~3 min; (6) pestaña Preguntar durante la reunión: chips fijos + pregunta libre → responde sobre lo dicho con timestamps.
- **G3 Ola 2 — Gusto visual del panel en vivo y el pill** (async): screenshots automáticos no disponibles esta sesión (el renderer del preview falla en captura; verificado por escaneo de overlays), así que revisa en vivo cuando hagas el G2.
- **G2 Ola 4 — Prueba física del patrón Granola** (10 min, con la app reiniciada): (1) elige plantilla "Ventas" en el dropdown de /reunion, inicia con AltGr+R (el hotkey debe usar la plantilla elegida), habla de un caso comercial con precio/tiempos y termina → el acta debe traer BANT (solo campos con evidencia) y chips de venta en Preguntar; (2) en otra reunión escribe 2-3 notas en vivo → el acta debe mostrarlas LITERALES con contexto de la IA y el resumen priorizando lo anotado; (3) haz clic en el chip mm:ss de una decisión → debe saltar y flashear el segmento correcto del transcript.
- **G2 Ola 5 — Prueba física del proactivo v2** (10 min, app reiniciada, reunión real AltGr+R con audio): (1) HUD flotante: despliégalo con AltGr+A y desde el pill; tarjetas de pendientes con ✓/✗ funcionan; Esc devuelve el foco a donde estabas; (2) "me perdí" AltGr+M → resumen de contexto reciente; (3) badge de 1 palabra en el pill cuando hay sugerencia; (4) detecciones 5.1: haz una pregunta que nadie responda y adquiere un compromiso hablando → deberían aparecer tarjetas (máx ~1 push/5 min salvo pendientes); (5) memoria cruzada 5.2: habla de un tema que YA esté en el acta de una reunión pasada de tu DB → tarjeta "El dd/mm se acordó: …" (solo en modo copilot/entrenador); (6) conflictos de hotkeys: con tu IDE abierto, verifica que AltGr+A y AltGr+M no disparen atajos raros del IDE (AltGr ≡ Ctrl+Alt; reporta cualquier choque); (7) modos: en Silencioso no debe aparecer NINGÚN push.
  - **Fix post-G2 (2026-07-03, commit bd58d53):** Johann probó el HUD real y reportó dos bugs que ninguna verificación headless podía atrapar: (a) el HUD se veía "casi invisible" — un QWidget plano con solo WA_TranslucentBackground no garantiza que su stylesheet pinte fondo/bordes en Windows (la pill nunca dependió de esto: pinta a mano en paintEvent); (b) el HUD quedaba DETRÁS del pill — la pill reasserta HWND_TOPMOST por Win32 cada 1s y el HUD solo tenía el hint más débil de Qt. Corregido replicando el patrón de pill_widget.py (paintEvent con QPainter para el fondo redondeado, mismo mecanismo Win32 SetWindowPos con timer de 500ms) + anclaje que se recalcula con la altura REAL tras el primer show() + ancho/tipografía/botones rediseñados. Verificado con smoke test headless (offscreen) sin excepciones; **la verificación visual final sigue pendiente de tu ojo** — vuelve a probar el HUD.
- **G2 Ola 6 — Prueba física de plataforma y dictado** (15 min, app reiniciada): **(webhook 6.1)** en Configuración → Webhook: actívalo apuntando a un receptor tuyo (p. ej. webhook.site con WEBHOOK_ALLOW_LOCAL=false, o un servicio local con =true), pon un secreto, cierra una reunión corta → debe llegar UN POST con header X-Vflow-Signature verificable; con scope=pendientes el body NO trae resumen ni transcript; prueba también una URL http:// o a 192.168.x sin el opt-in → la UI/log debe rechazarla; **(dead-drop)** setea PENDING_EXPORT_DIR a una carpeta y cierra otra reunión → aparece vflow-pendientes-<id>-<fecha>.md; **(6.2)** dicta algo que tu diccionario corrija → en el historial esa fila muestra "Ver crudo" (el original) y "Deshacer edición IA" lo restaura; corrige a mano UNA palabra de una transcripción en el dashboard → en el panel Diccionario aparece la sección "Sugeridas" con el par, nace DESACTIVADA, Aceptar la activa y Descartar la borra; **(6.3)** activa "Modos de dictado por app" en Configuración, dicta en tu cliente de email → registro formal; dicta en el IDE (code.exe) → términos técnicos intactos; dicta en una app NO mapeada → texto idéntico al de siempre; verifica que la latencia con el toggle OFF sigue siendo la normal, y que "Deshacer edición IA" también revierte el reformateo.
- **G2 Ola 7 — Prueba física del briefing OPS** (5 min, app reiniciada): (1) crea un .md corto con contexto tuyo compartible (p.ej. `C:\OPS\_briefing\contexto.md`: un proyecto activo + un compromiso con fecha); (2) en Configuración → "Copiloto con contexto OPS" pega la ruta y guarda; (3) inicia una reunión real (AltGr+R) y habla ~1 min tocando algo que se relacione con tu briefing; (4) en /reunion → pestaña "Preguntar" pregunta "¿esto conecta con algo mío?" → la respuesta debe CONECTAR con lo del briefing citando timestamps, sin inventar; (5) cambia el modo proactivo a **Silencioso** y repite la pregunta → la respuesta ya NO debe usar el briefing (gate de privacidad para pantalla compartida); (6) borra el .md mientras la app corre y pregunta otra vez → no debe romperse (fail-open, responde sin briefing). Nota: el susurro NO empuja tarjetas de contexto en el insight stream (eso es backlog v1.1, ver G4 Ola 7).
- **G4 Ola 7 — Recorte de alcance del briefing OPS (decisión del debate, revisable async):** el plan pedía inyectar el briefing en el insight stream en vivo (update_state) para habilitar tarjetas de detección "🧭 Contexto:". El debate adversarial (2 Opus con código real) probó que eso descalibra las detecciones 5.1 (0 FP en 12 ventanas, modelo Haiku susceptible a 8KB fijos) y que su verificación honesta exige ≥12 ventanas midiendo FP Y FN. Decisión: **v1 entrega el briefing SOLO en el chat pull "Preguntar" (answer_live)** — donde no hay detecciones que romper, el usuario lo pidió explícitamente y el costo de un error es una respuesta mediocre, no una interrupción. Las tarjetas "🧭 Contexto:" del insight stream quedan como v1.1 con recalibración bloqueante. Si quieres esas tarjetas ya (aceptando el trabajo de recalibración), pídelo; no bloquea el resto. **Nota de privacidad a tener en cuenta al usar la feature:** (a) comparte pantalla → pon modo Silencioso (ahora el briefing también se omite del chat en Silencioso); (b) con INSIGHTS_FALLBACK=true (default), si tu backend primario falla, el prompt con briefing puede salir por groq/openrouter — misma frontera de red que el transcript, pero el briefing es un dossier tuyo más concentrado; tenlo presente si el briefing lleva algo sensible.
- **G4 Ola 3 — Recorte de alcance (decisión técnica, revisable async):** el plan prometía "turnos e interrupciones (solape)" pero el debate adversarial probó contra el código que el solape entre canales NO es computable de forma fiable: el loopback WASAPI se salta silencios, así que los ejes de tiempo de "Yo" (mic continuo) y "Ellos" (muestras discontinuas) divergen sin mapa común. v1 entrega: talk-time %, talk-to-listen, monólogo más largo por canal, WPM, preguntas por canal, y "turnos" aproximados por alternancia de speaker en el texto. "Interrupciones" queda fuera (necesitaría timestamps de pared por buffer del loopback: posible v2 anotando el instante de llegada de cada buffer). Si quieres esa v2, pídela; no bloquea nada de lo demás.

## En curso
- (vacío) — todas las olas 1-7 del plan están COMPLETAS. No queda trabajo ejecutable por el orquestador; lo pendiente son gates humanos async (G2/G3, ver "PARA JOHANN"). Si Johann quiere el backlog v1.1 de la Ola 7 (tarjetas "🧭 Contexto:" en el insight stream con recalibración de 5.1), eso abre una unidad nueva, no reanuda ésta.

## Completado (cont.)
- [x] Ola 7 COMPLETA (2026-07-03, Copiloto con contexto OPS, unidad 7.1 briefing v1, commit 9c83041). Dirigió @opus-4.8; understand por @sonnet-5; debate adversarial por 2×@opus-4.8 (ángulos correctitud/side-cases + verificación-real/privacidad/detecciones, veredicto RECHAZAR forma actual → v1 recortada); ejecutó @sonnet-5. **Alcance final v1: briefing SOLO en el chat pull answer_live, NO en el insight stream** (core/insights.py intacto). core/ops_briefing.py nuevo (cache TTL 60s + invalidate, build-fuera-del-lock/swap-dentro, guard 8KB sobre bytes leídos, utf-8-sig, fail-open total, nunca loguea contenido); assistant.py (briefing en prefix con válvula de sacrificio en cascada transcript>briefing>insights, gate silent, instrucción condicional); web/server.py (setting + Ajustes + invalidate al guardar); config.py comentario-catálogo; CLAUDE.md §17. Verificado: 20 tests nuevos pass, suite 340 pass / 10 fallos preexistentes (baseline 320/10, 0 regresiones) + llamada REAL claude-cli (susurro conecta transcript↔briefing citando Acme/viernes solo-del-briefing + timestamps reales, sin inventar). Gates G2/G4 Ola 7 en PARA JOHANN. Backlog v1.1: tarjetas "🧭 Contexto:" en insight stream con recalibración bloqueante de 5.1.
- [x] Ola 6 COMPLETA (2026-07-03, Plataforma y dictado, EN SERIE por O7): 6.1 webhook HMAC + dead-drop pendientes (@opus-4.8 ejecutó, daad367: core/webhook.py con firma HMAC-SHA256, anti-SSRF con validación de IP resuelta y bloqueo de DNS mixto, payload minimizado por WEBHOOK_SCOPE sin transcript, fire-and-forget post-persistencia, secreto DPAPI write-only, requests a top-level + lock regenerado; 27 tests + flujo real local) · 6.2 crudo/Undo + diccionario sugerido (@sonnet-5 ejecutó, b952411: raw_text con migración idempotente, transcribe/translate con kwarg return_raw — elegido sobre atributo por carrera de datos entre hilos de chunks —, toggle "Ver crudo"/"Deshacer edición IA" no retroactivo, sugerencias SOLO desde el PUT del dashboard con difflib enabled=0 + bandeja Sugeridas; fix de paso: la rama de recuperación por DB corrupta no corría _MIGRATIONS; 18 tests + UI por estados computados) · 6.3 modos de dictado por app (@sonnet-5 ejecutó, 473c872: captura del exe en foco vía ctypes en save_frontmost_app, mapa exe→preset configurable, 3 presets email/chat/codigo, reformateo en background con timeout 8s y fallback al original, claude-cli NUNCA inicia el reformateo — el fallback interno de _chat puede llevarlo a groq/openrouter —, raw_text "más crudo gana" coherente con 6.2; 33 tests) · 6.4 ya estaba (rediseño shell). Suite verificada por el orquestador con el venv del repo: 320 pass / 10 fallos preexistentes (el reporte de "14 fallos" del ejecutor 6.3 era artefacto de correr con otro Python). Ambos ejecutores 6.1 y 6.2 se atascaron una vez (watchdog 600s) y se reanudaron por mensaje con contexto vivo (reintento 1, patrón útil). Gate G2 Ola 6 en PARA JOHANN.
- [x] Ola 5 COMPLETA (2026-07-03, Proactivo v2, orden 5.3 → 5.1 → 5.2): 5.3 gating proactivo + HUD flotante (4395425 + fix import ctranslate2 05b9164) · 5.1 detecciones proactivas como segunda intención del MISMO update_state — cero cuota extra (cd77946; calibración: 0 falsos positivos en 12 ventanas reales, control positivo OK; flags PROACTIVE_DETECT_* con kill-switch por clase; detections_json persistido) · 5.2 memoria cruzada en vivo (@fable-5-low ejecutó, @fable-5 dirigió/verificó diff+tests, 2c30480: retrieval puro CERO LLM en cada consolidación; _search_terms → meetings_search con bm25 AS score aditivo → gate por overlap ≥2 tokens (_tokens) → tarjeta tipo 'cruzada' vía PROACTIVE con dedup 1/reunión pasada/sesión, presupuesto agotado NO quema dedup, flag PROACTIVE_DETECT_CRUZADA; 16 tests nuevos pass re-ejecutados por el orquestador, suite 242 pass / 10 fallos preexistentes, 0 nuevos). Gate G2 Ola 5 en PARA JOHANN.

## Completado (fuera de olas, cont.)
- [x] Presets de backend + fallback automático con circuit breaker  (@sonnet-5 ejecutó, @fable-5 dirigió/verificó, 2026-07-03, commit 966128c; los cambios de UI quedaron absorbidos en 450fd77 por colisión con la sesión 1b — verificado línea a línea que están intactos)
  - Qué: botones "Usar mi suscripción" (ambos→claude-cli) y "Usar APIs (benchmark)" (live=groq, batch=openrouter) con validación de CLI/keys y toasts + INSIGHTS_FALLBACK (default true): si el backend primario falla, _chat reintenta con groq→openrouter (nunca endpoint/anthropic/claude-cli) y abre breaker por backend (INSIGHTS_FALLBACK_COOLDOWN=300s).
  - Debate Opus (APROBAR CON CAMBIOS): breaker por backend, is_available con task, retry dentro de _chat, endpoint excluido del fallback, validación en presets. Refutada: preset suscripción-en-vivo (decisión explícita de Johann, plan Max).
  - Verificado: 5 escenarios de fallback con monkeypatch (fallo→fallback, breaker salta directo, reintento tras cooldown, propagación con fallback off, endpoint excluido) · app viva reiniciada: settings devuelven insights_fallback=true, claude_cli_available=true, has_groq/openrouter_key=true; HTML servido contiene los presets.
  - Config activa de Johann: live=claude-cli (Haiku) + batch=claude-cli (Sonnet) con fallback armado hacia groq/openrouter.

## Completado (fuera de olas)
- [x] Backends Claude para insights/acta (pedido de Johann 2026-07-03)  (@sonnet-5 ejecutó, @fable-5 dirigió/verificó, 2026-07-03, commits 4e1f88c + 208b56a)
  - Qué: backends "anthropic" (API oficial, ANTHROPIC_API_KEY cifrada DPAPI, ANTHROPIC_MODEL_LIVE=claude-haiku-4-5 / ANTHROPIC_MODEL_BATCH=claude-sonnet-5, sin sampling params ni thinking explícito, aplana bloques text, maneja refusal) y "claude-cli" (suscripción vía claude -p, SOLO batch, prompt por stdin, cwd neutro %APPDATA%\Vflow) en core/insights.py + config.py (descifrado DPAPI) + selector y campo de key en panel Configuración + _budget_chars + vflow.spec hiddenimports + requirements/lock + docs.
  - Verificado: imports OK · matriz is_available OK · llamada REAL a claude-cli con la suscripción (JSON parseado por _extract_json) · fix de Fable tras revisar el diff: en dev APP_DATA_DIR es el repo → claude cargaba CLAUDE.md/.mcp.json por llamada; ahora cwd neutro verificado con subprocess interceptado (208b56a).
  - Atajo sin código disponible: backend openrouter + OPENROUTER_MODEL=anthropic/claude-haiku-4.5 (del debate, alternativa B).
  - Pendiente manual (Johann): elegir backend en el dashboard (Configuración) y, si usa el backend API, pegar su ANTHROPIC_API_KEY.
  - Last checkpoint: debate adversarial cerrado (Opus: RECHAZAR v1 por alcance subestimado → diseño v2 reconciliado). Objeciones: #1 descifrado DPAPI hardcodeado (ACEPTADA: bloque en config.py) · #2 cableado real en _chat/is_available/_model (ACEPTADA: brief enumera 7 puntos) · #3 vflow.spec sin el SDK (ACEPTADA: hiddenimports) · #4 _extract_json vs bloques Claude (ACEPTADA: aplanar text blocks) · #5 temperature en toda la cadena (ACEPTADA: backend lo ignora; Sonnet 5 rechaza sampling no-default) · #6 transcript en argv (MITIGADA: prompt por stdin + nota de privacidad en UI) · #7 which(claude) en .exe (MITIGADA: CLAUDE_CLI_PATH → which → %APPDATA%\npm) · #8 latencia CLI en live (ACEPTADA: claude-cli SOLO batch) · #9 "IDs de modelo inventados" (REFUTADA: claude-haiku-4-5 y claude-sonnet-5 verificados contra la doc oficial vía skill claude-api) · #10 _budget_chars (ACEPTADA: casos nuevos). Alternativa B de Opus adoptada como atajo sin código (openrouter + anthropic/claude-*); descartar claude-cli rechazado (pedido central de Johann, viable batch-only). Riesgo extra detectado por Fable: el subprocess de claude-cli debe correr con cwd en el dir de datos, no en el repo (evita cargar CLAUDE.md/.mcp.json del proyecto).
  - Next action: agente Sonnet 5 implementando (brief completo enviado); al retorno: revisar diff real + verificación c (llamada real claude-cli) → commit único.

## Completado
- [x] Ola 4 COMPLETA (2026-07-03, patrón Granola, 4 commits): 4.0 presupuesto de contexto del acta (2a25e11, prerequisito hallado por el debate: generate_minutes/chapters truncan por el principio con aviso; helper budget_chars compartido) · 4.1 fusión de notas (f80986d, @fable-5-low: notas_usuario con literal protegido por post-proceso determinista + prioridad condicional; validada contra reunión real de 69 min vía claude-cli) · 4.2 trazabilidad (e9f1016, @sonnet-5: decisiones/pendientes con t snapeado en Python al segmento — patrón chapters —, chip mm:ss clicable con flash, tolerancia total a actas viejas; validada con reunión real: 3/3 y 4/4 snaps correctos) · 4.3 plantillas (e4900d5, @sonnet-5: 4 plantillas server-side, BANT solo-evidencia, chips por template sincronizados por polling + fix del orquestador para cambios externos). Todo verificado en navegador (chips, flash, dropdown, actas viejas, consola limpia).
- [x] Ola 3 COMPLETA (2026-07-03): 3.1 motor de métricas (@fable-5-med ejecutó, commit 946b710: Silero cacheado a nivel módulo + speech_timestamps en segundos, anclaje por muestras POR CANAL, funciones puras meeting_metrics, metrics_json en DB/API/MCP, pause() reordenado; fixture TTS con dropout simulado: talk-time error 0.0%, VAD ~100ms/ventana; interrupciones fuera de v1 → G4) · fix colateral 56b3852 (@fable-5 orquestador: onnxruntime debe importarse antes que PyQt6; el VAD del dictado estaba en fail-open silencioso en dev — hallado por el ejecutor, reproducido y verificado) · 3.2 UI (@sonnet-5 ejecutó, commit 19c4b6d: mini-dona en tarjeta + sección Estadísticas en visor; corrección del orquestador en navegador: badge "largo" anclado al canal que cruza 90s, no a Yo fijo; verificado con filas sintéticas con/sin metrics, null paths, overlays cero, consola limpia, filas y FTS limpiados).
- [x] Ola 2 COMPLETA (2.1 + 2.2 + 2.3 + hotfix paleta). G2 ampliado en PARA JOHANN.
- [x] 2.3 Chat "Esta reunión" EN VIVO  (@fable-5-low ejecutó, @fable-5 dirigió/verificó, 2026-07-03, commit 19a50bc)
  - Qué: MEETING.snapshot() (foto atómica bajo lock), build_context_live/answer_live en core/assistant.py (recorte por el principio con aviso, _SYSTEM_LIVE cita mm:ss sin IDs, transcript vacío responde sin LLM), endpoint /api/meetings/chat rutea vivo/DB (flag live explícito o auto si activa+sin meeting_id), pestaña Preguntar con chips fijos + nota "reunión en curso".
  - Verificado: 20 tests (snapshot inmutable, recorte 18KB contiguo, 4 side cases, ruteo con test client) · llamada real claude-cli (respuesta anclada a timestamps, cero invención) · navegador: chips/nota por estado computado, live:false→DB, live:true→respuesta sobre segmentos sintéticos · pasada de superficie completa (escaneo de overlays: cero elementos tapando en / y /reunion; screenshot del preview falló por timeout del renderer → vía alterna aplicada según feedback verificar-ui-como-el-usuario).
- [x] Hotfix paleta Ctrl+K (reporte de Johann en vivo, 2026-07-03, commit 9fefc23): #palette-overlay{display:flex} por ID le ganaba a .hidden → paleta siempre visible tapando el dashboard. Lección registrada como feedback OPS verificar-ui-como-el-usuario + routers + CLAUDE.md Vflow (23e6788).
- [x] 2.2 Tarjetas de pendientes ✓/✗ + feedback  (@sonnet-5 ejecutó, @fable-5 dirigió/verificó, 2026-07-03, commit e99ed9f)
  - Qué: tarjetas keyeadas por hash de texto normalizado (inmunes a renumeración de IDs), caducidad ~180s, máx 3, POST /api/meeting/feedback, MEETING.add_feedback con dedup por key, feedback_json persistido; panel embebido del dashboard recortado a solo pendientes + link a /reunion.
  - Verificado: 7 tests + navegador (tarjeta aparece, feedback ✓ la retira y no reaparece, POST valida value, consola limpia).
- [x] 2.1 UI /reunion "En vivo" + estados del pill  (@sonnet-5 ejecutó, @fable-5 dirigió/verificó/corrigió, 2026-07-03)
  - Qué: /reunion con pestañas En vivo/Preguntar, header timer + VU por canal (levels RMS baratos por callback en MEETING), barra de 4 acciones (⭐ highlight, 📝 nota, ⏸ pausa congelar-reloj con _paused_total, ⏹ terminar), transcript con badges Yo/Ellos, placeholder #pending-cards; endpoints POST /api/meeting/{highlight,note,pause,resume} (CSRF via before_request global); notes_json en meetings; pill con anillo ámbar de reunión + timer mm:ss + indicador cian "Ellos" + clic corto (<6px) abre /reunion.
  - Corrección del orquestador tras verificación en navegador: el bloque "Análisis en vivo" (renderInsights) seguía empujando temas/pendientes en MEETING_PAGE — eliminado (push→pull; quedan las refs del panel embebido del dashboard, decisión de superficie para 2.2).
  - Verificado: 18 tests nuevos (pausa congela elapsed con monotonic mockeado, contigüidad post-resume, callbacks no-op en pausa, notes round-trip) + navegador con MEETING sintético (15/15 ids, timer corre y congela en pausa, VUs 60%/30% correctos, sin fuga de insights, endpoints ok, tabs ok) · suite: 0 regresiones nuevas.
  - Pendiente manual (Johann): pill (anillo/timer/indicador cian/clic) requiere app de escritorio → G2 ampliado.
- [x] 1.3 Highlight AltGr+H  (@sonnet-5 ejecutó, @fable-5 dirigió/verificó, @opus-4.8 debatió, 2026-07-03)
  - Qué: señal highlight_pressed en core/hotkey.py (vk 0x48 + flag _h_held anti auto-repeat), slot _on_highlight en main.py (guard reunión activa, beep 1046 Hz + tray), MEETING.add_highlight() thread-safe + persistencia highlights_json (migración idempotente en db/database.py), generate_minutes(*, highlights=) inyecta timestamps al prompt y _MINUTES_SYSTEM pide momentos_destacados (callar > inventar), render en renderMinutes + actaHtml (web), meeting_export (.md), assistant._format_acta y docstring MCP get_minutes. Tests: tests/test_highlight.py (11 pass).
  - Verificado: 7 tests sintéticos de hotkey (auto-repeat suprimido: 1 emit tras 20 press; T/R/modo1 sin regresión) + 4 unitarios DB round-trip · suite completa sin regresiones nuevas (10 fallos preexistentes confirmados con stash, mocks Groq vs Python 3.14) · acta REAL vía claude-cli con transcript de la DB (id 15): momentos_destacados con contexto correcto, sin inventar · render en navegador (Flask 5679 + fila de prueba, visor historial y renderMinutes OK, consola sin errores, fila y FTS limpios tras la prueba).
  - Pendiente manual (Johann): gate G2/G3 arriba.
  - **Con esto la OLA 1 queda completa (1.1 + 1.2 + 1.3).**
- [x] 1.1 Servidor MCP local  (@fable-5, 2026-07-03, commit 527156d)
  - Hecho: debate adversarial del contrato (Opus 4.8, 10 objeciones respondidas) · db/database.py: helper _connect() con busy_timeout 5s en TODAS las conexiones, PRAGMA journal_mode=WAL incondicional en _init_db, modo read_only=True (URI mode=ro, salta DDL/migraciones/backfill, error accionable si la DB no existe), meetings_search(raise_errors=) · mcp_server/ nuevo (FastMCP stdio, tools search_meetings/get_minutes/get_transcript) · .mcp.json (venv python) · mcp==1.28.1 en requirements.in/.txt + requirements.lock regenerado con hashes · CLAUDE.md sección 12.
  - Verificado: smoke unitario de las 3 tools · cliente MCP real (SDK stdio) con la app corriendo Y reunión activa: 3 tools OK, 4 llamadas en 0.02s, captura nunca bloqueada, reunión de prueba cerró limpia (silencio → saved:false, sin fila basura) · escritura concurrente con lector RO en transacción abierta: sin locks, snapshot isolation OK, fila de prueba borrada · RO no puede escribir (OperationalError a nivel SQLite) · grep confirma migración total a _connect().
  - Pendiente manual (Johann): conectar desde Claude Code vía .mcp.json en una sesión nueva y consultar una reunión real.

## Decisiones (append-only)
- 2026-07-31 **Token local de sesion para el dashboard** (commit f3faf67). Se cierra la unica falla que este fork heredaba del upstream: `_csrf_check` exime GET/HEAD/OPTIONS a proposito, asi que las LECTURAS de `/api/transcriptions` y `/api/meetings` estaban abiertas a cualquier proceso local, y aqui eso ya no son dictados sueltos sino transcripts y actas de reuniones con terceros. **Dos divergencias DELIBERADAS de la convencion del repo, ambas porque aqui el lado seguro del fallo es el contrario:** es fail-CLOSED (al reves de `ops_briefing.py`, donde abrirse es inocuo) y el killswitch apaga SOLO con el literal `false` (no el `== "true"` habitual), para que basura en el `.env` deje la proteccion encendida. **Limite ACEPTADO, no es deuda:** un proceso del mismo usuario de Windows puede leer el archivo del token o la SQLite; esto sube el liston de "curl trivial" a "hay que leer un archivo". Verificacion independiente que MUTO `is_enabled`/`verify` para probar que los tests detectan un guard roto. : origen = auditoria del upstream desde OPS, no el backlog : @fable-5 (dir) + @opus-5 (ejec) + @sonnet-5 (verif)
- 2026-07-31 **Plan nuevo `docs/PLAN-DICTADO-2026-07-31.md`** (commit 9bc8c99), 8 olas, paralelo al PLAN-MEJORAS. Copia del upstream lo que aporta (smart commands, Transform, snippets, Command Mode) y suma dos cosas que salieron de mirar el propio codigo. **Tres correcciones que cambian el encuadre:** (1) la idea del correo estructurado YA existia en `dictation_modes` (Ola 6.3) y solo le faltaban el preset de lista, la eleccion manual y ser visible; (2) correr el modelo en LOCAL no arregla la inyeccion de prompt, solo la confidencialidad, y ademas el fallback de `insights` manda a la nube en silencio si LM Studio no esta arriba; (3) los modelos locales del upstream son Apple MLX y no corren aqui, pero el backend local esta fijado a CPU teniendo GTX 1060 con CUDA 12.9 (de ahi la Ola 7, que empieza MIDIENDO). **Debate adversarial: APROBAR CON CAMBIOS, 9 objeciones, 4 ALTAS, todas reconciliadas dentro del plan.** La que lo justifica: yo iba a poner `DICTATION_MODES_ENABLED` en ON por defecto y, como `DEFAULT_MODE_MAP` ya mapea outlook/slack/teams/whatsapp/discord con backend Groq, eso habria mandado a la nube el dictado de mensajeria de cualquiera que actualizara. Descubrible y activado por defecto NO son lo mismo. **Gates ABIERTOS: G1** (como se copia Transform: previsualizar / lista negra / no hacerlo) y **G2** (Hub nativo Qt / QWebEngineView / navegador). : gatillo debate por plan : @fable-5 (dir) + @sonnet-5 (atacante)
- 2026-07-03 Debate adversarial plan Ola 7 (Fable diseñó el plan; Opus 4.8 dirige y 2 adversarios Opus atacan con código real; veredicto: RECHAZAR forma actual → v1 recortada y endurecida). **Cambio de alcance central (flag para Johann):** el briefing OPS se inyecta SOLO en el chat pull (answer_live), NO en el insight stream update_state en v1 → protege las detecciones 5.1 (calibradas a 0 FP en 12 ventanas; Haiku en vivo es susceptible a dilución de atención por 8KB fijos); las tarjetas "🧭 Contexto:" del insight stream pasan a backlog (v1.1) con recalibración bloqueante como gate. Es la contingencia que el propio plan preveía ("si degrada, se apaga por default en el insight stream"), aplicada preventivamente. Objeciones aceptadas: A-1/A-2+válvula (ALTA: avail negativo destruye el transcript con endpoint 18KB+briefing 8KB → briefing en _fixed_prefix CON válvula de sacrificio: se descarta el briefing antes que recortar el transcript reciente; invierte el "nunca recortar briefing" del plan, avalado) · B-Foco2 (ALTA privacidad: modo Silencioso NO gatea el pull, answer_live no consulta get_mode → fuga en pantalla compartida → inyección omitida cuando PROACTIVE_MODE=='silent') · A-4/A-5 (mtime NTFS ~1s + TOCTOU + doble stat → TTL 60s + invalidate() explícito al guardar Ajustes, patrón dictionary.py, sin mtime; build-fuera-del-lock/swap-dentro por I/O potencialmente lenta) · A-1 guard (8KB medido en len(bytes) sobre el contenido LEÍDO, no st_size, para cerrar el TOCTOU de crecimiento) · A-7 (utf-8-sig contra BOM + except Exception amplio para dir/symlink/no-ASCII, todo fail-open) · A-6 (dotenv quota backslashes de rutas Windows → reader normaliza con normpath y tolera comillas; test de round-trip) · A-3 (ubicación system vs user en update_state → MOOT: no se inyecta ahí) · B-persist (briefing nunca en detections_json ni logs; log solo path+bool, jamás el contenido) · B-fallback (INSIGHTS_FALLBACK puede desviar el prompt-con-briefing a groq/openrouter → documentar en help del setting + PARA JOHANN). Refutada/moot: A-8 (inyectar solo en consolidate) y toda la superficie de insight stream, por el recorte a chat-pull. : gatillo §10.2 por ola + kickoff Ola 7 (debate OBLIGATORIO) : @opus-4.8 (dir) + 2×@opus-4.8 (adversarios)
- 2026-07-03 Debate adversarial plan Ola 6 (Fable propone, Opus 4.8 ataca con código real; veredicto: APROBAR CON CAMBIOS; 10/10 ACEPTADAS, 0 refutadas). O1 (ALTA) el texto CRUDO pre-diccionario no existe: transcribe() devuelve ya corregido (transcriber.py:213) y no hay columna raw (ACEPTADA: 6.2a = columna raw_text + migración idempotente + transcribe devuelve (raw, corrected) + db.insert acepta raw_text + toggle NO retroactivo + "Undo" = re-guardar raw como text; toca transcriber.py → colisiona con 6.3) · O2 (ALTA) no existe resolución HWND→proceso en el repo y el HWND se consume en paste_text (ACEPTADA: 6.3 incluye capa Win32 nueva — GetWindowThreadProcessId+OpenProcess+QueryFullProcessImageName — capturando el EXE en save_frontmost_app antes del pegado; mapa exe→preset CONFIGURABLE, no fijo) · O3 (ALTA) LLM post-dictado rompería la latencia del hot-path (ACEPTADA: reformateo en el worker background con pill en PROCESSING + timeout con fallback a texto crudo + claude-cli PROHIBIDO para esta tarea) · O4 (ALTA) "detectar corrección manual" es ciego fuera del dashboard (ACEPTADA: 6.2b acotada al PUT de update_transcription — diff viejo vs nuevo; única señal real) · O5 webhook inline en stop() colgaría el cierre (ACEPTADA: hilo daemon fire-and-forget post-persistencia, patrón _export; timeout corto, máx reintentos con backoff, fallo jamás propaga) · O6 SSRF outbound a URL configurable (ACEPTADA: resolver DNS y bloquear IP privada/loopback/link-local salvo opt-in explícito; https por default; HMAC no sustituye esto) · O7 las 3 unidades chocan en web/server.py: paralelización falsa (ACEPTADA: EJECUCIÓN EN SERIE 6.1→6.2→6.3, corrige al plan maestro que decía "paralelizables por pares") · O8 SAVE_HISTORY=false silencia el webhook (ACEPTADA: no se envía acta en-RAM — coherencia de privacidad — pero se loguea y la UI lo advierte junto al toggle del webhook) · O9 payload sin minimizar (ACEPTADA: shape explícito con selector de alcance — solo pendientes / acta completa —; transcript crudo EXCLUIDO siempre en v1; notas literales documentadas) · O10 requests no es top-level en requirements.in (ACEPTADA: declararlo + regenerar lock; HMAC es stdlib, cero deps nuevas para la firma). : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Debate adversarial plan Ola 5 (Fable propone, Opus 4.8 xhigh ataca; veredicto: RECHAZAR forma actual → rediseñado). O1 (ALTA) pasada detections separada = o un ciclo tarde o 2x cuota claude-cli (ACEPTADA: alternativa de Opus adoptada — detecciones como SEGUNDA INTENCIÓN del mismo update_state, un solo prompt {estado, detecciones}, cero llamadas extra; si la calibración muestra degradación del estado, se separa y se presenta el costo 2x a Johann como decisión) · O2 (ALTA) #pending-cards es un renderer de UNA clase con tipo hardcodeado (ACEPTADA: 5.3 incluye renderer genérico de tarjetas por clase + gating por modo client-side; pendientes migra a la máquina genérica) · O3 (ALTA) meetings_search no expone score bm25 (ACEPTADA: proyectar bm25 AS score — 1 línea — y gatear v1 por overlap de tokens con _tokens existente) · O4 clic derecho del pill es superficie nueva (ACEPTADA: handler nuevo, alcance reconocido) · O5 AltGr+A/M ≡ Ctrl+Alt+A/M puede chocar con IDEs (MITIGADA: no verificable estáticamente; va al caso G2 con instrucción de reporte) · O6 monólogo-en-curso sobre _speech tiene lag 12-22s (ACEPTADA: redefinido sobre _level_* sostenido en el timer de 1s — señal sin lag) · O7 pre-filtro mataba "acuerdo vago" (DISUELTA por O1: la llamada ocurre igual para el estado; las detecciones viajan gratis, sin pre-filtro) · O8 answer_live desde HUD debe ir en thread + señal QueuedConnection (ACEPTADA: especificado) · O9 contención del RLock por el HUD (ACEPTADA: lecturas cortas vía accessors) · O10 toggle de window flags en caliente parpadea (ACEPTADA: flags fijos + SetForegroundWindow explícito para el modo input, patrón clipboard.py). : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Debate adversarial plan Ola 4 (Fable propone, Opus 4.8 high ataca; veredicto: NO APROBAR TAL CUAL → reordenado). O6 (ALTA, el hallazgo estructural): generate_minutes NO tiene presupuesto de contexto (_budget_chars es exclusivo del chat); reunión larga + backend endpoint 18KB → acta vacía por fail-safe, y 4.1/4.3 suman tokens justo ahí (ACEPTADA: unidad nueva 4.0 prerequisito — helper compartido de presupuesto por backend batch + truncado del transcript por el principio con aviso en el prompt) · O1 confiar el "t" crudo al LLM (ACEPTADA: patrón chapters completo — LLM devuelve mm:ss, Python hace _mmss_to_seconds + snap al segmento más cercano) · O2 decisiones como dicts rompe 2 renders web + 2 exports que hacen esc(d)/f-string (ACEPTADA: forma canónica {texto,t?} normalizada en el ORIGEN + tolerancia str|dict en renders porque las actas viejas de la DB siguen siendo strings) · O3 miedo al FTS refutado por Opus: _flatten_str_or_dict_list ya es inmune (BUENA NOTICIA, se descarta) · O4/O5 localStorage para el hotkey = dos fuentes de verdad (ACEPTADA: el último template vive SOLO en MEETING._last_template server-side; el dropdown lo setea por endpoint; el hotkey lee el mismo estado) · O7 "lo humano pesa más" en el system permanente sesgaría actas sin notas (ACEPTADA: la instrucción va en el user-message CONDICIONAL, solo cuando hay notas) · O8 gasto de validación sin tope (ACEPTADA: máx 3 llamadas LLM en 4.1) · O9 confusión de superficie de chips (ACLARADA: los chips del producto SON los del asistente/pestaña Preguntar — ASST_CHIPS_LIVE; esa es la superficie correcta, wording del plan corregido). Orden final: 4.0 → 4.1 → 4.2 → 4.3, en serie. : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Debate adversarial plan Ola 3 (Fable propone, Opus 4.8 high ataca; veredicto: APROBAR CON CAMBIOS, 3 bloqueantes). O1 apply_vad NO tiene singleton de Silero: get_vad_model() construye 2 InferenceSession ONNX en CADA llamada (ACEPTADA: cachear el modelo a nivel módulo en core/vad.py con Lock; apply_vad también lo usa → acelera además el dictado) · O2 coste VAD atrasaría la cola del worker (ACEPTADA vía O1 + medir en fixture) · O3/O4 window_start es tiempo de PARED y los timestamps VAD son MUESTRAS; el loopback WASAPI se salta silencios → ejes por canal divergen y el solape entre canales es ficción (ACEPTADA: anclar por muestras acumuladas POR CANAL; interruptions ELIMINADA de v1 y turns recalculada sobre la alternancia de speaker en los segmentos de TEXTO, documentada como aproximada — RECORTE vs el plan maestro que prometía "turnos e interrupciones (solape)": anotado como G4 para Johann) · O5 meetings_recent sin metrics_json (ACEPTADA: añadir al SELECT) · O6 gap flush→_paused deja frames cruzando la pausa (ACEPTADA: en pause(), _paused=True bajo lock ANTES del flush) · O7 wpm 0/0 y N/0 (ACEPTADA: wpm=None si talk<1s) · O8 lock durante inferencia ONNX congelaría captura+dashboard (ACEPTADA como invariante ejecutable: inferencia fuera del lock, solo el append bajo lock) · O9 fixture TTS demasiado limpio (ACEPTADA parcial: TTS + silencios reales + dropout simulado del loopback por canal) · O10 SAVE_HISTORY=false (ACEPTADA: Stats solo para reuniones persistidas; stop() devuelve metrics en RAM). : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Debate adversarial plan Ola 2 (Fable propone, Opus 4.8 xhigh ataca; veredicto: APROBAR CON CAMBIOS). #1 POST /api/meeting/highlight y /note NO existen, hay que crearlos (ACEPTADA) · #2 levels RMS por polling con _tail_rms bajo el RLock = contención (ACEPTADA con mitigación distinta: cada callback de audio guarda el RMS de SU chunk en un float por canal; status() solo lee 2 floats) · #3/#4 pausa descartando frames rompe timestamps/_carry/duración (ACEPTADA: alternativa de Opus adoptada — congelar reloj con _paused_total, flush de ventana al pausar, flag _paused en ambos callbacks, _elapsed() excluye lo pausado) · #5/#6 visualizador es mono-serie y "Ellos" nunca alimenta viz (ACEPTADA: alcance rebajado — el pill conserva sus barras (mic) y añade indicador cian de actividad "Ellos" alimentado por el RMS del _sys_callback; VU por canal en el header web usa los mismos floats; NO se reescribe AudioVisualizer) · #7 answer()/endpoint acoplados a _db (ACEPTADA: bifurcación presupuestada en 2.3) · #8 IDs de pendientes NO estables entre consolidaciones (ACEPTADA, bloqueante clave: caducidad/dedup de tarjeta keyeada por hash de texto normalizado reutilizando _normalize/_tokens, no por id) · #9 feedback_json sin consumidor en código (MITIGADA: persistir es decisión de producto no re-litigable del kickoff; el bucle de mejora es proceso documentado en PENDIENTES; columna se mantiene por simplicidad) · #10 click-vs-drag del pill requiere press-pos + umbral y señal a main.py (ACEPTADA: umbral ~6px, señal Qt, main abre la URL del dashboard) · #11 2.1 sin la tarjeta de 2.2 no es verificable completa (ACEPTADA: 2.1 entrega placeholder de tarjeta; 2.2 añade ✓/✗+caducidad+persistencia). : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Debate adversarial unidad 1.3 (Fable propone, Opus 4.8 high ataca; veredicto: APROBAR CON CAMBIOS). Objeciones: #1 highlights como kwarg en generate_minutes, nunca posicional (ACEPTADA) · #2 el render del acta son 3 sitios web (renderMinutes, actaHtml, viewer reusa actaHtml) no uno (ACEPTADA: alcance ampliado explícito; alternativa "fusionar highlights en chapters" DESCARTADA: menos explícita, acopla a capítulos LLM y tampoco llega a export/assistant) · #3 auto-repeat de teclado dispararía ~20 highlights/s manteniendo H (ACEPTADA, bloqueante: flag _h_held anti-repeat; el patrón R/T no es copiable tal cual porque son toggles idempotentes) · #4 exponer momentos_destacados en docstring de get_minutes del MCP (ACEPTADA, era-agentica) · #5 meeting_export.py también renderiza campos fijos (ACEPTADA: sección en el .md) · #6 assistant._minutes_to_text ignoraría highlights (ACEPTADA: incluirlos) · #7 AltGr+H y layouts ES/LatAm (ACEPTADA sin cambio: H no es dead-key; va al caso G2) · #8 H cancela el armado del modo 1 (MITIGADA: comportamiento correcto existente, cubierto por test sintético) · #9 no hay regeneración de acta hoy (no aplica). : gatillo §10.2 por ola : @fable-5 + @opus-4.8
- 2026-07-03 Restricciones heredadas del plan (no re-litigadas): mode=ro, WAL+busy_timeout en db/database.py, stdio sin auth de red, reusar db/database.py + FTS5, respetar SAVE_HISTORY. : vienen del debate adversarial del plan (objeción #4) : @fable-5
- 2026-07-03 Debate adversarial del contrato MCP (Fable propone, Opus 4.8 ataca; veredicto: APROBAR CON CAMBIOS). Objeciones: #1 mode=ro crashea sin archivo DB (ACEPTADA: check de existencia en _connect(), error MCP limpio, sin crash) · #2 WAL no garantizado (MITIGADA: PRAGMA WAL incondicional en _init_db en cada arranque de la app; sin app corriendo no hay escritores) · #3 RO+WAL exige dir escribible (MITIGADA: mismo usuario/máquina, documentado) · #4 marcadores snippet (ACEPTADA: solo chr(2)/chr(3)→«», '…' se conserva) · #5 error FTS tragado como [] (ACEPTADA: kwarg raise_errors en meetings_search; MCP lo mapea a error) · #6 faltaba 'citas' y chapters es columna aparte (ACEPTADA: minutes = dict parseado completo passthrough; chapters de chapters_json) · #7 re-parse O(n) por página (MITIGADA: escala local, optimizar sería sobre-ingeniería) · #8 migración parcial a _connect() (ACEPTADA: migración total + grep de verificación) · #9 .mcp.json frágil (ACEPTADA: python del venv, feature dev-only documentada) · #10 FTS stale en RO (ACEPTADA como limitación documentada). Alternativas descartadas: MCP-en-Flask (viola stdio y acopla ciclo de vida), snapshot VACUUM INTO (staleness+complejidad innecesarios). : contrato público nuevo, gatillo §10.2 : @fable-5 + @opus-4.8
