# AUDITORÍA FABLE — Vflow (Sflow.Win) — 2026-07-06

> Revisión multi-arista del proyecto completo (correctitud, seguridad, arquitectura, escalabilidad,
> agent-ready, ROI, mantenibilidad, potencial). Metodología: 6 auditores Sonnet 5 en paralelo por
> zona + verificación adversarial de los hallazgos altos con 3 refutadores de contexto fresco
> (2 Opus 4.8, 1 Sonnet 5). Dos candidatos de severidad alta NO sobrevivieron la refutación
> (un supuesto use-after-free en `local_backend.warmup()` y el supuesto bloqueo total de Flask por
> `/api/youtube-transcript`): no se reportan, y no vale la pena redescubrirlos.
> Lo ya registrado como pendiente conocido (build .exe, retención de reuniones, interrupciones/solape,
> briefing v1.1, 10 tests con mocks rotos por Python 3.14) NO se reporta como hallazgo nuevo.
>
> **Adenda 2ª pasada (mismo día, a pedido de Johann):** se reconcilió el inventario de PLANES
> ESCRITOS del proyecto contra el código real (PRP.md, docs/PLAN-OLAS.md, docs/FASE3_SPEC.md,
> docs/PENDIENTES.md, docs/Inspiracion.md): ver la sección "Planes escritos no ejecutados o
> parciales". Esa pasada además corrigió una oportunidad: la versión inicial proponía "acta
> instantánea al colgar", pero el acta YA se genera automáticamente en stop() (core/meeting.py:609;
> ya lo había atrapado la objeción #2 del debate del PLAN-OLAS). Retirada y reemplazada.

## Veredicto en 3 líneas

El proyecto está notablemente sano para la velocidad a la que se construyó: cero fugas de secretos,
SQL parametrizado, XSS/CSRF sólidos, fail-open disciplinado y un CLAUDE.md que no miente (5/5
afirmaciones muestreadas verificadas). PERO la 2ª pasada de caza directa (Fable) encontró un
agujero sistémico que la 1ª no vio: **el ciclo de vida de la reunión no tiene serialización
start/stop ni token de generación, y una secuencia stop→start rápida — disparable por impaciencia
normal con el backend `claude-cli` lento — corrompe la reunión nueva, filtra el micrófono y cruza
datos entre reuniones en la DB.** Ése es ahora el fuego #1 (Ola 0 del plan).
**LA mayor palanca de mantenibilidad sigue siendo partir el monolito `web/server.py` (4.804 líneas,
~76% frontend inline, 112 rutas) y dejar la suite en verde; pero el ciclo de vida de la reunión y
la pérdida silenciosa de ~60s de dictado se arreglan ANTES, porque corrompen datos del usuario.**

> **Adenda 2ª pasada de caza directa (Fable 5, mismo día).** Tras notar que la 1ª pasada delegó la
> lectura a Sonnet 5, se corrió una segunda ronda con 4 subagentes **Fable** cazando lo no obvio
> (contratos entre módulos, invariantes, timing) en las zonas de mayor riesgo, con la lista de lo ya
> hallado para reportar solo lo NUEVO. Los 4 hallazgos de mayor peso pasaron por refutador fresco
> (2 Opus): los 4 CONFIRMADOS, con 2 sub-claims mal atribuidos descartados. La tabla y los side
> cases de esta pasada están abajo en "Hallazgos 2ª pasada (Fable)".

## Tabla priorizada (ROI/riesgo)

| # | Hallazgo | Evidencia | Sev. | Esfuerzo | Quick win |
|---|---|---|---|---|---|
| 1 | `web/server.py` es un monolito: 4.804 líneas, de las cuales ~3.645 (76%) son dos strings Python con HTML/CSS/JS inline (`HTML_TEMPLATE` línea 145, `MEETING_PAGE` línea 2915) + 112 rutas. Viola frontera entrypoint-shim; cualquier cambio de UI es editar strings sin test posible; es el hotspot que serializa todas las olas | web/server.py:145, 2915 (medido: 4.804 líneas) | Alta (mantenibilidad) | Alto (por fases) | No |
| 2 | Suite de verificación degradada: 10 fallos "preexistentes normalizados" + 8 archivos de test huérfanos en la raíz (870+ líneas: `test_assistant.py`, `test_dual_capture.py`, `test_fts_search.py`, etc.) fuera de `tests/` y no documentados; módulos con 0 cobertura: `core/secrets.py` (DPAPI), `core/url_transcribe.py` (769 líneas), `core/backends/local_backend.py`, todo `ui/` | raíz del repo; tests/ (19 archivos); grep de referencias | Media | Medio | Parcial |
| 3 | Polling del panel en vivo sin incremental: `GET /api/meeting` devuelve TODOS los segmentos en cada poll y hay DOS pollers simultáneos (2000ms y 1500ms). A 3h de reunión: ~148 KB/poll, ~1,2 req/s, cientos de MB por sesión y `sorted()` de toda la lista por llamada. Confirmado por refutador; hoy tolerable en localhost, techo real que crece con la duración | web/server.py:4480-4488, 2123, 3479; core/meeting.py:342-349 | Media | Bajo-medio | Sí |
| 4 | `ProactiveGate` (singleton `PROACTIVE`) sin ningún lock, mutado desde 3 hilos (Qt, insight, consolidación); además TOCTOU entre `should_push()` y `mark_pushed()` (el flag `_insight_running` se suelta en meeting.py:963 ANTES de `_cross_memory_check` en :971, así que sí se solapan). Impacto real verificado: tarjeta proactiva perdida esporádica (carrera `_purge_expired` reasigna `_queue` vs `append`) o doble push cosmético; sin crash (GIL). Fix trivial | core/proactive.py:59-161; core/meeting.py:963-971, 1099-1107, 1236-1243; main.py:1050,1094-1096 | Media | Bajo | Sí |
| 5 | Rutas configurables sin validar: `PENDING_EXPORT_DIR` y `OPS_BRIEFING_PATH` se aceptan como string arbitrario en `/api/settings`. El dead-drop escribe `.md` en CUALQUIER ruta escribible (p. ej. carpeta Startup) si algo compromete el endpoint local; el briefing lee cualquier `.md` legible | web/server.py:4019, 4061, 3982-4081 | Media | Bajo | Sí |
| 6 | `last_failed_recording.wav`: audio crudo de la grabación fallida en texto plano en `%APPDATA%\Vflow\`, sin TTL, sin cifrar, sin borrado tras éxito posterior. Incoherente con la disciplina DPAPI del resto (keys, cookies). Escenario: falla Groq durante una reunión confidencial y la voz queda en disco indefinidamente | main.py:746-754 | Media (privacidad) | Bajo | Sí |
| 7 | Contratos duplicados sin fuente única: `CARD_STYLES` escrito 2 veces a mano (dict Python en ui/hud_widget.py:73 y objeto JS en web/server.py:3146; añadir un tipo de tarjeta exige editar ambos, nada verifica que coincidan) y `_SILENCE_RMS = 0.012` hardcodeado en core/proactive.py:35 en vez de importar `MEETING_SILENCE_RMS` de config.py (si se cambia por .env, proactive queda desincronizado en silencio) | ui/hud_widget.py:73; web/server.py:3146; core/proactive.py:32-35 | Media | Bajo | Sí |
| 8 | Config dispersa: ~90 `os.getenv` fuera de `config.py` (insights.py 25+, web/server.py 20+); `TRANSCRIPTION_BACKEND` releído con su default `"groq"` duplicado en al menos 5 archivos. No hay catálogo ejecutable único; el CLAUDE.md documenta ~45 variables en prosa paralela al código | grep os.getenv (medido); core/transcriber.py, backends/__init__.py, web/server.py | Media | Medio | No |
| 9 | Acoplamiento por dato (regla arquitectura OPS #7): `web/server.py:67` abre `sqlite3.connect(DB_PATH)` crudo y hace `UPDATE url_queue SET status='pending'` directo, bypasseando `db/database.py` que ya expone la familia `url_queue_*`. Única violación real encontrada; invisible a un grep de imports | web/server.py:38-70; db/database.py:490-565 | Baja | Bajo | Sí |
| 10 | `GroqBackend._get_client()` cachea el cliente con la key vieja: cambiar `GROQ_API_KEY` desde el dashboard no tiene efecto hasta reiniciar la app (el singleton nunca se recrea; `release()` es no-op para Groq) | core/backends/groq_backend.py:158-168 | Baja (UX real) | Bajo | Sí |
| 11 | `insights._last_error` es global de módulo compartido entre tareas live y batch sin lock: una llamada batch concurrente pisa el error del insight stream (o lo limpia), y `MEETING._run_insight_update` lo lee asumiendo que refleja SU llamada. El panel puede mostrar "sin error" habiendo error, o un error ajeno | core/insights.py:51, 341-371; core/meeting.py:1014 | Baja | Bajo | Sí |
| 12 | El diccionario personal solo se aplica en la ruta de subtítulos de `transcribe_url`, NO en la ruta de audio (TikTok/IG/YouTube sin subs): las correcciones "escucho X, escribo Y" no corren para la mitad del motor URL | core/url_transcribe.py:730 vs 606-626 | Baja (gap funcional) | Bajo | Sí |
| 13 | Ventana TOCTOU de generación en el dictado: `_chunk_worker` compara `gen != self._generation` ANTES de tomar `_chunk_state_lock`, no atómicamente con la escritura; un chunk de la sesión anterior puede colarse en la nueva si se suelta y re-presiona el hotkey en la ventana de microsegundos | main.py:609-616, 760-772 | Baja | Bajo | Sí |
| 14 | Deuda agent-ready (señalar, no construir): shapes de error inconsistentes entre endpoints (`{"error"}` vs `{"ok":false}` vs `{"ok","item":None}` que no distingue "inactiva" de "fallo"); límite fijo 200 sin cursor en historial; capacidades de control (iniciar/parar reunión, nota, highlight, diccionario, cola URL) solo accesibles por HTTP local sin doc machine-readable. El MCP read-only es decisión correcta; esto es el siguiente escalón SI el copiloto OPS lo pide | web/server.py:4569, 3824; mcp_server/server.py | Baja | Medio | No |

## Side cases encontrados (escenario concreto que los dispara)

1. **Tarjeta proactiva perdida**: el tick Qt de 1s ejecuta `pop_deliverable` → `_purge_expired` reasigna `self._queue = keep` justo cuando el hilo de insights hace `append` sobre la lista vieja; el append cae en la lista descartada y esa detección jamás se muestra (core/proactive.py:122 vs 152).
2. **Doble push en la misma ventana de presupuesto**: consolidación e insight se solapan (meeting.py:963 libera `_insight_running` antes de `_cross_memory_check`), ambos ven `should_push() == True` y encolan; el usuario ve 2 tarjetas donde el diseño promete ~1 cada 5 min.
3. **Texto de la sesión anterior en el dictado nuevo**: soltar el hotkey y volver a presionarlo en la ventana entre el check de `gen` y la escritura del chunk (main.py:609).
4. **Cambio de Groq API key en caliente sin efecto**: guardar una key nueva en el dashboard y seguir dictando; la app usa la key vieja hasta reiniciar (groq_backend.py:158).
5. **Pausa que no parpadea**: el glifo ⏸ del pill depende de `_spinner_angle`, que solo avanza en PROCESSING; durante RECORDING pausado queda congelado y nunca parpadea (ui/pill_widget.py:382).
6. **Detección silenciosamente descartada**: `_detection_key` hashea solo el texto, sin la clase; si el LLM reclasifica la misma frase (compromiso → acuerdo vago) en otro ciclo, el dedup la mata aunque sea una detección distinta (core/meeting.py:1041-1046).
7. **DB corrupta recuperada en silencio**: `_init_db` renombra a `.corrupt` y arranca con DB vacía avisando solo por `logger.warning`; el usuario puede perder el historial sin enterarse hasta días después (db/database.py:194-227).
8. **Webhook duplicado en retry**: si el receptor procesó el intento 1 pero la respuesta llegó tarde (timeout del cliente), el retry reenvía el mismo payload con la misma firma; no hay idempotency-key. Documentar como requisito del receptor (core/webhook.py:220-248).
9. **Breaker compartido entre tareas**: el circuit breaker es por backend, no por tarea; un fallo del insight EN VIVO con groq abre el breaker también para el acta BATCH durante 300s, mandándola a fallback innecesariamente (core/insights.py:118-141).
10. **Contención de CPU con backend local**: `get_backend()` es singleton por proceso; una transcripción de URL larga y un dictado en vivo simultáneos compiten por los mismos hilos CTranslate2 (degradación, no bloqueo; verificado que Flask 3.1.3 corre threaded=True).
11. **Brecha de documentación de privacidad del briefing**: PROGRESS.md ya avisa que con fallback el briefing puede salir por groq/openrouter, pero si `INSIGHTS_BACKEND_BATCH` es directamente un backend cloud, el briefing SIEMPRE sale a esa nube; CLAUDE.md §17 no lo dice en plano (core/assistant.py:481-496).

## Oportunidades de producto (arista 8: opciones para decidir, NO pendientes)

Separadas del diagnóstico. Filtro aplicado: solo lo que refuerza el núcleo (dictado + reuniones
100% locales y privadas, "tu reunión nunca sale de tu máquina", memoria consultable por agentes).

| Oportunidad | Tipo | ROI esperado | Esfuerzo | Por qué potencia el núcleo |
|---|---|---|---|---|
| **1. Marcado AUTOMÁTICO de momentos clave** (hoy el highlight es solo manual con AltGr+H; PENDIENTES.md lo deja explícito: "marcado automático sigue pendiente") | Mejora de función existente | Medio-alto: los momentos importantes quedan en el acta aunque no llegues a tiempo al hotkey; puede viajar como segunda intención del update_state existente (el patrón que ya probó la unidad 5.1 de detecciones), cero cuota LLM extra | Bajo-medio | Refuerza el acta como memoria fiel de la reunión; el highlight manual sigue mandando (lo automático se marca distinto y nunca lo pisa) |
| **2. Chat de memoria CROSS-reunión + redacción de entregables** (el "Potor" del backlog Proactor, ítem #1 más pedido; hoy el chat es por-reunión o en vivo) | Mejora de función existente | Alto: convierte el archivo de reuniones en activo consultable ("¿qué quedó con X cliente este mes?", "redacta el email de seguimiento"); FTS5 y el chat ya existen, falta la capa multi-reunión y 2-3 plantillas de entregable | Medio | Es el paso que hace que grabar reuniones COMPONGA valor con el tiempo en vez de acumular filas; alineado con filosofía (activo que se compone) |
| **3. Retención/auto-purga opcional de reuniones + aviso de DB recuperada** (backlog conocido, lo elevo a recomendación) | Mejora de función existente | Medio-alto en privacidad: hoy los transcripts completos viven para siempre; los dictados ya tienen retención, las reuniones no | Bajo | Coherencia de la promesa de privacidad; y de paso cierra el side case 7 (pérdida silenciosa) |
| **4. Pendientes → OPS** (los compromisos detectados se exportan como tareas consumibles por Levy/OPS; el dead-drop markdown ya existe, falta el contrato de tarea) | Feature nueva (fase avanzada ya prevista en backlog) | Alto para TU flujo: Vflow como sensor del sistema de tareas del OPS; cierra el bucle reunión → compromiso → ejecución | Bajo-medio | Es la integración que ninguna app cloud puede dar (todo local); refuerza a Vflow como órgano del OPS, no app suelta. Requiere gate de contrato (G1) |
| **5. Contexto personal en los prompts** (nombre, rol, dominio del usuario en system prompts de insights/acta/chat; ítem 5 del benchmark Tactiq) | Mejora de función existente | Medio: pendientes con responsable correcto ("Yo" = Johann), actas menos genéricas; es 1 setting + 1 línea por prompt | Mínimo | Calidad de acta/insights sube gratis en cada reunión; complementa el briefing OPS ya implementado |

**Lo que NO recomiendo perseguir ahora** (aunque suene atractivo): exponer más superficie MCP de
control (hallazgo 14) sin un consumidor concreto que lo pida; diarización multi-hablante (ya
descartada con razón); i18n de prompts (techo barato de resolver cuando haya señal real de
productización).

## Hallazgos 2ª pasada (Fable) — lo que la 1ª pasada no vio

Estos son NUEVOS (no duplican la tabla de arriba). Los 4 primeros pasaron verificación adversarial fresca.

| # | Hallazgo | Evidencia | Sev. | Verificado | Esfuerzo |
|---|---|---|---|---|---|
| F1 | **Ciclo de vida de reunión sin serialización start/stop ni token de generación.** `stop()` pone `_active=False` al instante pero tarda segundos-minutos (joins de 120s + acta LLM hasta 180s con claude-cli); durante esa ventana `is_active()` ya es False y ninguna ruta de start consulta `_meeting_stopping`. Un 2º AltGr+R (o "Iniciar" en el dashboard) arranca la reunión B mientras `stop(A)` sigue: `stop(A)` mete el sentinela en el `_transcribe_q` de B (mata su worker), detiene y anula `self._mic/_sys` de B (micrófono grabando contra fuentes muertas / leak), y persiste el acta leyendo el estado ya reseteado de B; además los daemons de insight/consolidación de A (sin id de generación, no joinados) hacen merge sobre el store de B (temas fantasma en su acta) | core/meeting.py:472, 547-584, 950, 975; main.py:822, 828; web/server.py:4494 | **Alta** | CONFIRMADO (Opus). Prob. media: disparable por impaciencia normal con claude-cli | Delicada |
| F2 | **Pérdida silenciosa de un chunk (~60s) en el ensamblado final del dictado.** `_on_hotkey_released` para el chunk timer pero NO joina los `_chunk_worker` en vuelo; `extract_chunk` ya drenó `self.frames` (el audio del chunk vive SOLO en el worker en vuelo); `_transcribe_final` ensambla `_chunk_results` con lo que haya. Si el tramo final corto gana la carrera a un chunk anterior lento (latencia Groq), el texto pegado omite ~60s sin ningún error | main.py:667, 692; core/recorder.py:329, 344; main.py:605-618 | **Alta** | CONFIRMADO (Opus). Prob. baja pero real, invisible (peor que un crash) | Bajo-medio |
| F3 | **SSRF por redirección en el webhook.** `requests.post` sigue redirects por default; `validate_url` solo valida el host original. Un receptor (opt-in, elegido por el usuario) comprometido que responda 307→`http://169.254.169.254/` o IP privada hace que requests re-POSTee el body firmado al destino interno. Fix: `allow_redirects=False` | core/webhook.py:234 | Media-baja | CONFIRMADO (Opus). Gating opt-in + payload sin transcript acotan el daño; hardening válido del anti-SSRF que ya existe | Bajo |
| F4 | **"Deshacer edición IA" siembra una sugerencia de diccionario INVERTIDA.** El undo hace PUT con el crudo; el endpoint corre `suggest_dictionary_pairs(old=corregido, new=crudo)` sin distinguir undo de edición manual → si el diccionario corrigió "clode→Claude", el undo propone "Claude→clode" en la bandeja (100% determinista por undo). Daño efectivo requiere un 2º clic en "Aceptar", pero la sugerencia venenosa siempre se genera | web/server.py:3898-3912, 1363; core/dictionary.py:315 | Media-baja | CONFIRMADO núcleo (Opus); 2 sub-claims del hallazgo original refutados y descartados | Bajo |
| F5 | Duplicación de texto en cada empalme de chunk de URL: el solape de ~2s se transcribe dos veces y `" ".join` no deduplica → frase repetida cada ~240s (~14 veces en 1h) | core/url_transcribe.py:451-469 | Media | Fable (no re-verificado aparte) | Bajo |
| F6 | Un chunk de URL que falla tira TODO el trabajo previo (sin retry por chunk ni resultado parcial); un chunk vacío deja hueco silencioso de hasta 4 min sin marcador | core/url_transcribe.py:462-464, 628-638 | Media | Fable | Bajo-medio |
| F7 | Carrera de `_clean_crypt32_argtypes`: muta el singleton de proceso `crypt32.CryptUnprotectData.argtypes` sin lock, cubriendo la descarga completa; dos descargas concurrentes (worker de cola + POST /api/youtube-transcript, Flask multihilo) se pisan → reaparece "expected LP__DATA_BLOB" intermitente, o argtypes en None permanente | core/url_transcribe.py:134-165, 523 | Media | Fable | Medio |
| F8 | Worker de cola URL marca como `error` el item ANTERIOR (ya `done`): `"item_id" in dir()` reusa la local de la iteración previa; si `url_queue_next_pending()` lanza, `url_queue_set_error` pisa un item que sí se transcribió | web/server.py:119-121; db/database.py:532-538 | Media | Fable | Bajo |
| F9 | Duplicado en `transcriptions` tras crash del worker daemon entre `insert()` y `url_queue_set_done`: al reiniciar, el item vuelve a `pending` y se re-descarga + re-inserta (2ª fila, 2º gasto de API); no hay clave de idempotencia | web/server.py:64-68, 101-108 | Media | Fable | Bajo-medio |
| F10 | AltGr+T y AltGr+R sin guard de auto-repeat (H/A/M sí lo tienen): mantener la tecla un pelín de más repite el toggle a ~30 Hz → thrash de estado, beeps, y con AltGr+R **múltiples `MEETING.stop()` concurrentes** (agrava F1) | core/hotkey.py:192-194, 223-234 | Media | Fable | Bajo |
| F11 | El "timeout duro de 8s" del reformateo de dictado es ilusorio: `future.result(timeout=8)` lanza, pero salir del `with ThreadPoolExecutor` hace `shutdown(wait=True)` que bloquea hasta que el backend termine → el pegado puede tardar decenas de segundos con red colgada (contradice CLAUDE.md) | core/dictation_modes.py:150-155 | Media | Fable | Bajo |
| F12 | `momentos_destacados` es el único bloque del acta que pasa CRUDO del LLM sin validar ni gatear por los highlights reales (los demás bloques sí se reconcilian en Python): si el LLM alucina la clave en una reunión sin highlights, el acta persiste momentos inventados; si devuelve string, `_format_acta` itera caracteres | core/insights.py:1280-1282 | Media | Fable | Bajo |
| F13 | `_truncate_transcript_to_budget` con `allowance<=0` devuelve el transcript COMPLETO (fail invertido): justo cuando la parte fija consume ≥90% del presupuesto (backend endpoint 18KB), el caso que MÁS necesita truncar desborda el contexto → acta vacía o truncado silencioso del final (lo más valioso) | core/insights.py:979-982 | Media | Fable | Bajo |
| F14 | Otros de robustez/menores (Fable, no verificados aparte): `duration` subreportada ~5x en dictados >60s (recorder.py:312-316, solo métricas); Ctrl+Alt+T de otra app dispara grabación pese a ARMING_DELAY (hotkey.py:140-142); MCP degrada a LIKE en la 1ª búsqueda pero reporta `fts:true` (database.py:815, mcp_server/server.py:79); settings sin validar + JS confirma "Guardado ✓" incondicional, un valor no numérico deja GET /api/settings en 500 persistente (web/server.py:4068, 3990, 1863); chips de trazabilidad del acta en vivo apuntan a elemento muerto o a otra reunión (web/server.py:3494-3506); `get_insights()` copia shallow → `json.dumps` puede reventar el insert con "dict changed size" (meeting.py:292-300, 1355); recuperación de DB corrupta no recrea `meetings_fts` (database.py:206-227); overlap de chunk 3x menor en modo system por frames de 342≠1024 (recorder.py:92-93, 323); timeout Groq 10s marginal para chunks de 240s (groq_backend.py:167); longest_monologue inflado por compresión de silencios del loopback (meeting_metrics.py); VTT parser descarta subtítulos que parecen timestamp/número (url_transcribe.py:249-253) | (ver líneas) | Baja-media | Fable | Variado |

**Patrón de fondo que revela esta pasada:** los cimientos (seguridad, SQL, escape) están bien; la
fragilidad está en (a) el **ciclo de vida concurrente** — MeetingSession lanza 4 tipos de hilos de
fondo sin token de generación ni serialización start/stop (F1, F10, y las carreras de ProactiveGate
de la 1ª pasada son la misma familia), y (b) los **contratos de segunda derivada** — flujos nuevos
(undo 6.2, sugerencias, trazabilidad, chunking de URL) que reutilizan endpoints/estados viejos sin
distinguir la intención o sin resetear estado. No es descuido puntual: es la deuda de haber crecido
rápido por olas sin un dueño explícito del ciclo de vida.

## Side cases nuevos (2ª pasada) con el disparador concreto

1. **Reunión nueva muerta + micrófono fugado**: terminas una reunión con AltGr+R (backend claude-cli, el acta tarda ~30-60s), ves el pill "procesando", vuelves a pulsar AltGr+R creyendo que no registró → la reunión nueva no transcribe nada, el micrófono queda capturando contra fuentes anuladas, y la DB guarda una fila cruzada (F1).
2. **Minuto de dictado que desaparece**: dictas más de 60s y sueltas la tecla justo tras un flush con poca voz al final, mientras el chunk anterior aún está en la API con red lenta → el texto pegado omite ese chunk entero, sin aviso (F2).
3. **El diccionario aprende al revés**: corriges a mano una transcripción, luego pulsas "Deshacer edición IA" → aparece una sugerencia que invierte una corrección real del diccionario; si la aceptas, empiezas a romper dictados futuros (F4).
4. **Webhook que llama a tu red interna**: activas el webhook a un receptor que resulta comprometido; su 307 redirige el POST firmado a una IP privada de tu LAN (F3).
5. **Item de la cola marcado mal**: un ítem de URL se transcribe bien, pero un lock transitorio de SQLite en el siguiente ciclo del worker lo marca como "error" en la UI (F8); o si cierras la app entre el insert y el done, al reabrir se re-descarga y duplica (F9).
6. **AltGr+R sostenido un instante de más**: el auto-repeat encadena start/stop de reunión y varios `MEETING.stop()` a la vez, que es justo el gatillo de F1 (F10).

## Planes escritos no ejecutados o parciales (inventario reconciliado contra el código)

| Plan | Estado real verificado | Qué hacer con él |
|---|---|---|
| `PRP.md` | COMPLETED (build fundacional del producto) | Nada; es historia |
| `docs/PLAN-OLAS.md` (olas 1-7) | Ejecutado COMPLETO según PROGRESS.md, con 2 recortes G4 deliberados que siguen abiertos como decisión tuya: (a) interrupciones/solape entre canales (v2 posible anotando timestamps de pared por buffer del loopback), (b) briefing v1.1 (tarjetas "🧭 Contexto" en el insight stream, con recalibración bloqueante de las detecciones 5.1; v2 chat OPS-aware y v3 Engram en backlog). El hotfix shift-stop está hecho pero con la prueba manual tuya pendiente | Los recortes quedan como gates tuyos (ya viven en PROGRESS.md PARA JOHANN); NO los metí al plan de mejoras porque ambos exigen tu sí explícito |
| `docs/FASE3_SPEC.md` | **NO ejecutado (0 de 2 features)**: ni `core/audio_session.py` (Feature A: silenciar el audio del sistema al dictar, pycaw) ni `core/hotkey_config.py` (Feature B: hotkeys configurables desde el dashboard) existen; no hay pycaw en el código. ADVERTENCIA: el spec quedó desactualizado respecto al código de hoy (cita "AltGr+Space" que luego fue AltGr+T, la ruta vieja del proyecto y un web/server.py pre-rediseño) | Absorbido en el plan de mejoras como Ola 6 con gate: primero decides si las 2 features siguen deseadas; si van, se RE-VALIDA el spec contra el código actual antes de codear (no seguirlo a ciegas). Si ya no las quieres, archivar el spec para que ningún agente futuro lo ejecute por error |
| `docs/PENDIENTES.md` | Backlog vivo, bien mantenido (lo hecho está tachado con commit). Ítems SIN ejecutar relevantes: marcado automático de momentos clave (elevado a oportunidad #1), chat de memoria cross-reunión (oportunidad #2), retención de reuniones (unidad 3.2 del plan), pendientes → OPS (unidad 5.4), pre-meeting context pack, agenda con reloj, bandeja pasiva de datos duros, diff post-reunión "qué viste tú vs qué vio la IA", biblioteca de prompts favoritos, chat guardado por reunión, idioma de salida configurable, WAV por canal opt-in (soundbites), selección múltiple en diccionario, backend local embebido (llama-cpp), build .exe (checklist listo, señales de retomar definidas) | Lo que no entró al plan de mejoras se queda donde está (PENDIENTES.md es su dueño); no se pierde nada |
| `docs/Inspiracion.md`, `docs/benchmarks/*` | Material de referencia (capturas Proactor, benchmarks de modelos), no planes | Nada |

## Preguntas abiertas (solo tú puedes responderlas)

1. **¿Vflow sigue siendo herramienta personal o hay intención real de productizarlo/compartirlo en
   6-12 meses?** De esto depende cuánto invertir en el hallazgo #1 (monolito): para uso personal
   basta la extracción mínima (Ola D recortada); para productizar, la partición completa por
   features es prerequisito.
2. **¿Reuniones de más de 3 horas son un caso real tuyo?** Define la prioridad del polling
   incremental (#3): si tus reuniones son de 1-2h, es higiene; si grabas jornadas, es urgente.
3. **¿El build del .exe sube de prioridad?** Sigue pospuesto con criterio, pero los gates físicos
   G2 de las Olas 2-7 siguen sin correr; si Vflow va a otra máquina este trimestre, conviene
   meter el build en el plan.
4. **¿Las reuniones son memoria permanente del OPS o datos con caducidad?** Decide la oportunidad
   #3 (retención) y también si conviene backup automático de la DB (hoy una corrupción la
   renombra y arranca de cero).
5. **¿Quieres las tarjetas "🧭 Contexto" del insight stream (backlog v1.1 del briefing)?** Exige
   recalibrar las detecciones 5.1 (≥12 ventanas midiendo FP y FN); no lo incluí en el plan por
   defecto porque el debate de la Ola 7 lo dejó como decisión tuya.
6. **¿Siguen deseadas las dos features de FASE3_SPEC.md (silenciar audio al dictar, hotkeys
   configurables)?** El spec tiene meses, es anterior al giro hacia reuniones y quedó
   desactualizado. Si sí: Ola 6 del plan (con re-validación del spec). Si no: archivarlo, para
   que ningún agente futuro lo ejecute por error creyéndolo vigente.

---

**Plan ejecutable derivado de este informe**: `PLAN-MEJORAS-2026-07-06.md` (mismo directorio),
en formato plan-por-olas-autonomo, con ruteo de modelos por unidad y gates humanos.
