# Ola 4 — Diseño de extracción del monolito `web/server.py` (unidad 4.0)

> Propuesta del director (Opus 4.8) para el debate adversarial obligatorio. Base: 4 destilados de
> reconocimiento (2026-07-12, R1 mapa web/server.py · R2 inventario os.getenv · R3 impacto PyInstaller
> · R4 Apéndice C + contrato macrosistema). Alcance D1:B = COMPLETO (4.1 + 4.2 + 4.3 + abrir debate 4.4).
> **Nada de esto se codea hasta reconciliar el debate por escrito en PROGRESS.md.**

## Hechos verificados (no re-litigar; salen del código real)

- `web/server.py` sirve 2 páginas HTML como **raw strings** vía `render_template_string` (ya es motor
  Jinja2): `HTML_TEMPLATE` (líneas 227-2975, ~2748) y `MEETING_PAGE` (3014-3885, ~871). Entre ambas
  (2977-3012) vive lógica de servidor (`_is_local_url`, `_csrf_check`) — NO arrastrarla al archivo de template.
- **Cero** `{{` / `{%` / `{#` literales en ambos templates (grep exhaustivo) → migrar a archivos `.html`
  con `render_template` no rompe per se. La seguridad hoy es disciplina manual + comentario (línea 195).
- **3 puntos de inyección** server→cliente, todos por `.replace()` de placeholders `__TOKEN__`:
  1. `_MT_INCREMENTAL_JS` (197-225): fuente única compartida, inyectada **a module-load** en AMBOS
     templates (líneas 2224 y 3350). Test guardián `tests/test_meeting_incremental.py::TestHelperPresentInBothDocuments`.
  2. `__MEETING_TEMPLATE_OPTIONS_HTML__` (3045): **request-time** en `_meeting_page_html()`.
  3. `__MEETING_TEMPLATES_CHIPS_JSON__` (3750): **request-time** (json.dumps).
  - `HTML_TEMPLATE` NO tiene inyección por-request; el puerto NUNCA se inyecta (cliente usa URLs relativas).
- **50 rutas** `@app.route` (no 112 como estimó el plan). Agrupables: páginas HTML (3), dictados/historial
  (7), ajustes+keys (7), diccionario (9), cola URL (4), reunión en vivo (9), reuniones historial (8),
  asistente (1), instagram+youtube (2).
- **CSRF** `@app.before_request` global (`_csrf_check`, 2997-3011): protege TODOS los métodos mutantes.
  Global a la app, NO por blueprint.
- **Estado global de módulo** (sin encapsular): `_db=TranscriptionDB()` (185), `MEETING` (import 13),
  `PROACTIVE/_proactive` (18), `_download_lock/_download_state` (25-31), `_url_worker_lock/_started`
  (38-39), `_ENV_PATH` (33), `app.config["SECRET_KEY"]` (182, regenerado por proceso).
- **Worker de cola URL**: daemon arrancado desde `start_web_server()` (5080), NO por import de blueprint.
- **Puerto libre**: `_find_free_port(5678, 50)` (5064-5073); `start_web_server` lanza `app.run` en thread daemon.
- **Flask**: `Flask(__name__, static_folder=WEB_STATIC_DIR, static_url_path="/static")` (180).
  `template_folder` NO se pasa hoy. `config.WEB_STATIC_DIR` ya resuelve dev/bundle vía `_RESOURCE_DIR`.
  `jinja2`/`markupsafe` ya en hiddenimports del spec.
- **vflow.spec datas**: `('web/static','web/static')` (línea 33). NO hay templates. Patrón replicable.
- **config.py**: `_RESOURCE_DIR` (dev=repo / bundle=`_MEIPASS`), `WEB_STATIC_DIR` (150). No hay `WEB_TEMPLATES_DIR`.
- **4.3 — hallazgo mayor (R2)**: **14 constantes de `config.py` están MUERTAS** (nunca importadas;
  el comportamiento real lo gobiernan lecturas `os.getenv` perezosas y duplicadas en `core/*`/`web/server.py`):
  `GROQ_API_KEY, TRANSCRIPTION_BACKEND, LOCAL_WHISPER_MODEL, LOCAL_MODEL_IDLE_MINUTES, GROQ_FALLBACK,
  AUDIO_SOURCE, VAD_ENABLED, INSIGHTS_ENABLED, INSIGHTS_BACKEND, INSIGHTS_MODEL, INSIGHTS_ENDPOINT_URL,
  INSIGHTS_ENDPOINT_KEY, INSIGHTS_ENDPOINT_MODEL, INSIGHTS_ENDPOINT_MAX_TOKENS`. Además `WHISPER_LANGUAGE`
  en config.py es un **literal hardcodeado "es"** (no lee entorno) mientras el env var homónimo SÍ se lee
  perezoso en 5 sitios. ~68 vars únicas: ~20 estáticas, ~48 perezosas. Bugs latentes anotados aparte
  (no son de 4.3): `LOCAL_WHISPER_MODEL` no recarga el backend al cambiar; `CLAUDE_CLI_MODEL_BATCH/_LIVE`
  inconsistente en `insights.py:218` vs `574-577`; 3 vars sin documentar (`OPENROUTER_BASE_URL`,
  `OPENROUTER_REASONING_EFFORT`, `ASSISTANT_CONTEXT_BUDGET_CHARS`).

## Criterios de éxito (los dos, no negociables)

1. **Equivalencia observable**: la UI servida es idéntica antes/después POR VISTA. Verificación:
   navegador con estado COMPUTADO (getComputedStyle, no solo classList) + pasada de superficie completa
   de cada vista tocada + shell + overlays con trío oculto/abre/cierra (feedback `verificar-ui-como-el-usuario`).
   Suite pytest verde sobre el baseline de la Ola 2 (617 pass / 0 fail).
2. **Contratos limpios (era-agentica.md)**: el reorden deja cada feature con una interfaz pública mínima
   que puedan consumir por igual UI, MCP y una futura API/CLI de agentes (4.5). En concreto: tras 4.2 los
   handlers son shims delgados sobre la lógica de negocio, y el reorden NO estorba la extracción futura de
   `core/context_pack.py` (prerequisito duro de 4.5, contrato macrosistema O8).

## Propuesta por unidad

### 4.1 — Extraer el frontend a `web/templates/` (Jinja2) + assets

- Crear `web/templates/dashboard.html` (de `HTML_TEMPLATE`) y `web/templates/reunion.html` (de `MEETING_PAGE`).
- `config.py`: añadir `WEB_TEMPLATES_DIR = os.path.join(_RESOURCE_DIR, "web", "templates")` (espejo exacto
  de `WEB_STATIC_DIR:150`).
- `web/server.py:180`: pasar `template_folder=WEB_TEMPLATES_DIR` al constructor Flask.
- `vflow.spec`: añadir `('web/templates','web/templates')` a `datas` (mismo patrón que static).
- **Inyecciones → Jinja limpio**:
  - `_MT_INCREMENTAL_JS`: extraer a un **partial compartido** `web/templates/_mt_incremental.html`
    incluido con `{% include %}` en ambas páginas → el invariante "fuente única en ambos documentos" se
    vuelve **estructural** (sin `.replace()` a module-load). Actualizar el test guardián para verificar la
    presencia en el HTML **renderizado** de ambas rutas (no en las constantes Python, que dejan de existir).
  - `__MEETING_TEMPLATE_OPTIONS_HTML__` y `__MEETING_TEMPLATES_CHIPS_JSON__`: pasar como **variables de
    contexto** a `render_template("reunion.html", template_options_html=..., chips_json=...)`. El JSON se
    inyecta con `{{ chips_json|safe }}` (ya es json.dumps, seguro) en el punto exacto de hoy.
  - **Protección `{% raw %}`**: envolver los bloques grandes de JS/CSS inline en `{% raw %}...{% endraw %}`,
    dejando FUERA solo los ~3 puntos de inyección Jinja. Esto blinda contra que una edición futura
    introduzca `{{`/`{%` literal en JS y rompa el render en silencio (hoy la única defensa es un comentario).
- **CSS/design system**: v1 mantiene el `<style>` inline DENTRO de cada template `.html` (el premio es
  sacar 3600 líneas de `.py`; separar el CSS a `.css` externo es un segundo salto más riesgoso — FOUC,
  rutas de bundle, cache — con beneficio marginal). Se anota "extraer CSS a static" como follow-up, NO en
  esta unidad. *(Punto para el adversario: ¿el plan exige el CSS externo ya? Justificar diferirlo.)*
- **Granularidad de commit**: propuesto **2 commits** (uno por página) + 1 commit de plumbing
  (config.WEB_TEMPLATES_DIR + template_folder + spec + partial compartido + test retargeteado) que va
  PRIMERO. Total 3 commits para 4.1. *(El plan permite "1 commit por vista si el debate lo aprueba".)*
- Método incremental seguro: extraer una página, verificar en navegador que sirve idéntico, commitear;
  luego la otra. Nunca las dos a la vez sin verificar entre medias.

### 4.2 — Blueprints Flask por feature

- Nuevo paquete `web/blueprints/` (o `web/routes/`): `pages.py` (/, /reunion, /logo), `transcriptions.py`,
  `settings.py` (settings+keys+microphones+local-model), `dictionary.py`, `url_queue.py`, `meeting.py`
  (/api/meeting*), `meetings.py` (/api/meetings* + chat), `media.py` (instagram+youtube).
- **Módulo de estado compartido** `web/state.py`: dueño ÚNICO de `_db`, el import de `MEETING`/`PROACTIVE`,
  `_download_lock/_download_state`, `_ENV_PATH`, y los helpers privados de settings (`_set_env_key`,
  `_save_secret_key`, `_validate_*`, `_safe_int_env`, `_blacklisted_export_roots`). Los blueprints IMPORTAN
  de aquí — **jamás reinstancian** `TranscriptionDB()` (rompería el singleton → conexiones/candados divergentes).
- `web/server.py` queda como **app factory + shim**: `create_app()` instancia Flask (template_folder/
  static_folder), registra el `@app.before_request` CSRF **una sola vez a nivel de app** (NO por blueprint;
  un `@bp.before_request` por error dejaría otros blueprints sin CSRF), registra los blueprints, y
  `start_web_server()` conserva el arranque del worker + `_find_free_port` + `app.run` en thread.
- **CSRF y helpers transversales** (`_is_local_url`, `_LOCAL_HOSTNAMES`, `_csrf_check`) van a
  `web/state.py` o `web/security.py`, NO a un blueprint.
- Los handlers NO cambian de lógica, solo de casa (contrato del plan). Donde un handler tenga lógica de
  negocio reutilizable, se deja como función delgada llamable; la extracción a servicios `core/<feature>/`
  es territorio de 4.4, no de 4.2. *(Punto para el adversario: ¿4.2 debe ir más lejos hacia servicios para
  cumplir el criterio agent-ready, o basta con handlers delgados + 4.4 hace la capa de servicio?)*

### 4.3 — Catálogo único de config

- **NO** tomar `config.py` como fuente de verdad ciega: 14 de sus constantes están muertas y mentirían.
  El catálogo se construye desde los **sitios de lectura reales**.
- **Estáticas** (~20, leídas una vez al arranque): las que YA viven y se usan en config.py se quedan
  (MEETING_CHUNK_*, INSIGHTS_MIN_WORDS/FIRST_WORDS/INTERVAL/CONSOLIDATE_*, retention boot-only, etc.).
- **14 constantes muertas**: propuesto **eliminarlas** de config.py (cleanup neutro de comportamiento:
  nadie las importa; verificar con grep `from config import <X>` por cada una antes de borrar). Alternativa:
  cablearlas correctamente — rechazada porque duplicaría los `os.getenv` perezosos que ya gobiernan. El
  literal-trampa `WHISPER_LANGUAGE="es"` de config.py se elimina o renombra (nombre engañoso).
- **Perezosas** (~48): **preservar la semántica de re-lectura en caliente es sagrado**. Propuesto un patrón
  ligero: un **registro/catálogo documentado** en config.py (tabla de NOMBRE + default + semántica
  estática/perezosa + kill-switch sí/no) que sea la referencia única, y (opción A, mínima) los lectores
  perezosos siguen haciendo `os.getenv` en su call-site pero referencian el nombre/default del catálogo;
  o (opción B, más ambiciosa) helpers `config.get_<x>()` que envuelven `os.getenv` SIN cachear. **Recomiendo
  opción A** para 4.3 v1: evita churnear 48 call-sites en múltiples módulos (que serializa con TODO y
  multiplica el riesgo de romper hot-reload), entrega el valor real (un lugar donde ver todos los knobs) y
  deja la migración a helpers como refinamiento posterior. *(Punto central para el adversario: A vs B.)*
- CLAUDE.md apunta al catálogo. Prueba explícita obligatoria: los flags perezosos (`PROACTIVE_MODE`,
  `PROACTIVE_DETECT_*`, `TRANSCRIPTION_BACKEND`, `AUDIO_SOURCE`, `SAVE_HISTORY`) siguen releyéndose sin reiniciar.

### 4.4 — GATE (encarpetar `core/` por features)

- Alto blast radius. Con D1:B + visión agéntica, el valor está justificado, PERO sigue siendo GATE que
  requiere OK explícito de Johann y su propio debate tras 4.1-4.3. **No se ejecuta en esta ola.** Se deja
  anotado en PROGRESS.md PARA JOHANN con la recomendación (SÍ, como ola nueva) y su dependencia con 4.5
  (`core/context_pack.py` necesita fronteras limpias de `core/`).

## Orden y serialización

- Todo toca `web/server.py` → **4.1 → 4.2 → 4.3 EN SERIE** (hotspot único). Dentro de 4.1, las 2 páginas
  en serie con verificación entre medias. 4.3 puede solaparse conceptualmente pero como toca config.py +
  core/* + web/server.py, va después de 4.2 para no navegar un árbol a medio migrar.
- Recursos únicos en serie: puerto del dashboard, navegador de verificación, DB dev. El verificador levanta
  su PROPIO server fresco DESPUÉS de las ediciones.

## Preguntas abiertas para el adversario (ataca estas primero)

1. **Migración de inyecciones a Jinja**: ¿el partial `{% include %}` + `{% raw %}` es correcto, o hay un
   caso donde `{% raw %}` rompa algo (p.ej. un `{{ }}` que SÍ necesitemos, o interacción con `|safe`)?
2. **CSS inline vs externo en 4.1**: ¿diferir el CSS externo traiciona el criterio del plan o es prudente?
3. **4.2 profundidad**: ¿handlers delgados bastan para el criterio agent-ready, o 4.2 debe crear ya la capa
   de servicio (y entonces se solapa con 4.4)?
4. **4.3 opción A vs B**: ¿el catálogo-registro (A) es suficiente, o el churn a helpers (B) vale el riesgo?
   ¿Eliminar las 14 constantes muertas es seguro o esconde un uso dinámico (getattr) que el grep no ve?
5. **Singleton `_db`/`MEETING`**: ¿`web/state.py` como dueño único cubre todos los import-sites, o hay un
   orden de import circular (blueprints ↔ state ↔ server) que muerda?
6. **Equivalencia**: ¿"HTML idéntico" es verificable de forma fiable, o el render Jinja introduce
   diferencias de whitespace/escape que un diff marcaría y confundiría la verificación?
7. **Fallo más probable** del plan completo y **qué unidad** es la más peligrosa.

---

## RECONCILIACIÓN DEL DEBATE (director Opus 4.8, 2026-07-12) — este es el diseño FINAL a ejecutar

Adversario: Opus 4.8, veredicto APROBAR CON CAMBIOS; 7 objeciones, todas aceptadas (2 no-op/BAJA).
Los cambios de abajo SOBREESCRIBEN la propuesta original donde difieran. El ejecutor sigue ESTO.

**4.1 — Extraer frontend (diseño final):**
1. **Método de extracción (crítico, obj.1):** volcar a disco el **VALOR RENDERIZADO** de la string
   Python, NO copiar el texto fuente. Concreto: tras el module-load, `open("web/templates/dashboard.html",
   "w", encoding="utf-8").write(HTML_TEMPLATE)` (con `_MT_INCREMENTAL_JS` ya inyectado) y análogo para
   `reunion.html`. Motivo: `HTML_TEMPLATE`/`MEETING_PAGE` son triple-quoted NO-raw con 66 sitios `\\u`,
   `\\n`, `\\d`, `\\/`, `\\s` (p.ej. `:3742` `'\\u00f3'`, `:1698` `/^#\\/?/`, `:3197-3236` regex markdown,
   `:3457` `'\\u2014'`); en el VALOR de la string colapsan a un solo backslash (lo que el navegador recibe
   hoy). Copiar el fuente dejaría `\\u00f3` literal → UI con `ó`, bullets `•`, emojis rotos.
2. **Inyecciones → variables de contexto (obj.4), SIN `{% raw %}` general:**
   - `dashboard.html`: archivo estático puro (sin inyección). `render_template("dashboard.html")`.
   - `reunion.html`: 3 variables de contexto — `{{ mt_js|safe }}` (helper compartido, NO `{% include %}`),
     `{{ template_options_html|safe }}`, `{{ chips_json|safe }}`. `render_template("reunion.html",
     mt_js=_MT_INCREMENTAL_JS, template_options_html=..., chips_json=...)`. El `mt_js` va TAMBIÉN a
     `dashboard.html` como variable (ambas páginas lo usan; se pasa a las dos).
   - Se descarta `{% raw %}` (miscount silencioso > beneficio; hoy hay CERO `{{`/`{%` en los cuerpos y
     `${...}` de JS es inofensivo para Jinja). Blindaje futuro = test guardián, no `{% raw %}`.
3. **Test guardián nuevo (reemplaza al `{% raw %}`):** asserta que el HTML renderizado de `/` y `/reunion`
   (a) no contiene `{{`/`{%`/`{#` sueltos, (b) conserva un snippet JS con `${...}` intacto, (c) renderiza
   un carácter acentuado por `textContent`/substring (no computed-style). NO se toca
   `TestHelperPresentInBothDocuments` (obj.7: ya asserta sobre HTML renderizado vía `client.get`; con
   `{{ mt_js|safe }}` sigue conteniendo `fetchMeetingIncremental` y ya no `__MT_INCREMENTAL_JS__` → pasa
   tal cual); solo se le añade el assert de acento.
4. **CSS**: inline dentro de cada `.html` en v1 (el value-dump lo hace natural). Split a `.css` externo =
   follow-up, NO esta unidad.
5. **Plumbing**: `config.WEB_TEMPLATES_DIR = os.path.join(_RESOURCE_DIR,"web","templates")` (espejo de
   `WEB_STATIC_DIR:150`, absoluto → robusto a bundle); `template_folder=WEB_TEMPLATES_DIR` en `Flask(...)`
   (`:180`); `('web/templates','web/templates')` en `vflow.spec` datas. jinja2/markupsafe ya en hiddenimports.
6. **Verificación (obj.6):** DOM + getComputedStyle + substring tests + assert de acento por textContent.
   **PROHIBIDO el byte-diff** como criterio (Jinja mete deltas de whitespace inofensivos). Pasada de
   superficie completa por vista + overlays trío oculto/abre/cierra (verificar-ui-como-el-usuario).
7. **Commits**: plumbing (config+spec+Flask) PRIMERO, luego `dashboard.html`, luego `reunion.html`;
   verificar en navegador ENTRE páginas. 3 commits.

**4.2 — Blueprints (diseño final):**
1. **Re-exportación obligatoria (obj.2, requisito duro):** `web/server.py` DEBE re-exportar todos los
   símbolos que la suite importa por nombre, para no romper la colección de tests. Lista verificada de
   import-sites a preservar: `app`, `_db`, `MEETING`, `PROACTIVE`, `_validate_briefing_path`,
   `_validate_export_dir`, `_process_next_url_item` (usados por `tests/test_dictation_modes.py:399`,
   `test_meeting_retention.py:187`, `test_settings_validation.py:22,117`, `test_sflow.py:499`,
   `test_url_endpoint_errors.py:30`, `test_meeting_incremental.py:40,50,68-71,178,255,264`,
   `test_assistant_live.py:274,298,309`, `test_url_queue_worker.py:23`). Patrón: `from web.state import
   _db, MEETING, PROACTIVE, _validate_briefing_path, _validate_export_dir; from web.blueprints.url_queue
   import _process_next_url_item; app = create_app()`. **Correr la suite ENTRE cada blueprint movido**, no
   al final.
2. `web/state.py` = dueño ÚNICO de `_db=TranscriptionDB()`, imports de `MEETING`/`PROACTIVE`,
   `_download_lock/_download_state`, `_url_worker_lock/_started`, `_ENV_PATH`, helpers de settings/CSRF
   (`_is_local_url`, `_LOCAL_HOSTNAMES`, `_csrf_check`, `_set_env_key`, `_save_secret_key`, `_validate_*`,
   `_safe_int_env`, `_blacklisted_export_roots`). Blueprints importan de aquí; **jamás reinstancian** (obj.
   no-problema: el riesgo no es ciclo sino doble-instanciación del singleton → conexiones/candados divergentes).
3. `create_app()` registra el `@app.before_request` CSRF UNA vez a nivel de app (no por blueprint) +
   registra blueprints. `start_web_server()` conserva worker + `_find_free_port` + `app.run` en thread.
   Confirmado (adversario): un solo hook (`_csrf_check`), cero `after_request`/`errorhandler`/
   `context_processor`/`teardown` → nada oculto que perder.
4. Blueprints: `pages` (/, /reunion, /logo), `transcriptions`, `settings` (settings+keys+microphones+
   local-model), `dictionary`, `url_queue`, `meeting`, `meetings` (+chat), `media` (instagram+youtube).
   Handlers delgados; extracción a servicios `core/<feature>/` es 4.4, no 4.2.

**4.3 — Catálogo config (diseño final):**
1. **Hecho R2 corregido (obj.3):** de las 14 "muertas", `GROQ_API_KEY` y `AUDIO_SOURCE` SÍ se importan en
   `main.py:67` (import sin uso en el cuerpo). Las otras 12 son muertas de import
   (`TRANSCRIPTION_BACKEND, LOCAL_WHISPER_MODEL, LOCAL_MODEL_IDLE_MINUTES, GROQ_FALLBACK, VAD_ENABLED,
   INSIGHTS_ENABLED, INSIGHTS_BACKEND, INSIGHTS_MODEL, INSIGHTS_ENDPOINT_URL, INSIGHTS_ENDPOINT_KEY,
   INSIGHTS_ENDPOINT_MODEL, INSIGHTS_ENDPOINT_MAX_TOKENS`). Antes de borrar CUALQUIERA: grep
   `from config import <X>` + `config\.<X>` + `getattr(config` por cada una. Borrar `GROQ_API_KEY`/
   `AUDIO_SOURCE` exige quitar además su import en `main.py:67` (verificado que el cuerpo no las usa por
   nombre). **4.3 toca `config.py` + `main.py` + tests** — no es 1 archivo.
2. `WHISPER_LANGUAGE="es"` literal-trampa de config.py se elimina (miente frente a 5 `os.getenv`).
3. **Perezosas (~48): opción A + test de sincronía (obj.5).** Catálogo-registro en config.py (NOMBRE +
   default + semántica + kill-switch) COMO fuente de documentación única, y un **test que parsea los
   call-sites reales y falla** si (a) un default en el código diverge del catálogo o (b) aparece un
   `os.getenv` no catalogado. Sin ese test, A es doc que envejece. Los call-sites siguen haciendo
   `os.getenv` en runtime (semántica de hot-reload intacta); NO se churnean los 48 sitios (opción B
   descartada: serializa con todo, riesgo de romper hot-reload por caché). Prueba explícita de hot-reload
   de `PROACTIVE_MODE`, `PROACTIVE_DETECT_*`, `TRANSCRIPTION_BACKEND`, `AUDIO_SOURCE`, `SAVE_HISTORY`.
4. Bugs latentes hallados por R2 NO son de 4.3 (se anotan como higiene, no se arreglan aquí):
   `LOCAL_WHISPER_MODEL` no recarga el backend al cambiar; `CLAUDE_CLI_MODEL_BATCH/_LIVE` inconsistente
   en `insights.py:218` vs `574-577`; 3 vars sin documentar (`OPENROUTER_BASE_URL`,
   `OPENROUTER_REASONING_EFFORT`, `ASSISTANT_CONTEXT_BUDGET_CHARS`) — el catálogo SÍ debe incluirlas.

**Unidad más peligrosa (adversario):** 4.2 (mueve la propiedad de `_db`/`MEETING`/validadores, todos
importados por la suite). Por eso el requisito de re-exportación + suite entre blueprints es duro.
**Fallo más probable:** RED masivo de la suite (import surface de 4.2) + defectos de acento invisibles a
computed-style (escape de 4.1) — ambos blindados arriba.
