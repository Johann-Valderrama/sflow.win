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
       Solo cuentan taps LIMPIOS: si otra tecla se presiona mientras Shift está abajo
       (Shift+A para una mayúscula, Shift+Enter), ese Shift NO cuenta ni para iniciar
       ni para detener. La decisión se toma al SOLTAR Shift, no al presionarlo, y
       cualquier tecla no-Shift invalida una secuencia de taps en curso. Motivo: el
       usuario dicta manos-libres y a la vez teclea en otra app (p. ej. responde un
       WhatsApp); una mayúscula no debe cortar el dictado a mitad de frase.
    3. Mantener Ctrl+Shift+Alt:  hold → traducir al idioma destino
       (Shift debe estar presionado ANTES de Alt). También usa ARMING_DELAY.
    4. AltGr + T toggle:         manos-libres → traducir (una pulsación inicia, otra detiene).
       Anteriormente era AltGr+Space; cambiado a AltGr+T para evitar conflicto con el
       atajo global de Claude (Ctrl+Alt+Space, que en Windows ≡ AltGr+Space).

    NOTA: AltGr se rastrea separado del Alt regular para que AltGr solo NO active Ctrl+Alt (modo 1).
    NOTA: En Windows, AltGr genera internamente Ctrl+Alt. pynput expone AltGr como
    keyboard.Key.alt_gr en on_press; la detección de 'T' se hace por vk (0x54) para ser
    robusta frente a layouts donde AltGr+T produce un carácter alternativo. Si el layout
    no genera alt_gr como evento especial sino como Ctrl+Alt, el vk de T sigue siendo 0x54.
    Escribir 't' normal (sin AltGr) NO disparará el modo 4 porque _alt_gr_held será False.
    Ctrl+Alt+T (sin AltGr físico) puede comportarse igual que AltGr+T si pynput no distingue
    entre ambos en el layout del usuario — este es el mismo comportamiento que tenía AltGr+Space.
    """

    pressed = pyqtSignal()
    released = pyqtSignal()
    translate_pressed = pyqtSignal()
    meeting_toggle = pyqtSignal()  # AltGr+R: iniciar/terminar modo reunión (toggle)
    highlight_pressed = pyqtSignal()  # AltGr+H: marcar momento destacado durante la reunión
    hud_toggle = pyqtSignal()      # AltGr+A: abrir/cerrar el HUD proactivo (unidad 5.3)
    lost_pressed = pyqtSignal()    # AltGr+M: "Me perdí" — resumen de los últimos 2 min (unidad 5.3)

    def __init__(self):
        """Inicializa el estado de teclas, el timer de armado y la detección de triple-tap."""
        super().__init__()
        self._ctrl_held = False
        self._alt_held = False       # Alt regular (no AltGr)
        self._alt_gr_held = False    # AltGr rastreado por separado
        self._shift_held = False
        self._recording = False
        self._hands_free = False
        self._alt_gr_t_mode = False  # True cuando está en modo toggle AltGr+T
        self._h_held = False  # supresión de auto-repeat para AltGr+H (no es un toggle idempotente)
        self._a_held = False  # supresión de auto-repeat para AltGr+A (mismo patrón que H)
        self._m_held = False  # supresión de auto-repeat para AltGr+M (mismo patrón que H)
        self._r_held = False  # supresión de auto-repeat para AltGr+R (unidad 0.3, mismo patrón que H)
        self._t_held = False  # supresión de auto-repeat para AltGr+T (unidad 0.3, mismo patrón que H)

        self._listener: keyboard.Listener | None = None

        # Detección de triple-tap (Shift)
        self._last_shift_press = 0.0
        self._shift_tap_count = 0
        self._shift_chord = False  # True si otra tecla se presionó con Shift abajo (acorde, no tap)

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
        self._alt_gr_t_mode = False
        self._h_held = False
        self._a_held = False
        self._m_held = False
        self._r_held = False
        self._t_held = False
        self._shift_tap_count = 0
        self._last_shift_press = 0.0
        self._shift_chord = False

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
        # Incluye 't'/'T' (vk=0x54) como tecla "permitida" durante el armado para que
        # AltGr+T no aborte el timer de modos 1/3. En la práctica el timer de armado
        # solo está activo cuando Ctrl+Alt están presionados, lo que coincide con AltGr;
        # el modo 4 se procesa antes de llegar aquí, así que esto es solo un guard extra.
        _T_VK = 0x54
        if hasattr(key, 'vk') and key.vk == _T_VK:
            return True
        return key in (
            keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
            keyboard.Key.alt,    keyboard.Key.alt_l,    keyboard.Key.alt_r,
            keyboard.Key.alt_gr,
            keyboard.Key.shift,  keyboard.Key.shift_l,  keyboard.Key.shift_r,
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
        # Detectar 'T' por vk (0x54) para robustez ante layouts donde AltGr+T
        # genera un carácter alternativo. El char puede ser 't', 'T' u otro glifo;
        # el vk siempre es 0x54 en teclados PC estándar bajo Windows.
        _T_VK = 0x54
        is_t = hasattr(key, 'vk') and key.vk == _T_VK
        # Detectar 'R' por vk (0x52) para el toggle de reunión (AltGr+R), robusto
        # ante layouts igual que la detección de 'T'.
        _R_VK = 0x52
        is_r = hasattr(key, 'vk') and key.vk == _R_VK
        # Detectar 'H' por vk (0x48) para el highlight de reunión (AltGr+H), robusto
        # ante layouts igual que la detección de 'T'/'R'.
        _H_VK = 0x48
        is_h = hasattr(key, 'vk') and key.vk == _H_VK
        # Detectar 'A' por vk (0x41) para el toggle del HUD proactivo (AltGr+A) y
        # 'M' por vk (0x4D) para "Me perdí" (AltGr+M), robustos ante layouts igual
        # que la detección de 'T'/'R'/'H' (unidad 5.3).
        _A_VK = 0x41
        is_a = hasattr(key, 'vk') and key.vk == _A_VK
        _M_VK = 0x4D
        is_m = hasattr(key, 'vk') and key.vk == _M_VK

        # --- Tap limpio de Shift: cualquier otra tecla invalida tap y secuencia ---
        # Va ANTES de los bloques con return (reunión, modo 4) para que el acorde
        # quede marcado aunque esos bloques corten el flujo.
        if not is_shift:
            self._shift_tap_count = 0          # otra tecla corta una secuencia de taps en curso
            if self._shift_held:
                self._shift_chord = True       # Shift está siendo usado como acorde (Shift+A), no como tap

        # --- Modo Reunión: toggle AltGr + R (independiente del dictado) ---
        # La reunión es una sesión propia (core.meeting.MEETING), no usa la máquina
        # de estados de dictado: solo emite la señal y el slot decide iniciar/terminar.
        # NO es un toggle idempotente frente al auto-repeat de Windows: sin _r_held,
        # mantener R presionado encadena start/stop de reunión a ~30 Hz (bug F10).
        # self._r_held suprime los repeats hasta el release real de R; el toggle
        # legítimo (press-release-press) queda intacto.
        if is_r and self._alt_gr_held:
            if not self._r_held:
                self._r_held = True
                self.meeting_toggle.emit()
            return

        # --- Highlight de reunión: AltGr + H (marcar momento destacado) ---
        # Sin supresión de auto-repeat, mantener H presionado emitiría la señal
        # decenas de veces por segundo (Windows repite on_press mientras la tecla
        # sigue abajo). self._h_held bloquea los repeats hasta el release real de H.
        if is_h and self._alt_gr_held:
            if not self._h_held:
                self._h_held = True
                self.highlight_pressed.emit()
            return

        # --- Toggle del HUD proactivo: AltGr + A (unidad 5.3) ---
        # Mismo patrón anti-auto-repeat que H: sin _a_held, mantener A presionado
        # togglearía el HUD decenas de veces por segundo.
        if is_a and self._alt_gr_held:
            if not self._a_held:
                self._a_held = True
                self.hud_toggle.emit()
            return

        # --- "Me perdí": AltGr + M (unidad 5.3) ---
        if is_m and self._alt_gr_held:
            if not self._m_held:
                self._m_held = True
                self.lost_pressed.emit()
            return

        # NOTA: el atajo de Transform NO vive aquí. Lo registra `core/global_hotkey.py`
        # con `RegisterHotKey` de Win32, porque este listener OBSERVA las teclas pero
        # no se las queda, y Transform actúa sobre texto SELECCIONADO: cualquier tecla
        # que la aplicación en foco interprete destruye justo lo que se iba a
        # transformar. Ver la cabecera de ese módulo, con las cuatro combinaciones
        # medidas antes de cambiar de enfoque.

        # NO es un toggle idempotente frente al auto-repeat de Windows: sin _t_held,
        # mantener T presionado un instante de más alterna el toggle interno
        # (_alt_gr_t_mode/_recording) a ~30 Hz — el primer repeat entra por la rama
        # "detener" y el siguiente por "iniciar" (bug F10). self._t_held suprime los
        # repeats hasta el release real de T; el toggle legítimo (press-release-press)
        # queda intacto.
        if is_t and self._alt_gr_held:
            if not self._t_held:
                self._t_held = True
                if self._alt_gr_t_mode and self._recording:
                    # Segunda pulsación → detener grabación
                    self._alt_gr_t_mode = False
                    self._recording = False
                    self.released.emit()
                elif not self._recording:
                    # Primera pulsación → iniciar traducción (sin hold)
                    self._alt_gr_t_mode = True
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
            # Ignorar auto-repeat de Windows
            if self._shift_held:
                return
            self._shift_held = True
            self._shift_chord = False  # tap potencial; se invalida si otra tecla llega antes del release
            # El stop de manos-libres y el triple-tap se deciden en _on_release,
            # solo si el tap fue LIMPIO (sin otras teclas mientras Shift estuvo abajo).

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
        _H_VK = 0x48
        is_h = hasattr(key, 'vk') and key.vk == _H_VK
        if is_h:
            self._h_held = False
        _A_VK = 0x41
        if hasattr(key, 'vk') and key.vk == _A_VK:
            self._a_held = False
        _M_VK = 0x4D
        if hasattr(key, 'vk') and key.vk == _M_VK:
            self._m_held = False
        _R_VK = 0x52
        if hasattr(key, 'vk') and key.vk == _R_VK:
            self._r_held = False
        _T_VK = 0x54
        if hasattr(key, 'vk') and key.vk == _T_VK:
            self._t_held = False

        # Actualizar estado de modificadores
        if is_ctrl:
            self._ctrl_held = False
        elif is_alt:
            self._alt_held = False
        elif is_alt_gr:
            self._alt_gr_held = False
        elif is_shift:
            self._shift_held = False
            clean_tap = not self._shift_chord
            self._shift_chord = False
            if clean_tap:
                now = time.time()
                # Manos-libres (modo 2): un tap LIMPIO de Shift detiene la grabación
                if self._hands_free and self._recording:
                    self._hands_free = False
                    self._recording = False
                    self._shift_tap_count = 0
                    self.released.emit()
                    return
                # Detección de triple-tap (solo taps limpios)
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

        # Cancelar armado si se sueltan Ctrl o Alt antes de que expire el timer
        if (is_ctrl or is_alt) and self._arm_timer is not None:
            self._cancel_arm()

        # Modo 4 (toggle AltGr+T): la detención la maneja la segunda pulsación en _on_press.
        # Soltar AltGr NO detiene la grabación — el usuario debe pulsar AltGr+T de nuevo.
        if self._alt_gr_t_mode:
            return

        # Modos 1 y 3 (hold): detener cuando se suelta Ctrl o Alt
        # Modo 2 (manos-libres): la detención la maneja _on_press (tap de Shift)
        if self._recording and not self._hands_free:
            if not (self._ctrl_held and self._alt_held):
                self._recording = False
                self.released.emit()
