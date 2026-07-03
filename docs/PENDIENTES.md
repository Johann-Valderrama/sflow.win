# Pendientes y opciones futuras de Vflow

Última actualización: 2026-06-12. Estado del proyecto: backend local + diccionario + VAD + lock de seguridad commiteados en `windows-variant` y en uso diario vía `run.bat` (modo dev).

## 1. Build del .exe (POSPUESTO deliberadamente)

**Qué es:** generar `Vflow.exe` con `build.bat` (PyInstaller) para usar la app sin venv/consola, con arranque con Windows.

**Cuándo retomarlo** (cualquiera de estas señales):
- (a) Querer usar Vflow en otra máquina.
- (b) Querer compartirlo con alguien.
- (c) El proyecto se estabilice y convenga congelar una versión "de uso diario" separada del código en desarrollo.

**El terreno ya está preparado — checklist al retomarlo:**
1. `build.bat` ya corre `pip-audit` como paso previo; instalar deps con `pip install --require-hashes -r requirements.lock` para build reproducible.
2. `vflow.spec` ya incluye lo del backend local: `collect_all` de `ctranslate2`, `faster_whisper`, `av` (DLLs FFmpeg) y `onnxruntime` (VAD). **Nunca probado en build real** — validar primero.
3. El .exe pesará ~900 MB con las libs locales (antes ~50-80 MB). El modelo NO va dentro: se descarga on-demand a `%APPDATA%\Vflow\models` desde el dashboard.
4. Validar en máquina limpia (o usuario Windows limpio): arranque, modo Groq, descarga de modelo, modo local (el crash de OpenMP se mitigó importando `ctranslate2` antes de PyQt6 en `main.py` — verificar que PyInstaller preserva ese orden), VAD, diccionario.
5. El salto de tamaño puede disparar SmartScreen: mantener `version_info.txt` actualizado; considerar firma de código si se distribuye.
6. Subir `APP_VERSION` en `config.py`.

## 2. Diccionario v2 (cuando haya volumen real de entradas)

El esquema ya está preparado (`source`, `hit_count` en la tabla `dictionary`):
- Orden por frecuencia de uso en la UI (hit_count ya se registra).
- Sugerencias semi-automáticas: detectar candidatos y proponerlos como `source='suggested'` en una bandeja de revisión — **sugerir, nunca auto-aplicar**.
- Selección múltiple / borrado en lote.
- Decidido NUNCA: tags/categorías, selección de vocabulario por contexto/app activa.

## 3. Otras opciones identificadas (sin compromiso)

- **sherpa-onnx** como backend alternativo más ligero si algún día el target es hardware modesto (~800 MB menos de RAM que faster-whisper con small).
- **Modelo `medium`** local: ya soportado por el selector del dashboard; solo descargar si `small` falla con vocabulario difícil (probar antes el diccionario, que suele bastar).
- **Regenerar `requirements.lock`** al añadir cualquier dependencia: `pip-compile --generate-hashes --allow-unsafe --output-file requirements.lock requirements.in` (política: paquetes con >30 días en PyPI).

## Decisiones de diseño que NO revisar sin motivo

- En modo local nada sale a internet; el fallback a Groq es opt-in explícito (`GROQ_FALLBACK`, default false).
- Traducción local solo →inglés (límite de Whisper); otros idiomas requieren Groq.
- Filtro de alucinaciones vive en `core/transcriber.py`, agnóstico al backend, ANTES de los reemplazos del diccionario.
- `import ctranslate2` antes de PyQt6 en `main.py` — no mover (evita crash nativo OpenMP 0xC0000005).

---

# Modo Reunión — estado vs Proactor.ai (jun 2026)

Mapa de funciones de Proactor (páginas /work-meeting, /sales, /business, /education
+ features sueltas) contra lo que Vflow ya tiene y lo que falta.

## Ya implementado ✅ (commits en `windows-variant`)
- Captura dual mic+loopback con diarización por canal (Yo/Ellos). [Proactor: solo mic del browser]
- Transcripción en vivo por chunks con corte por silencio + carryover de prompt.
- Insights en vivo (rolling state, IDs estables, ítems pegajosos): temas, pendientes, propuestas, citas.
  ≈ "Insight Stream" + "AI Advice" + "Puntos Clave Accionables" de Proactor.
- Pendientes/compromisos con responsable + fecha + hora. ≈ "Smart To-do".
- Sección "Próximas reuniones / citas". (Proactor no la separa explícitamente.)
- Detección de cambio de tema (code-side) + consolidación por evento.
- Acta post-reunión (resumen/decisiones/pendientes/propuestas/citas/temas). ≈ "Meeting Wiki".
- Dual backend insights Groq nube / LM Studio local, conmutable en dashboard.
- Export a Markdown con frontmatter (contrato OPS) + ventana dedicada /reunion + historial + borrado.
- Métricas de fluidez (churn/jitter/retracciones) en log.
- Bucle de mejora de prompts: comparar transcript vs acta (datos en DB) y endurecer system prompts.
- YA SUPERA a Proactor: audio/video/YouTube a texto (url_transcribe), 100% local/offline, hotkeys
  globales, sin bot en la llamada, export markdown para OPS.

## Pendiente ⬜ (prioridad alta → baja)
1. **Chat de memoria tipo "Potor"**: preguntar al asistente sobre las reuniones; redactar informes,
   FAQs, traducir, rastrear objetivos; cross-sesión. Diseño acordado: FTS5 de SQLite + cargar wikis
   completos al contexto (NO embeddings al principio). [el más pedido por Proactor]
2. **Búsqueda full-text del historial de reuniones** (FTS5). Prerequisito del chat.
3. **Redacción de entregables** desde la reunión: borrador de email de seguimiento, informe, etc.
   (una llamada LLM sobre el acta/transcript). Encaja con el chat.
4. ~~**Momentos clave con marca de tiempo / highlights**: marcar automáticamente (o a mano) los
   momentos importantes para saltar a ellos. Hoy hay timestamp por segmento pero no "key moments".~~
   **HECHO 2026-07-03** (commit 37bbfd4): AltGr+H marca el momento a mano (marcado automático
   sigue pendiente); ver "Momentos destacados" en CLAUDE.md.
5. **Plantillas por tipo de reunión / "modo agenda"** (ventas con BANT, educación, negocio):
   el diferenciador que Proactor NO tiene bien resuelto. Da contexto al LLM → mejores insights.
6. **BANT auto-extraído** para modo ventas (Budget/Authority/Need/Timeline). Subcaso de plantillas.
7. **Retención/auto-purga de reuniones** (eliminar > N días), como ya existe para dictados.
8. **Backend local embebido** (camino A: llama-cpp-python) cuando haya modelo ganador; hoy se usa
   LM Studio (camino B).
9. **Servidor de inferencia on-prem compartido** para escalar a empresa (portátiles sin GPU);
   el backend `endpoint` ya lo soporta apuntando a una URL OpenAI-compatible en la LAN.
10. **Pre-meeting context pack** (cruzar reuniones previas del mismo cliente/proyecto al iniciar) +
    integración bidireccional con el OPS (dead-drop de packs). Fase avanzada de históricos.

## Descartado / no perseguir (con motivo)
- **Latencia <1s / streaming token-a-token** (claim de Proactor): Groq Whisper no es streaming; el
  chunking ~12-22s con corte por silencio basta para actas. No vale el coste/fragilidad.
- **Investigación proactiva en la web durante la reunión** (Proactor education/sales): rompe el
  modelo local/privado; baja prioridad.
- **Multi-Round Detection explícito** ("el tema resurge N veces"): la consolidación ya fusiona;
  no parece aportar lo suficiente.

## Bucle de mejora de prompts (recurrente)
Tras reuniones reales: leer de la DB `transcript` vs `minutes_json`/`insights_json`, identificar
fallos del modelo pequeño, endurecer los system prompts en `core/insights.py`, re-validar
re-generando el acta sobre el MISMO transcript. (1er ciclo hecho: temas 11→4, pendientes 1→0,
propuestas 9→0 al prohibir "investigar/analizar X" desde contenido descriptivo.)

## Observaciones de la UI real de Proactor (capturas, jun 2026)

Detalles concretos vistos en las capturas de /work-meeting, /sales, /business, /education
que sirven de referencia para diseñar nuestras versiones:

- **Línea de tiempo con momentos clave etiquetados**: barra de tiempo con segmentos
  nombrados por la IA ("Define Promotion Goals", "Opportunity Cost", "Client's Past Pain")
  + timestamp, clicables para saltar al momento. → IMPLEMENTABLE: ya tenemos timestamp por
  segmento; añadir una pasada LLM que agrupe el transcript en "capítulos/momentos" + UI de
  timeline en /reunion. Eleva la prioridad del ítem "momentos clave/highlights".
- **Potor (chat) como panel fijo a la derecha** con: (a) CHIPS de preguntas sugeridas
  ("¿Cuáles son mis action items?", "Lista requisitos del cliente", "¿Qué conceptos clave…?"),
  (b) redacción de ENTREGABLES ("escribe un plan de acción", "escribe email de seguimiento" →
  Potor "revisa reuniones" y arma el draft con asunto). → concreta el ítem "chat de memoria".
- **Pestañas del panel**: Insights | AI Advice | Key takeaways | To-do List | Transcribe (o Wiki).
  Nosotros apilamos todo en Foco/Revisión; evaluar separar en pestañas para menos ruido visual.
- **"Expand details" por insight**: tarjeta colapsada (El Qué) que se expande al razonamiento
  (El Por Qué). + botones 👍/👎 de feedback por insight (señal para el bucle de mejora).
- **Diarización multi-hablante con NOMBRES** (Clara, Alex, David, Professor) + avatares. Nosotros
  hacemos 2 canales (Yo/Ellos) por diseño; nombrar a 3+ remotos requeriría diarización ML
  (descartado por coste). Mantener Yo/Ellos salvo demanda fuerte.
- **BANT Summary** estructurado (Budget/Authority/Need/Timeline) como insight de ventas → confirma
  el ítem "plantillas por tipo de reunión".
- **Colaboración**: "Shared with me" + "Share the Record" (compartir grabaciones con el equipo).
  Para empresa; nuestro equivalente local sería el export markdown al OPS compartido.
- **Educación — "Explora más allá del aula"**: Potor sugiere ENLACES EXTERNOS (YouTube, artículos)
  sobre el tema. Rompe el modelo 100% local/privado; baja prioridad (opt-in si acaso).
- **Editar título de la reunión** (✏️ junto al nombre) y **"Upload Media"** (subir audio/video a
  transcribir) — ya tenemos lo segundo vía url_transcribe/AUDIO_SOURCE.

### Prioridad refinada tras ver la UI
1. Chat de memoria "Potor" con chips sugeridos + redacción de entregables (email/informe/plan).
2. Línea de tiempo de momentos clave (capítulos etiquetados) + saltar al momento.
3. Búsqueda full-text del historial (prerequisito del chat).
4. Plantillas por tipo de reunión (ventas/BANT, educación, negocio) / modo agenda.
5. "Expand details" + 👍/👎 en insights (UX + alimenta el bucle de mejora de prompts).

---

# Modo Reunión — estado vs Tactiq (jul 2026)

Recorrido de la app real (app.tactiq.io, cuenta gratuita de Johann, sesión del 2026-07-03).
Tactiq: extensión de Chrome que captura Meet/Zoom/Teams/Webex web (sin bot en la llamada),
app de escritorio para Mac, dashboard cloud. Free tier: 10 reuniones/mes + 5 créditos de IA.

## Inventario de funciones observadas en Tactiq

- **Workspace por reunión con 4 pestañas**: Chats de IA (hilos guardados, varios por reunión,
  con 👍/👎, copiar, descargar, borrar) | Transcripción (búsqueda interna, hablantes con
  timestamp, caja "pregúntale a la IA" al pie) | Flujos de trabajo | Notas y comentarios
  (notas libres + comentarios con @menciones).
- **Estadísticas de participantes**: dona con % de tiempo hablado por persona + duración.
- **Chat de IA global cross-reunión**: seleccionas N reuniones y chateas sobre el conjunto,
  con chips rápidos (Resumen, Elementos de acción, Ideas clave) y selector de idioma.
- **Biblioteca de prompts** ("Herramientas de IA"): guardar/organizar prompts favoritos en
  grupos, compartibles con equipo, pestaña "Explorar" comunitaria.
- **Flujos de trabajo (su feature bandera)**: builder no-code con disparador (al terminar
  reunión / manual), pasos encadenados con paso de confirmación opcional, historial de
  ejecuciones. Plantillas: email de resumen a participantes, guardar en GDrive/OneDrive,
  ticket en Linear, página en Notion, stand-up diario, extraer action items, notas 1:1,
  performance review. Integraciones vivas: Slack, email, Notion, Linear, HubSpot, Pipedrive,
  GDrive, OneDrive, Dropbox, Zapier; en waitlist: Jira, GitHub, ClickUp, Asana, Salesforce...
- **Servidor MCP propio** (banner "Conectar Tactiq con Claude (beta)"): expone la biblioteca
  de reuniones a Claude/ChatGPT (buscar y recuperar resúmenes desde el asistente).
- **Informes**: tabla de reuniones (fecha, duración, nº hablantes, plataforma) con filtros y
  export CSV.
- **Ajustes relevantes**: idioma de salida IA (fijo o "el de la reunión"), contexto personal
  para prompts (nombre, rol, campo, caso de uso: moldea los resúmenes), corrección automática
  (pares reemplazar→con, versión simple de nuestro diccionario), etiquetas, color de resaltado,
  notificación de transcripción a participantes (consentimiento), "preguntar antes de
  transcribir", guardar el chat junto a la transcripción.
- **Importar histórico**: grabaciones de Meet desde Google Drive; subir transcripción o
  grabación a mano; Zoom cloud/OneDrive "muy pronto".
- **Espacios** (carpetas de equipo) + "Compartido conmigo" + compartir transcripción con
  participantes automáticamente (de pago).

## Dónde Vflow ya gana ✅

- 100% local/offline posible (Tactiq es cloud puro con cuota; free = 10 reuniones/mes).
- Insight Stream EN VIVO (temas/pendientes/propuestas rodantes): el panel vivo de Tactiq es
  transcript + preguntas on-demand, no insights proactivos.
- Captura dual mic+loopback con Yo/Ellos sin depender de plataforma (Tactiq solo funciona en
  las 4 plataformas soportadas vía extensión; una llamada telefónica o Discord no existe).
- URL transcribe (YouTube/TikTok/IG), diccionario con pin/hit_count/presupuesto (superior a
  su auto-correct plano), acta estructurada con export markdown al OPS.

## Inspiración accionable (prioridad ROI, de mayor a menor)

1. **Servidor MCP local de reuniones** ⭐ el hallazgo de mayor palanca. Exponer
   `search_meetings` (FTS5 ya existe), `get_minutes`, `get_transcript` como MCP stdio sobre la
   misma SQLite. Cualquier agente del OPS (Levy, Claude Code) consulta la memoria de reuniones
   sin abrir el dashboard. Coincide con la regla era-agentica y con el ítem 10 del backlog
   Proactor (integración OPS); Tactiq valida que el mercado va ahí. Costo bajo: SDK MCP python
   + reusar db/database.py.
2. **% de tiempo hablado Yo/Ellos** por reunión: trivial con los segmentos por canal que ya
   existen; métrica en la tarjeta de la reunión (+ posible aviso "hablaste el 80%").
3. **Chips de preguntas sugeridas + prompts guardados** en el chat de memoria: los chips ya
   estaban decididos (backlog Potor #1); Tactiq añade la idea de una mini-biblioteca de
   prompts favoritos del usuario (tabla pequeña en SQLite, UI en el panel del chat).
4. **Acciones post-reunión configurables** (versión esbelta de sus workflows): al generar el
   acta, disparar acciones opt-in: export md a carpeta OPS (ya existe), copiar resumen al
   portapapeles, POST a webhook genérico (cubre n8n/Zapier/lo que sea sin construir 20
   integraciones). NO construir un builder visual: un checklist de 3-4 acciones en Ajustes.
5. **Contexto personal para la IA** (nombre, rol, dominio) inyectado en los system prompts de
   insights/acta/chat: barato y mejora la calidad de pendientes con responsable ("Yo" = Johann).
6. **Notas manuales por reunión**: campo de notas libre junto al acta (columna en la tabla
   meetings + textarea). Complementa lo generado.
7. **Chat guardado junto a la reunión**: persistir los hilos del chat de memoria por reunión
   (hoy el chat es efímero); Tactiq guarda N hilos por reunión con título.
8. **Idioma de salida configurable** del acta/insights (fijo o "el de la reunión"): una línea
   en el prompt + setting.
9. **Etiquetas por reunión**: baja prioridad mono-usuario; el FTS ya cubre encontrar.

## No copiar (con motivo)

- **Builder visual de workflows + catálogo de integraciones**: sobre-ingeniería para un
  usuario; el webhook genérico da el 80%. Revisar solo si Vflow se productiza.
- **Espacios/equipo/compartir con participantes**: multi-tenant cloud; fuera del modelo local.
- **Cuota de reuniones/créditos**: su modelo de negocio, no una feature.
- **Import desde Google Drive**: ya cubierto mejor por url_transcribe + subir archivo.

## Nota de posicionamiento (si Vflow se productiza algún día)

Tactiq cobra $8-16/mes con cuota; Vflow corre ilimitado a ~$0.02/hora de Groq o gratis en
local. El ángulo "tu reunión nunca sale de tu máquina + sin cuota" es diferenciador real
frente a Tactiq/Proactor/Otter, todos cloud con límites en free tier.

---

# Investigación Tactiq + Fireflies (jul 2026) — arquitectura y oportunidad

Investigación web (3 agentes) que complementa el recorrido de la app de Tactiq. Confirma cómo
computan "participantes y estadísticas" y abre una oportunidad concreta para Vflow.

## Arquitectura comparada (por qué importa)

| | Vflow | Tactiq | Fireflies |
|---|---|---|---|
| Captura | Audio real: mic + loopback WASAPI (2 canales) | Extensión Chrome, lee captions nativos, NO graba audio | Bot "Fred" que se une a la llamada (o extensión sin bot en Meet); graba audio+video |
| Hablantes | Yo/Ellos gratis por hardware (canal = hablante) | Nombres reales gratis de los labels de la plataforma | Nombres de plataforma + diarización ML propia |
| Motor | Whisper (Groq / faster-whisper local) | OpenAI sobre captions | Motor propio cloud (DER ~7.2%) |
| Datos | En la máquina del usuario | Cloud (solo texto) | Cloud GCP+AWS US, retención 12m+, cifrado at-rest (no E2EE real) |
| Alcance | Cualquier fuente de audio (curso, Discord, teléfono) | Solo Meet/Zoom/Teams en navegador | Solo plataformas con link web |
| Cuota free | Sin cuota | 10 reuniones + 5 créditos IA/mes | Límite de reuniones/almacenamiento |

Clave: cómo saben "quién habló cuánto" es consecuencia de su arquitectura (viven dentro de la
plataforma y leen captions etiquetados), no de mejor tecnología. Vflow tiene los hablantes
separados por hardware, así que puede calcular lo mismo SIN ML de diarización.

## Oportunidad nueva ⭐ — Panel de conversation intelligence Yo/Ellos (sin ML)

Fireflies vende caro (estilo Gong) analítica de conversación. Con los 2 canales físicos que
Vflow ya captura, casi toda esa analítica sale gratis con VAD por canal + regex sobre el
transcript que ya existe. Ninguna app local/privada lo ofrece.

Algoritmo talk-time (sin diarización, offline, tiempo real):
```
para cada canal c en {yo, ellos}:
    segmentos_c = silero_vad(canal_c)            # silero ya está en el stack (VAD Groq)
    segmentos_c = merge(gap<0.3s) ; filter(dur>=0.2s)
    talk_c = Σ (t_fin − t_ini)
total = talk_yo + talk_ellos
pct_yo = talk_yo/total*100 ; pct_ellos = talk_ellos/total*100
```
Métricas derivadas baratas (todas con lo que ya hay):
- Talk-to-listen ratio = talk_yo/talk_ellos (benchmark venta ~43/57).
- Longest monologue: segmento continuo más largo por canal (bandera si >2 min).
- Turnos / interrupciones: cambios de canal activo; solape = ambos VAD activos a la vez
  (detectable porque los canales son independientes, imposible con audio mono diarizado).
- Filler words ("eh","um","este"): regex por canal / min de habla.
- Preguntas hechas: segmentos que terminan en "?" por canal.

Prioridad: junto al MCP local, es la mejora de mayor palanca. UI: bloque en la tarjeta de la
reunión y en /reunion. Diferenciador: "Gong local y gratis" para 1:1, ventas, coaching.

Diarización multi-hablante (nombrar 3+ en "Ellos") = capa FUTURA opcional: sherpa-onnx (ONNX,
CPU-only, ~6.6 MB) preferido sobre pyannote (PyTorch ~1.5 GB). No perseguir salvo demanda.

## Confirmaciones sobre el backlog

- **MCP local de reuniones**: triple-validado. Tactiq (beta, Business tier + Claude Connector)
  Y Fireflies (servidor MCP oficial en `api.fireflies.ai/mcp`, listado en el directorio MCP de
  Claude, tools get_transcript/get_transcripts/get_user). Ambos líderes ya exponen sus reuniones
  a asistentes de IA vía MCP: la señal de mercado es inequívoca. Recomendación #1 sin cambios.
  Vflow lo haría local (stdio, sin cuenta cloud) sobre la SQLite existente.
- **AI Workflows hacia SaaS** (CRM/Slack/Notion): la capa donde una app local pierde por diseño.
  Fireflies expone GraphQL API + webhooks (POST JSON al terminar la transcripción, firma HMAC
  SHA-256). Respuesta esbelta para Vflow: webhook genérico saliente al generar el acta (no
  builder visual, no 20 integraciones), ya en el backlog Tactiq de arriba.
- Fireflies confirma que **grabar audio/video + soundbites** es un caso que Tactiq NO cubre
  (solo texto). Vflow ya grada audio; guardar el WAV de la reunión + "highlights con timestamp"
  (ítem 4 del backlog Proactor) cubre esto mejor que Tactiq.

---

# UX/UI — benchmarks Granola/Fathom/superwhisper y plan de rediseño (jul 2026)

Lo que faltaba en la investigación Tactiq/Fireflies no eran features: era la categoría UX.
Granola (granola.ai) es EL benchmark de experiencia para apps de reunión sin bot con captura
de audio del sistema (misma arquitectura que Vflow, ya con versión Windows con paridad casi
total). Fathom aporta el patrón de inmediatez; superwhisper/Wispr Flow los patrones de dictado.

## Diagnóstico de la UI actual (código revisado en web/server.py)

- Dashboard: una sola columna max-w-4xl donde los paneles (ajustes, diccionario, atajos, URL,
  historial) se apilan a golpe de toggle en el header. Sin navegación real.
- /reunion: todo apilado en una página (vivo + acta + historial + chat). El momento "en vivo"
  no tiene jerarquía: el estado es una línea text-xs.
- Tipografía: casi todo text-xs (10-12px) en white/40-55 sobre #0a0a0f. Contraste bajo
  uniforme = sensación "triste": el contenido pesa lo mismo que el cromo.
- Estados vacíos: una línea gris ("Inicia una reunión para ver..."). Sin onboarding.
- Sin atajos de teclado en la web, sin command palette, iconografía SVG correcta pero
  botonera plana de iconos al 40%.

## Patrones robables (impacto/esfuerzo, del informe de benchmarks)

1. **"Mis notas" en vivo + Mejorar con IA** (Granola core): panel donde el usuario tira
   bullets durante la reunión; al terminar, el LLM los fusiona con el transcript y el acta
   destaca lo humano. Ya tenemos transcript + insights + LLM: esfuerzo medio, impacto máximo.
2. **Acta instantánea al colgar** (Fathom): generar el acta automáticamente al terminar la
   captura, sin botón. Cero espera percibida.
3. ~~**Atajo Highlight en vivo** (Fathom): AltGr+H marca timestamp; los momentos marcados
   aparecen destacados en el acta. Esfuerzo bajo (es solo marcar tiempo).~~
   **HECHO 2026-07-03** (commit 37bbfd4).
4. **Lupa de trazabilidad** (Granola): cada bullet del acta enlaza al fragmento del transcript
   origen. Confianza barata (tenemos timestamps por segmento).
5. **Plantillas por tipo de reunión** que definen la estructura del acta (ya estaba en el
   backlog Proactor; Granola confirma que es de lo más valorado).
6. **Undo AI Edit / ver crudo** (Wispr Flow): toggle raw/procesado en cada transcripción
   (guardamos el raw: esfuerzo mínimo).
7. **Diccionario que aprende**: al detectar corrección manual de un dictado, sugerir entrada
   (encaja con source='suggested' ya previsto en diccionario v2).
8. **Modos de dictado por app activa** (superwhisper) con 3 presets sensatos por defecto
   (email formal / chat casual / código). Evitar su error: settings infinitos sin defaults.
9. **Command palette Ctrl+K** + estados vacíos con onboarding accionable.
10. **Timestamps clicables** que saltan a la línea del transcript (ya hay flash de segmento
    en /reunion: extenderlo).

## Plan de rediseño UI (orden sugerido)

1. **Jerarquía tipográfica y contraste**: base 14px para contenido, white/85 texto principal,
   white/50 solo para metadatos; acentos por canal (Yo=violeta, Ellos=cian) y por fuente
   (mic/system/url). Es CSS: un día de trabajo, transforma la sensación.
2. **/reunion en dos modos**: "En vivo" (transcript + mis notas + insights, UI mínima estilo
   Granola, VU meters por canal, timer grande) y "Biblioteca" (historial con detalle en tabs:
   Acta | Transcript | Chat | Stats). Durante la reunión, cero features en la cara.
3. **Dashboard con navegación lateral** (Dictados | Reuniones | Diccionario | URL | Ajustes)
   en vez de paneles-toggle apilados: el patrón Tactiq que sí vale la pena copiar.
4. **Tarjeta de reunión** con dona de talk-time Yo/Ellos + duración + badges (conecta con el
   panel de conversation intelligence de la sección anterior).
5. Command palette + atajos web + estados vacíos (con el atajo de grabar como onboarding).

Regla transversal (Granola): durante la reunión, UI mínima; el poder vive después, a un clic.

## Panel en vivo: rediseño push→pull (decisión jul 2026, tras ver el panel de Tactiq)

Problema del panel actual ("Análisis en vivo" en /reunion): el Insight Stream EMPUJA todo
(temas, pendientes, propuestas, citas) a una columna que crece sin parar con el mismo peso
visual. Durante una reunión nadie puede leer un muro que se actualiza; compite con la
conversación por atención. Tactiq/Fireflies/Granola coinciden en el patrón opuesto: en vivo
la IA es PULL (preguntas bajo demanda) y solo lo urgente se empuja.

Rediseño (mockup mostrado en sesión):
- **Dos pestañas** como Tactiq: "En vivo" (transcript Yo/Ellos + campo "mis notas") y
  "Preguntar" (chat con chips scoped a la reunión en curso: "¿Puntos clave hasta ahora?",
  "¿Qué me falta preguntar?", "Pendientes y responsables").
- **Push mínimo**: SOLO pendientes detectados aparecen como tarjeta discreta con ✓/✗
  (el feedback alimenta el bucle de mejora de prompts). Temas y propuestas dejan de
  mostrarse en vivo: van al acta.
- **El motor de insights NO se elimina**: sigue corriendo en background con su rolling
  state; alimenta el acta instantánea al colgar y hace que las respuestas del chat en vivo
  sean casi gratis (el estado ya está computado). Solo cambia QUÉ se muestra.
- **Header con signos vitales**: timer + VU por canal (confianza de que la captura vive).
- **Barra inferior de 4 acciones**: Highlight (AltGr+H) | Nota rápida | Pausar | Terminar.
- El chat "Esta reunión" del asistente (hoy deshabilitado hasta elegir del historial) se
  habilita para la reunión EN CURSO usando el transcript vivo + estado de insights.

## Panel proactivo v2: qué gana el derecho a interrumpir (ideas jul 2026)

Principio: la barra no es "esto es interesante" sino "esto cambia lo que el humano hará en
los próximos 2 minutos". Tres palancas: selectividad (pocas clases de alerta), timing
(cuándo mostrar) y caducidad (las sugerencias expiran, el muro era un problema de
persistencia). Presupuesto de atención explícito: máximo ~1 push cada 5 min salvo pendientes.

Detecciones que SÍ ganan el derecho (usan la asimetría Yo/Ellos, computable sin ML extra):
1. **Pregunta sin responder**: "Ellos" preguntaron algo y ningún segmento tuyo posterior lo
   respondió en N turnos. "Te preguntaron X hace 4 min y quedó abierta."
2. **Compromiso adquirido**: detectar cuando YO me comprometo ("te lo envío mañana") y
   contarlo. Tarjeta discreta + contador; al final "hiciste 5 promesas" va al acta.
3. **Acuerdo vago**: se cerró un tema sin fecha/responsable ("quedamos en eso") → nudge
   "sin fecha ni dueño".
4. **Memoria cruzada en vivo** ⭐ (nadie lo tiene local): FTS del rolling state contra actas
   pasadas; si el tema actual matchea algo previo, tarjeta "El 12/6 se acordó X; esto lo
   contradice / lo retoma". Prerequisito barato: FTS ya existe.
5. **Agenda con reloj**: si hay agenda/plantilla cargada, checklist que se auto-marca por
   tema cubierto; UN solo nudge cuando queda ~20% del tiempo con ítems sin tocar.
6. **Bandeja pasiva de datos duros**: montos, fechas, plazos, nombres que dicen "Ellos" se
   fijan solos en una tray lateral (pull visual, cero interrupción, nadie retiene números).

Timing (fuera de la caja, usa lo que ya tenemos):
- **Sugerir en los silencios**: el VAD por canal detecta lulls en tiempo real (aunque el
  transcript llegue con ~20s de lag). Las sugerencias tipo "podrías preguntar…" se muestran
  SOLO en pausas de conversación, cuando el humano puede leer. Interrumpir mientras hablan
  es tirar la sugerencia.
- **Caducidad**: cada sugerencia expira (~3 min) y desaparece sola. El panel nunca acumula.
- **Susurro en el pill**: el pill flotante (ya existe, always-on-top) muestra un badge de
  1 palabra cuando hay algo interrupt-worthy; el detalle se lee en el panel. Glanceable.
- **Botón "me perdí" (AltGr+M)**: resumen instantáneo de los últimos 2 min para cuando te
  desconcentraste. Pull, pero resuelve el caso real del 90% de "me estoy perdiendo algo".
- **Modos de intensidad**: Silencioso (solo pendientes) / Copiloto (todo lo de arriba) /
  Entrenador (añade coaching: monólogos >2 min, ratio de habla, preguntas hechas).

Post-reunión (cero costo de atención en vivo):
- **"Qué viste tú vs qué vio la IA"**: diff entre mis notas y los insights; enseña qué se
  te escapa y calibra confianza en el panel.
- **Pendientes → OPS**: los compromisos detectados se ofrecen como tareas exportables
  (markdown/webhook/MCP) al ecosistema OPS. Vflow como sensor del sistema de tareas.

NO perseguir: sugerencias de "AI Advice" genéricas cada N segundos (ruido, lo que mató al
panel v1), TTS al oído en vivo (peligroso, distrae), investigación web en vivo (rompe el
modelo local; ya descartado en el benchmark Proactor).
