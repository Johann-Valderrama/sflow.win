"""Conftest de la suite de Vflow.

Importa onnxruntime ANTES de que cualquier test module cargue PyQt6: en este
entorno (Python 3.14 + PyQt6 + onnxruntime), importar PyQt6 primero hace que
la import de onnxruntime falle con "DLL initialization routine failed", lo que
rompía los tests de VAD/métricas (test_meeting_metrics.py) solo cuando corrían
en la suite completa (test_highlight.py importa core.hotkey → PyQt6 en la
colección). Importarlo aquí primero fija el orden y evita el conflicto.

NOTA (hallazgo 3.1): el mismo conflicto aplica potencialmente al proceso de la
app (main.py carga PyQt6 antes que cualquier VAD); apply_vad hace fail-open,
pero las métricas de reunión quedarían vacías si onnxruntime no importa. Ver
reporte de la unidad 3.1 — decisión del orquestador, fuera del alcance aquí.
"""
try:
    import onnxruntime  # noqa: F401
except Exception:  # noqa: BLE001
    # Sin onnxruntime los tests de audio se saltan/fail-open por su cuenta.
    pass
