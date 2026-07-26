---
slug: sflow-win
nombre: Vflow (Sflow.Win)
dominio: app-desktop
estado: activo
ruta: Sflow.Win
stack: [python, pyqt6, groq-whisper, faster-whisper, sqlite, flask, jinja2, mcp]
privacidad: interno
docs_entrada: [README.md, CLAUDE.md]
proposito: Herramienta Windows de dictado por voz (STT) que crecio a asistente/copiloto de reuniones en vivo: dictado (alternativa open-source a Wispr Flow, ~$0.02/hr vs $15/mes) mas transcripcion Yo/Ellos con metricas de conversacion, actas automaticas, deteccion proactiva (pendientes/compromisos/acuerdos), memoria cruzada entre reuniones, chat en vivo, webhook saliente a OPS y servidor MCP local read-only para agentes.
created: 2026-06-19
updated: 2026-07-26
---

App desktop Python madura, con crecimiento fuerte desde junio 2026 (7 olas del PLAN-MEJORAS mas
el HUD de captura en vivo). Ya no es solo dictado: incluye panel de reuniones (`/reunion`) con
transcripcion en vivo, deteccion proactiva con HUD flotante (AltGr+A), asistente que responde
sobre la reunion activa o cruza varias actas, webhook saliente firmado hacia un consumidor OPS
(dead-drop de pendientes en `PENDING_EXPORT_DIR`), y un servidor MCP local (`mcp_server/`) que
expone busqueda/actas/transcript a agentes como Claude Code. Entrada: `README.md` + `CLAUDE.md`
(arquitectura y guia dev, muy detallado por seccion numerada).
