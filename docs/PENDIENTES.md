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
4. **Momentos clave con marca de tiempo / highlights**: marcar automáticamente (o a mano) los
   momentos importantes para saltar a ellos. Hoy hay timestamp por segmento pero no "key moments".
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
