import threading
import time
from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal
from config import DOUBLE_TAP_INTERVAL, ARMING_DELAY


class HotkeyListener(QObject):
    """Listener global de teclado con cuatro modos de activación:

    1. Mantener Ctrl+Alt:        hold → transcribir (soltar para detener).
       Tiene un retraso de armado (ARMING_DELAY) para no disparar accidentalmente
       si se usa Ctrl+Alt como parte de un atajo de otra aplicación (ej. Ctrl+Alt+L).
    2. Triple-tap Shift:         toggle manos-libres → transcribir (tap Shift de nuevo para detener).
    3. Mantener Ctrl+Shift+Alt:  hold → traducir al idioma destino
       (Shift debe estar presionado ANTES de Alt). También usa ARMING_DELAY.
    4. AltGr + Space toggle:     manos-libres → traducir (una pulsación inicia, otra detiene).

    NOTA: AltGr se rastrea separado del Alt regular para que AltGr solo NO active Ctrl+Alt (modo 1).
    """

    pressed = pyqtSignal()
    released = pyqtSignal()
    translate_pressed = pyqtSignal()

    def __init__(self):
        """Inicializa el estado de teclas, el timer de armado y la detección de triple-tap."""
        super().__init__()
        self._ctrl_held = False
        self._alt_held = False       # Alt regular (no AltGr)
        self._alt_gr_held = False    # AltGr rastreado por separado
        self._shift_held = False
        self._recording = False
        self._hands_free = False
        self._alt_gr_space_mode = False  # True cuando está en modo toggle AltGr+Space

        self._listener: keyboard.Listener | None = None

        # Detección de triple-tap (Shift)
        self._last_shift_press = 0.0
        self._shift_tap_count = 0

        # Timer de armado diferido para modos 1 y 3 (Ctrl+Alt)
        self._arm_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # Ciclo de vida del listener
    # ------------------------------------------------------------------

    def start(self):
        """Inicia el listener global de teclado en un hilo daemon."""
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        """Detiene y libera el listener de teclado."""
        if self._listener:
            self._listener.stop()
            self._listener = None

    def reset(self):
        """Resetea el estado tras un auto-stop externo (safety timer) para evitar releases espurios."""
        self._cancel_arm()
        self._recording = False
        self._hands_free = False
        self._alt_gr_space_mode = False
        self._shift_tap_count = 0
        self._last_shift_press = 0.0

    # ------------------------------------------------------------------
    # Armado diferido (modos 1 y 3)
    # ------------------------------------------------------------------

    def _cancel_arm(self):
        """Cancela el timer de armado pendiente (si existe) y lo descarta."""
        if self._arm_timer is not None:
            self._arm_timer.cancel()
            self._arm_timer = None

    def _fire_arm(self, translate: bool):
        """Callback del Timer: dispara grabación si Ctrl+Alt siguen sostenidos.

        Corre en el hilo del Timer. Re-verifica el estado para mitigar TOCTOU:
        si el usuario soltó Ctrl o Alt antes de que expirara el timer, no graba.
        El GIL garantiza atomicidad en la lectura/escritura de atributos bool individuales.
        """
        # Siempre limpiar la referencia al timer al final
        try:
            if self._ctrl_held and self._alt_held and not self._recording:
                self._recording = True
                self._hands_free = False
                if translate:
                    self.translate_pressed.emit()
                else:
                    self.pressed.emit()
        finally:
            self._arm_timer = None

    def _is_modifier_key(self, key) -> bool:
        """Devuelve True si la tecla es un modificador relevante (ctrl/alt/altgr/shift/space).

        Las teclas no modificadoras durante un timer de armado activo indican que
        el usuario está ejecutando un atajo de otra app — hay que cancelar el armado.
        """
        return key in (
            keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
            keyboard.Key.alt,    keyboard.Key.alt_l,    keyboard.Key.alt_r,
            keyboard.Key.alt_gr,
            keyboard.Key.shift,  keyboard.Key.shift_l,  keyboard.Key.shift_r,
            keyboard.Key.space,
        )

    # ------------------------------------------------------------------
    # Manejo de eventos
    # ------------------------------------------------------------------

    def _on_press(self, key):
        """Maneja pulsaciones de teclas para los cuatro modos de activación."""
        is_ctrl   = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt    = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r)
        is_alt_gr = key == keyboard.Key.alt_gr
        is_shift  = key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)
        is_space  = key == keyboard.Key.space

        # --- Modo 4: toggle AltGr + Space (traducir, manos-libres) ---
        if is_space and self._alt_gr_held:
            if self._alt_gr_space_mode and self._recording:
                # Segunda pulsación → detener grabación
                self._alt_gr_space_mode = False
                self._recording = False
                self.released.emit()
            elif not self._recording:
                # Primera pulsación → iniciar traducción (sin hold)
                self._alt_gr_space_mode = True
                self._recording = True
                self.translate_pressed.emit()
            return

        # --- Cancelación del armado por tecla no-modificadora ---
        # Si hay un timer de armado activo y llega una tecla que no es un modificador
        # conocido (ctrl/alt/shift/space/altgr), es un atajo de otra app → abortar
        # y salir sin re-armar (return temprano tras cancelar).
        if self._arm_timer is not None and not self._is_modifier_key(key):
            self._cancel_arm()
            return  # tecla de otra app; no re-armar en este evento

        # --- Actualización del estado de modificadores ---
        if is_ctrl:
            self._ctrl_held = True
        elif is_alt:
            self._alt_held = True
        elif is_alt_gr:
            self._alt_gr_held = True
        elif is_shift:
            now = time.time()

            # Ignorar auto-repeat de Windows
            if self._shift_held:
                return
            self._shift_held = True

            # Manos-libres (modo 2): un solo tap de Shift detiene la grabación
            if self._hands_free and self._recording:
                self._hands_free = False
                self._recording = False
                self.released.emit()
                return

            # Detección de triple-tap
            if now - self._last_shift_press < DOUBLE_TAP_INTERVAL:
                self._shift_tap_count += 1
            else:
                self._shift_tap_count = 1
            self._last_shift_press = now

            if self._shift_tap_count >= 3 and not self._recording:
                # Modo 2: triple-tap Shift → transcripción manos-libres
                self._shift_tap_count = 0
                self._hands_free = True
                self._recording = True
                self.pressed.emit()
                return

        # --- Modos 1 y 3: Ctrl+Alt hold con retraso de armado ---
        if self._ctrl_held and self._alt_held and not self._recording and self._arm_timer is None:
            translate = self._shift_held  # Modo 3 si Shift también está presionado
            if ARMING_DELAY <= 0:
                # Comportamiento inmediato (ARMING_DELAY desactivado)
                self._fire_arm(translate)
            else:
                self._arm_timer = threading.Timer(
                    ARMING_DELAY, self._fire_arm, args=(translate,)
                )
                self._arm_timer.daemon = True
                self._arm_timer.start()

    def _on_release(self, key):
        """Detecta liberación de teclas y detiene grabación en modos hold."""
        is_ctrl   = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt    = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r)
        is_alt_gr = key == keyboard.Key.alt_gr
        is_shift  = key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)

        # Actualizar estado de modificadores
        if is_ctrl:
            self._ctrl_held = False
        elif is_alt:
            self._alt_held = False
        elif is_alt_gr:
            self._alt_gr_held = False
        elif is_shift:
            self._shift_held = False

        # Cancelar armado si se sueltan Ctrl o Alt antes de que expire el timer
        if (is_ctrl or is_alt) and self._arm_timer is not None:
            self._cancel_arm()

        # Modo 4 (toggle AltGr+Space): la detención la maneja la segunda pulsación en _on_press.
        # Soltar AltGr NO detiene la grabación — el usuario debe pulsar AltGr+Space de nuevo.
        if self._alt_gr_space_mode:
            return

        # Modos 1 y 3 (hold): detener cuando se suelta Ctrl o Alt
        # Modo 2 (manos-libres): la detención la maneja _on_press (tap de Shift)
        if self._recording and not self._hands_free:
            if not (self._ctrl_held and self._alt_held):
                self._recording = False
                self.released.emit()
