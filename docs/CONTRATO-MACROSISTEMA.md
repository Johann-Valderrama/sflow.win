# CONTRATO MACROSISTEMA v1 — Vflow como sensor de un business-os
**Fecha:** 2026-07-12 · **Estado:** diseñado y debatido adversarialmente (Fable 5 propuso, Opus 4.8 atacó con el código real: 11 objeciones, todas aceptadas/mitigadas — registro al final). **Implementación: DESPUÉS del reorden (Olas 2-4); este documento es el spec que ejecutan las unidades 5.4 y 4.5.**
**Gates pendientes de Johann:** G1 (Parte A) y G-agent (Parte B) — preguntas exactas al final.

---

## Principios (no re-litigables)
1. **Autosuficiencia**: Vflow funciona 100% sin OPS ni agentes. Todo lo de aquí es opt-in y fail-open (OPS apagado ≠ efecto alguno en Vflow).
2. **Audio crudo JAMÁS sale.** `SAVE_HISTORY=false` se respeta (sin fila en DB no hay export ni contexto).
3. **read-only ≠ confidencial** (O6): el MCP es local/stdio, pero CUALQUIER cliente MCP conectado puede leer TODO el historial de reuniones. Esto se documenta en el README del MCP y en la UI de Ajustes. Backlog v2: flag de "reunión sensible/excluida" por reunión.
4. La conexión (MCP/API/CLI) es table stakes; el diferencial a construir en v2 es la **consolidación** (memoria de trabajo). v1 no promete lo que no hace (O4).

## Parte A — Pendientes → OPS (unidad 5.4): contrato de tarea v1

**Mecanismo:** evolución del dead-drop existente (`core/webhook.py::export_pendientes` → `PENDING_EXPORT_DIR`). Archivo por reunión, **YAML completo** (sin micro-formato con `|`, O10).

**Gate de emisión** (O7): solo se escribe si la reunión tiene **≥1 pendiente**. Reunión sin pendientes = ningún archivo.

**Formato** (`schema_version: 1`):
```yaml
tipo: tarea-vflow
schema_version: 1
origen: vflow-meeting
instalacion: <machine_id corto, estable por instalación>   # O3: meeting_id es local, no global
meeting_id: 26
fecha_reunion: "2026-07-12"
titulo_reunion: "Reunión 2026-07-12 08:42"
generado_en: "2026-07-12 09:00"          # hora local
pendientes:
  - id: "a3f8c1"                          # hash(texto normalizado) — dedup estable entre re-exports y reuniones (O1)
    texto: "Enviar el contrato a Acme"
    responsable: "Johann"                 # texto libre del acta o null — SIN enum yo/ellos (O2)
    due: {fecha: "2026-07-15", hora: null} # la agenda REAL extraída del acta, o null (O2)
    transcript_offset: "04:12"            # mm:ss DENTRO de la grabación; NO es fecha límite (O2) — o null
    contexto: "Se acordó tras revisar el presupuesto"
```

**Naming e idempotencia** (O3): `vflow-pendientes-<instalacion>-<meeting_id>-<hash8-contenido>.md`. Escritura **create-only**: si el archivo con ese nombre exacto ya existe (contenido idéntico por definición del hash) → no-op. Vflow NUNCA edita ni borra archivos del drop. Cambió el acta → cambia el hash → archivo NUEVO; el consumidor deduplica por `pendientes[].id`, no por archivo.

**Ownership**: tras el drop, el archivo es del CONSUMIDOR (agente OPS): lo procesa y lo mueve (p. ej. `procesadas/`). Si hay más de un consumidor, la coordinación es problema del lado OPS (el contrato garantiza solo: archivos inmutables, ids estables).

**Ciclo de cierre**: NO existe en v1 (O5). El consumidor no reporta "hecho" de vuelta. v2 diseñará el canal de retorno (p. ej. `vflow-done-<id>` en subcarpeta que la app lee) — hasta entonces Vflow no sabe qué pendiente se cerró, y por eso NO expone `list_pending_actions` (ver Parte B).

**Vista humana**: el checklist markdown legible actual puede mantenerse como sección `notas:` render-friendly DEBAJO del YAML o como archivo hermano — decisión del ejecutor, el contrato es el YAML.

## Parte B — Superficie agéntica de lectura (unidad 4.5): v1 honesta

**Prerequisito duro** (O8): extraer `core/context_pack.py` como módulo PURO (recibe query+budget, usa la DB read-only, devuelve el paquete) ANTES de exponer nada. MCP y CLI son puertas delgadas sobre esa única función — cero duplicación.

**Tool nueva: `get_related_context(query, budget_chars)`** — nombre honesto (O4): retrieval de contexto RELACIONADO, no "consolidado".
- Devuelve, ensamblado bajo presupuesto: decisiones/acuerdos que matchean (con **fecha de reunión SIEMPRE visible**, ordenados por recencia), pendientes relacionados (con id del contrato A), 1-3 citas con timestamp+meeting_id, métricas si aplican.
- **Orden de sacrificio del presupuesto** (O11), fijo y documentado: `decisiones recientes > pendientes > citas > métricas`. Lo recortado se declara (`truncado: [citas, metricas]`).
- **Señal de cobertura** (O9): la respuesta SIEMPRE incluye `cobertura: {reuniones_indexadas: N, fts_disponible: bool, advertencia: str|null}` — el lector RO no hace backfill del FTS; si puede haber reuniones invisibles, el agente lo SABE en vez de citar una memoria incompleta como total.
- **Documentación explícita en la tool**: "retrieval léxico por relevancia+recencia; NO reconcilia estado (una decisión revertida puede aparecer junto a su reversión — la fecha manda)". La reconciliación de estado (recency-decay, dedup semántico, LLM opcional) es la **unidad v2 "consolidación"** — el foso real, con su propio debate.
- `list_pending_actions`: **NO va en v1** (O5, sin señal de cierre mentiría "todo abierto para siempre"). Entra en v2 junto con el ciclo de cierre de la Parte A.

**Tools existentes**: `search_meetings`/`get_minutes`/`get_transcript` quedan como están (el transcript completo solo por pull explícito paginado).

**CLI** (`vflow-cli`): mismas tools por línea de comandos, montadas sobre `core/context_pack.py` y los helpers existentes del MCP (extraídos de los cuerpos `@mcp.tool()` a funciones compartibles — parte del mismo prerequisito O8).

**Permisos v1**: TODO read-only (mode=ro de SQLite intacto). **Control remoto de la app por agente (start/stop reunión, dictado): NO en v1**; sería v2 con gate propio de Johann.

## Registro del debate (2026-07-12, resumen)
Opus 4.8 (11 objeciones, veredicto APROBAR CON CAMBIOS): O1 sin id de pendiente no hay dedup → ACEPTADA (id=hash) · O2 el frontmatter propuesto tiraba fecha/hora reales y confundía t con deadline; el enum de responsable no existe en el schema real (insights.py:815-880) → ACEPTADA (due + transcript_offset + texto libre) · O3 contradicción create-only vs sobrescribir + meeting_id no es global → ACEPTADA (create-only por hash + machine_id) · O4 "consolidado" sin LLM = ensalada de snippets con decisiones obsoletas arriba → ACEPTADA (renombrar a get_related_context, consolidación = v2) · O5 no existe señal de "pendiente cerrado" en la DB (solo texto en minutes_json) → ACEPTADA (list_pending_actions fuera de v1) · O6 read-only ≠ confidencial, el MCP expone todo el historial → MITIGADA (documentar + backlog flag sensible) · O7 el dead-drop actual escribe también reuniones sin pendientes y solo con meeting_id≠None → ACEPTADA (gate ≥1 pendiente) · O8 el patrón 5.2 no es reutilizable tal cual (métodos acoplados al rolling state) → ACEPTADA (módulo puro prerequisito) · O9 el lector RO no backfillea FTS → ACEPTADA (señal de cobertura) · O10 micro-formato con pipes frágil → ACEPTADA (YAML completo) · O11 presupuesto sin orden de sacrificio → ACEPTADA (orden fijo declarado).

## GATES — RESUELTOS por Johann (2026-07-12, en chat)
- **G1 (Parte A): APROBADO.** Contrato de tarea v1 tal cual. Buzón: **`C:\OPS\_inbox-vflow\`** (creado, con `_LEEME.md` para el consumidor; el ejecutor de 5.4 configura ese path como default sugerido de PENDING_EXPORT_DIR en la doc, sin hardcodearlo).
- **G-agent (Parte B): APROBADO v1 completo** (solo lectura + `get_related_context` + CLI; consolidación, `list_pending_actions` y control remoto = v2). Advertencia de privacidad aceptada; flag de reunión sensible queda en backlog v2.

> Con ambos gates resueltos, las unidades 5.4 y 4.5 NO tienen gate pendiente: el ejecutor va directo
> a implementar contra este spec (después del reorden, como manda el plan).
