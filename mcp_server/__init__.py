"""Servidor MCP local de Vflow (stdio, read-only).

Expone la memoria de reuniones (tabla meetings + FTS5) a agentes locales
(Claude Code, Levy) sin abrir el dashboard. Feature dev/local: requiere el
venv del proyecto; NO se bundlea en el .exe (main.py no lo importa).
"""
