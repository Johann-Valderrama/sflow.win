"""E2E de Transform sobre selección: el flujo COMPLETO con teclas reales.

Por qué existe, dicho sin adornos: la Ola 3 se verificó con tests herméticos,
guardianes estructurales, mutaciones y cuatro auditores independientes, y aun así
**los tres fallos que importaron los encontró Johann usando la app**. Ninguna de
esas capas podía verlos, porque todas sustituían justo la pieza que fallaba:

  1. El Ctrl+C no copiaba porque el usuario aún sostenía AltGr (los tests
     sustituían `_send_ctrl_c`).
  2. El arreglo de eso desincronizaba el propio listener de atajos (los tests
     sustituían el forzado de teclas).
  3. El panel no recibía el teclado, así que pulsar "7" **borraba el texto
     seleccionado** del usuario (los tests llamaban `keyPressEvent` a mano, que es
     exactamente saltarse el problema).

La lección: en una feature que vive de INYECTAR y RECIBIR teclas del sistema, un
doble de la capa de teclado no verifica nada. Aquí no se sustituye teclado, ni
portapapeles, ni ventanas: lo único que se finge es la llamada al LLM.

TRES CANDADOS, y son la parte importante del archivo
-----------------------------------------------------
La primera versión de este script usó Notepad como ventana de prueba y **escribió
dentro de una nota sin guardar de Johann**: Notepad 11 es de pestañas, así que
lanzarlo se lo entrega a la instancia que ya está abierta, y `GetForegroundWindow`
devolvió una ventana que no era la nuestra. Nunca se comprobó. De ahí:

  1. La ventana de prueba es NUESTRA (un `QTextEdit` de este mismo proceso), no
     una app de terceros. No hay forma de escribir en un documento ajeno.
  2. Antes de teclear se comprueba que la ventana en foco es la nuestra POR HWND;
     si no lo es, ABORTA sin pulsar una sola tecla.
  3. Cada paso re-verifica el foco. Si algo se lo roba a mitad, aborta.

DOS ESCENARIOS
--------------
1. **Transform** (Ola 3): AltGr+X, elegir el prompt con su número, aplicar con Enter.
2. **Command Mode** (Ola 5): AltGr+V, terminar de hablar con Enter, aplicar con Enter.
   Aquí se finge también el micrófono y la transcripción (no hay forma de hablarle a
   un script), pero **no el atajo, ni el foco, ni el teclado del panel**, que es
   justo donde la Ola 3 se rompió cinco veces. Las dos preguntas que solo este
   escenario puede contestar: ¿Windows se quedó AltGr+V, o llegó a la aplicación y
   le destruyó la selección al usuario? ¿y el primer Enter lo recibió el panel, o se
   lo comió el editor?

Diagnóstico de hardware real, igual que `test_loopback.py` y `test_dual_capture.py`:
NO forma parte de la suite automatizada (`pytest.ini` fija `testpaths = tests`).
Se corre a mano y hay que dejarlo trabajar sin tocar el teclado ni el mouse:

    venv\\Scripts\\python.exe test_transform_e2e.py
"""
import ctypes
import ctypes.wintypes
import sys
import threading
import time

import onnxruntime  # noqa: F401  (orden obligatorio antes de PyQt6, ver tests/conftest.py)
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QTextEdit
from pynput.keyboard import Controller, Key

from core import clipboard, transform
from core.global_hotkey import (
    GlobalHotkey, COMMAND_HOTKEY_ID, COMMAND_MODS, COMMAND_VK, COMMAND_LABEL,
)
from ui.transform_panel import TransformPanel

_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]

FRASE = "esto es una prueba de transform con texto real"
RESULTADO_FALSO = "ESTO ES UNA PRUEBA DE TRANSFORM CON TEXTO REAL."
TECLA_PROMPT = "1"          # primer prompt del selector

FRASE_CMD = "esto es una prueba de command mode con texto real"
INSTRUCCION_FALSA = "ponlo todo en mayusculas"
RESULTADO_CMD_FALSO = "ESTO ES UNA PRUEBA DE COMMAND MODE CON TEXTO REAL"


class _AbortarE2E(Exception):
    """La ventana en foco no es la nuestra: no se teclea NADA."""


def _esperar(app, segundos):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        app.processEvents()
        time.sleep(0.01)


def _exigir_foco_propio(hwnd_propio, paso):
    """CANDADO 2 y 3: teclear solo si la ventana en foco es la nuestra."""
    actual = int(_user32.GetForegroundWindow())
    if actual != hwnd_propio:
        raise _AbortarE2E(
            f"[{paso}] la ventana en foco ({actual}) NO es la de prueba "
            f"({hwnd_propio}). Se aborta sin teclear para no escribir en una "
            f"aplicación ajena."
        )


def escenario_transform(app, kb, editor, hwnd_editor) -> int:
    """Ola 3: AltGr+X → elegir prompt con su número → aplicar con Enter."""
    print("\n=== ESCENARIO 1: Transform (Ola 3) ===")
    panel = TransformPanel()
    estado = {"captura": None, "prompt": None, "pegado": None, "hwnd": None}
    hotkey = GlobalHotkey()

    # La captura corre EN UN HILO, igual que main.py (unidad 3a-fix2). No es un
    # detalle del arnés: con `capture_selection` bloqueando el hilo de Qt, Qt no
    # llega a procesar el Ctrl+C inyectado y la captura vuelve vacía. La primera
    # versión de este E2E lo hacía inline y por eso "fallaba" sin que la app
    # estuviera mal. Un E2E que no refleja la arquitectura real mide otra cosa.
    class _Puente(QObject):
        listo = pyqtSignal(dict)

    puente = _Puente()

    def _on_captura(res: dict):
        estado["captura"] = (res["text"], res["status"])
        if res["status"] != "ok":
            return
        estado["hwnd"] = res["hwnd"]
        panel.open_picker(res["text"], [
            {"key": p["key"], "label": p["label"], "cuando": p["cuando"]}
            for p in transform.list_prompts()
        ])

    puente.listo.connect(_on_captura, Qt.ConnectionType.QueuedConnection)

    def _on_hotkey():
        def _worker():
            texto, status = clipboard.capture_selection()
            puente.listo.emit(
                {"text": texto, "status": status, "hwnd": clipboard.get_saved_hwnd()}
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_prompt(key):
        estado["prompt"] = key
        panel.open_waiting(key)
        panel.show_result(transform.transform_text(panel._original, key)["text"])

    def _on_accept(texto):
        estado["pegado"] = clipboard.paste_text(texto, hwnd=estado.get("hwnd"))

    hotkey.activated.connect(_on_hotkey, Qt.ConnectionType.QueuedConnection)
    panel.prompt_chosen.connect(_on_prompt)
    panel.accepted.connect(_on_accept)
    if not hotkey.register(app):
        print("FALLO: Windows negó el registro del atajo (otra app lo tiene tomado)")
        return 1

    try:
        _exigir_foco_propio(hwnd_editor, "escribir la frase")
        editor.setPlainText(FRASE)
        # setFocus() en el WIDGET, no solo activateWindow() en la ventana: medido
        # (banco del scratchpad) que sin foco de teclado en el widget el Ctrl+C
        # inyectado no copia, y la captura vuelve vacía por culpa del arnés.
        editor.setFocus()
        editor.selectAll()
        _esperar(app, 0.4)
        print(f"1. texto seleccionado en la ventana de prueba: {FRASE!r}")

        _exigir_foco_propio(hwnd_editor, "atajo AltGr+X")
        print("2. pulsando AltGr+X DE VERDAD")
        kb.press(Key.alt_gr)
        kb.press("x")
        time.sleep(0.08)
        kb.release("x")
        kb.release(Key.alt_gr)      # el usuario suelta, como en el uso real
        _esperar(app, 2.5)
        # LA comprobación que motivó RegisterHotKey: Windows tiene que haberse
        # QUEDADO la tecla. Si el texto cambió, la "X" llegó a la aplicación y le
        # destruyó la selección al usuario, que es el fallo que se está cerrando.
        tras_atajo = editor.toPlainText()
        if tras_atajo != FRASE:
            print(f"   FALLO: el atajo llegó a la app y alteró el texto -> {tras_atajo!r}")
            return 1
        print("   el atajo NO llegó a la aplicación: el texto sigue intacto")
        print(f"   captura: {estado['captura']}")
        if not panel.is_open():
            print("   FALLO: el panel no se abrió")
            return 1

        hwnd_panel = int(panel.winId())
        _exigir_foco_propio(hwnd_panel, "elegir prompt")
        print(f"3. pulsando '{TECLA_PROMPT}' DE VERDAD sobre el panel")
        kb.press(TECLA_PROMPT)
        kb.release(TECLA_PROMPT)
        _esperar(app, 1.5)
        print(f"   prompt elegido: {estado['prompt']}")

        _exigir_foco_propio(hwnd_panel, "aplicar con Enter")
        print("4. pulsando Enter DE VERDAD para aplicar")
        kb.press(Key.enter)
        kb.release(Key.enter)
        _esperar(app, 3.0)
        print(f"   pegado: {estado['pegado']}")
    except _AbortarE2E as exc:
        print(f"\nABORTADO POR CANDADO: {exc}")
        return 2
    finally:
        hotkey.unregister()
        panel.close_panel()

    _esperar(app, 0.5)
    final = editor.toPlainText()

    print(f"   texto final en la ventana de prueba: {final!r}")
    if final.strip() == RESULTADO_FALSO:
        print("PASA: el flujo completo reemplazó el texto por el resultado.")
        return 0
    print("FALLA.")
    if final.strip() == TECLA_PROMPT:
        print("  -> el número se escribió en la ventana: el panel NO recibió el teclado")
    elif FRASE in final:
        print("  -> el texto original sigue ahí: nunca se aplicó")
    return 1


def escenario_command_mode(app, kb, editor, hwnd_editor) -> int:
    """Ola 5: AltGr+V → hablar → Enter para terminar → Enter para aplicar.

    Se finge el micrófono y la transcripción (no hay forma de hablarle a un
    script), pero NO el atajo, NI el foco, NI el teclado del panel. Lo que este
    escenario contesta y ningún test hermético puede:

      - ¿Windows se quedó AltGr+V? Si llegó a la aplicación, la "V" habría
        reemplazado la selección, que es el fallo que costó cinco intentos.
      - ¿El PRIMER Enter lo recibió el panel? Si el panel no tuviera foco, ese
        Enter caería en el editor y le partiría el texto en dos al usuario.
    """
    print("\n=== ESCENARIO 2: Command Mode (Ola 5) ===")
    panel = TransformPanel()
    estado = {"captura": None, "escuchando": False, "pegado": None, "hwnd": None}
    hotkey = GlobalHotkey(
        hotkey_id=COMMAND_HOTKEY_ID, mods=COMMAND_MODS, vk=COMMAND_VK,
        label=COMMAND_LABEL,
    )

    class _Puente(QObject):
        listo = pyqtSignal(dict)

    puente = _Puente()

    def _on_captura(res: dict):
        estado["captura"] = (res["text"], res["status"])
        if res["status"] != "ok":
            return
        estado["hwnd"] = res["hwnd"]
        estado["escuchando"] = True      # aquí main.py arranca el recorder
        panel.open_listening(res["text"])

    puente.listo.connect(_on_captura, Qt.ConnectionType.QueuedConnection)

    def _on_hotkey():
        # Misma arquitectura que main.py: la captura NO corre en el hilo de Qt.
        def _worker():
            texto, status = clipboard.capture_selection()
            puente.listo.emit(
                {"text": texto, "status": status, "hwnd": clipboard.get_saved_hwnd()}
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_termino_de_hablar():
        """Lo que en main.py es: parar el recorder, transcribir, transformar."""
        estado["escuchando"] = False
        panel.open_waiting(f"«{INSTRUCCION_FALSA}»")
        panel.show_result(
            transform.transform_with_instruction(panel._original, INSTRUCCION_FALSA)["text"]
        )

    def _on_accept(texto):
        estado["pegado"] = clipboard.paste_text(texto, hwnd=estado.get("hwnd"))

    hotkey.activated.connect(_on_hotkey, Qt.ConnectionType.QueuedConnection)
    panel.listening_finished.connect(_on_termino_de_hablar)
    panel.accepted.connect(_on_accept)
    if not hotkey.register(app):
        print(f"FALLO: Windows negó el registro de {COMMAND_LABEL} (otra app lo tiene)")
        return 1

    try:
        _exigir_foco_propio(hwnd_editor, "escribir la frase")
        editor.setPlainText(FRASE_CMD)
        editor.setFocus()
        editor.selectAll()
        _esperar(app, 0.4)
        print(f"1. texto seleccionado en la ventana de prueba: {FRASE_CMD!r}")

        _exigir_foco_propio(hwnd_editor, f"atajo {COMMAND_LABEL}")
        print(f"2. pulsando {COMMAND_LABEL} DE VERDAD")
        kb.press(Key.alt_gr)
        kb.press("v")
        time.sleep(0.08)
        kb.release("v")
        kb.release(Key.alt_gr)
        _esperar(app, 2.5)
        tras_atajo = editor.toPlainText()
        if tras_atajo != FRASE_CMD:
            print(f"   FALLO: el atajo llegó a la app y alteró el texto -> {tras_atajo!r}")
            return 1
        print("   el atajo NO llegó a la aplicación: el texto sigue intacto")
        print(f"   captura: {estado['captura']}")
        if not panel.is_listening():
            print("   FALLO: el panel no entró en modo escucha")
            return 1

        hwnd_panel = int(panel.winId())
        _exigir_foco_propio(hwnd_panel, "terminar de hablar con Enter")
        print("3. pulsando Enter DE VERDAD para terminar de hablar")
        kb.press(Key.enter)
        kb.release(Key.enter)
        _esperar(app, 2.0)
        if estado["escuchando"]:
            print("   FALLO: el Enter no lo recibió el panel; sigue escuchando")
            return 1
        print("   el panel recibió el Enter y cerró la escucha")

        _exigir_foco_propio(hwnd_panel, "aplicar con Enter")
        print("4. pulsando Enter DE VERDAD para aplicar")
        kb.press(Key.enter)
        kb.release(Key.enter)
        _esperar(app, 3.0)
        print(f"   pegado: {estado['pegado']}")
    except _AbortarE2E as exc:
        print(f"\nABORTADO POR CANDADO: {exc}")
        return 2
    finally:
        hotkey.unregister()
        panel.close_panel()

    _esperar(app, 0.5)
    final = editor.toPlainText()

    print(f"   texto final en la ventana de prueba: {final!r}")
    if final.strip() == RESULTADO_CMD_FALSO:
        print("PASA: hablar la instrucción reemplazó el texto por el resultado.")
        return 0
    print("FALLA.")
    if "v" in final and FRASE_CMD not in final:
        print("  -> la 'v' del atajo llegó a la ventana: Windows no lo consumió")
    elif FRASE_CMD in final:
        print("  -> el texto original sigue ahí: nunca se aplicó")
    return 1


def main(cual: str = "todos") -> int:
    """``cual``: ``todos`` (default), ``transform`` o ``command``.

    Se puede pedir uno solo por una razón concreta, no por comodidad: si Vflow está
    CORRIENDO, ya tiene registrado su atajo con Windows y ``RegisterHotKey`` se lo
    niega a este script (el propio mecanismo que hace que el atajo funcione impide
    que dos procesos lo compartan). Poder correr solo el escenario nuevo evita
    tener que cerrarle la aplicación al usuario para verificar una ola.
    """
    kb = Controller()
    app = QApplication([])

    # Lo ÚNICO que se finge de la capa remota: la llamada al modelo. El micrófono y
    # la transcripción del escenario 2 se fingen aparte, en su propio driver.
    transform.transform_text = lambda text, key, **kw: {
        "ok": True, "text": RESULTADO_FALSO, "prompt": key, "backend": "fake",
    }
    transform.transform_with_instruction = lambda text, instruction, **kw: {
        "ok": True, "text": RESULTADO_CMD_FALSO, "prompt": "comando", "backend": "fake",
    }

    # CANDADO 1: la ventana de prueba es de este proceso.
    editor = QTextEdit()
    editor.setWindowTitle("VFLOW E2E — ventana de prueba, no escribas aquí")
    editor.resize(700, 220)
    editor.show()
    editor.raise_()
    editor.activateWindow()
    _esperar(app, 1.0)
    hwnd_editor = int(editor.winId())
    print(f"0. ventana de prueba PROPIA: hwnd={hwnd_editor}")

    try:
        if cual in ("todos", "transform"):
            rc = escenario_transform(app, kb, editor, hwnd_editor)
            if rc != 0:
                return rc
        if cual in ("todos", "command"):
            # El foco vuelve al editor antes del segundo escenario: el pegado
            # anterior lo dejó ahí, pero se re-activa explícitamente para no
            # depender de eso.
            editor.raise_()
            editor.activateWindow()
            editor.setFocus()
            _esperar(app, 0.8)
            return escenario_command_mode(app, kb, editor, hwnd_editor)
        return 0
    finally:
        editor.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "todos"))
