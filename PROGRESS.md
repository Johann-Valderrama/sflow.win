# PROGRESS — Plan por olas Vflow   (branch: windows-variant | últ. checkpoint: 2026-07-03)

## Objetivo / contexto
- Ejecutar las olas de docs/PLAN-OLAS.md. Este archivo es el handoff reanudable (plantilla orquestar-agentes §8).
- Kickoff activo: **Ola 1a — unidad 1.1 Servidor MCP local** (stdio, read-only sobre SQLite, tools search_meetings / get_minutes / get_transcript). Detalle: docs/PLAN-OLAS.md Ola 1.

## En curso
- (nada — Kickoff 1a cerrado; siguiente: Kickoff 1b en ventana propia)

## Completado
- [x] 1.1 Servidor MCP local  (@fable-5, 2026-07-03, commit 527156d)
  - Hecho: debate adversarial del contrato (Opus 4.8, 10 objeciones respondidas) · db/database.py: helper _connect() con busy_timeout 5s en TODAS las conexiones, PRAGMA journal_mode=WAL incondicional en _init_db, modo read_only=True (URI mode=ro, salta DDL/migraciones/backfill, error accionable si la DB no existe), meetings_search(raise_errors=) · mcp_server/ nuevo (FastMCP stdio, tools search_meetings/get_minutes/get_transcript) · .mcp.json (venv python) · mcp==1.28.1 en requirements.in/.txt + requirements.lock regenerado con hashes · CLAUDE.md sección 12.
  - Verificado: smoke unitario de las 3 tools · cliente MCP real (SDK stdio) con la app corriendo Y reunión activa: 3 tools OK, 4 llamadas en 0.02s, captura nunca bloqueada, reunión de prueba cerró limpia (silencio → saved:false, sin fila basura) · escritura concurrente con lector RO en transacción abierta: sin locks, snapshot isolation OK, fila de prueba borrada · RO no puede escribir (OperationalError a nivel SQLite) · grep confirma migración total a _connect().
  - Pendiente manual (Johann): conectar desde Claude Code vía .mcp.json en una sesión nueva y consultar una reunión real.

## Decisiones (append-only)
- 2026-07-03 Restricciones heredadas del plan (no re-litigadas): mode=ro, WAL+busy_timeout en db/database.py, stdio sin auth de red, reusar db/database.py + FTS5, respetar SAVE_HISTORY. : vienen del debate adversarial del plan (objeción #4) : @fable-5
- 2026-07-03 Debate adversarial del contrato MCP (Fable propone, Opus 4.8 ataca; veredicto: APROBAR CON CAMBIOS). Objeciones: #1 mode=ro crashea sin archivo DB (ACEPTADA: check de existencia en _connect(), error MCP limpio, sin crash) · #2 WAL no garantizado (MITIGADA: PRAGMA WAL incondicional en _init_db en cada arranque de la app; sin app corriendo no hay escritores) · #3 RO+WAL exige dir escribible (MITIGADA: mismo usuario/máquina, documentado) · #4 marcadores snippet (ACEPTADA: solo chr(2)/chr(3)→«», '…' se conserva) · #5 error FTS tragado como [] (ACEPTADA: kwarg raise_errors en meetings_search; MCP lo mapea a error) · #6 faltaba 'citas' y chapters es columna aparte (ACEPTADA: minutes = dict parseado completo passthrough; chapters de chapters_json) · #7 re-parse O(n) por página (MITIGADA: escala local, optimizar sería sobre-ingeniería) · #8 migración parcial a _connect() (ACEPTADA: migración total + grep de verificación) · #9 .mcp.json frágil (ACEPTADA: python del venv, feature dev-only documentada) · #10 FTS stale en RO (ACEPTADA como limitación documentada). Alternativas descartadas: MCP-en-Flask (viola stdio y acopla ciclo de vida), snapshot VACUUM INTO (staleness+complejidad innecesarios). : contrato público nuevo, gatillo §10.2 : @fable-5 + @opus-4.8
