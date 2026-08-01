"""Tests para la unidad 6.3: modos de dictado por app activa.

Cubre:
(a) parseo del mapa exe:preset (default, custom, malformado)
(b) matching por basename case-insensitive
(c) app no mapeada -> texto intacto (preset None)
(d) toggle off -> cero llamadas LLM (mock)
(e) timeout/fallo LLM -> texto original pegado (reformat_text devuelve None)
(f) claude-cli como backend batch -> se salta el reformateo (documented decision)
(g) captura de exe best-effort (mock de ctypes: fallo -> None)
(h) raw_text conserva el MAS crudo cuando diccionario Y reformateo cambian
(i) pipeline semi-real save_exe -> map -> prompt -> paste (con LLM mockeado)
(j) panel de settings: roundtrip GET/POST + CSRF
(k) Ola 2 (unidad 2a, docs/PLAN-DICTADO-2026-07-31.md): los 5 presets existen y
    son válidos, `parse_mode_map` acepta `lista`/`notas`, los 5 prompts traen la
    regla de respetar saltos de línea explícitos, y `reformat_text` funciona con
    los 2 presets nuevos.
"""
import os
import time
import concurrent.futures
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# (a) Parseo del mapa exe:preset
# ---------------------------------------------------------------------------
class TestParseModeMap:
    def test_default_map_parses(self):
        from core.dictation_modes import parse_mode_map, DEFAULT_MODE_MAP
        result = parse_mode_map(DEFAULT_MODE_MAP)
        assert result["outlook.exe"] == "email"
        assert result["slack.exe"] == "chat"
        assert result["code.exe"] == "codigo"

    def test_custom_map_parses(self):
        from core.dictation_modes import parse_mode_map
        result = parse_mode_map("notepad.exe:chat, MyApp.EXE:codigo")
        assert result["notepad.exe"] == "chat"
        assert result["myapp.exe"] == "codigo"  # normalizado a minúsculas

    def test_malformed_entries_ignored(self):
        from core.dictation_modes import parse_mode_map
        result = parse_mode_map("novalido,justexe:,  :email,foo.exe:preset_invalido,bar.exe:chat")
        assert result == {"bar.exe": "chat"}

    def test_empty_string_returns_empty_dict(self):
        from core.dictation_modes import parse_mode_map
        assert parse_mode_map("") == {}
        assert parse_mode_map(None) == {}

    def test_newline_separated_entries(self):
        from core.dictation_modes import parse_mode_map
        result = parse_mode_map("outlook.exe:email\nslack.exe:chat\r\ncode.exe:codigo")
        assert result == {"outlook.exe": "email", "slack.exe": "chat", "code.exe": "codigo"}


# ---------------------------------------------------------------------------
# (b) Matching por basename case-insensitive + (c) app no mapeada
# ---------------------------------------------------------------------------
class TestPresetForExe:
    def test_matches_basename_case_insensitive(self):
        from core.dictation_modes import preset_for_exe
        mode_map = {"outlook.exe": "email"}
        assert preset_for_exe("OUTLOOK.EXE", mode_map) == "email"
        assert preset_for_exe(r"C:\Program Files\Outlook\OUTLOOK.EXE", mode_map) == "email"

    def test_unmapped_app_returns_none(self):
        from core.dictation_modes import preset_for_exe
        mode_map = {"outlook.exe": "email"}
        assert preset_for_exe("notepad.exe", mode_map) is None

    def test_none_exe_returns_none(self):
        from core.dictation_modes import preset_for_exe
        assert preset_for_exe(None, {"outlook.exe": "email"}) is None

    def test_reads_env_when_mode_map_not_passed(self, monkeypatch):
        from core.dictation_modes import preset_for_exe
        monkeypatch.setenv("DICTATION_MODE_MAP", "myide.exe:codigo")
        assert preset_for_exe("MyIDE.exe") == "codigo"


# ---------------------------------------------------------------------------
# (d) Toggle off -> cero llamadas LLM
# ---------------------------------------------------------------------------
class TestModesEnabledToggle:
    def test_disabled_by_default(self, monkeypatch):
        from core.dictation_modes import modes_enabled
        monkeypatch.delenv("DICTATION_MODES_ENABLED", raising=False)
        assert modes_enabled() is False

    def test_enabled_when_true(self, monkeypatch):
        from core.dictation_modes import modes_enabled
        monkeypatch.setenv("DICTATION_MODES_ENABLED", "true")
        assert modes_enabled() is True

    def test_toggle_off_skips_llm_call_in_main_flow(self, monkeypatch):
        """Simula el guard usado en main.py: con el toggle apagado, reformat_text
        NUNCA se llama (cero llamadas a _chat)."""
        from core import dictation_modes
        monkeypatch.delenv("DICTATION_MODES_ENABLED", raising=False)

        with patch("core.insights._chat") as mock_chat:
            enabled = dictation_modes.modes_enabled()
            preset = dictation_modes.preset_for_exe("outlook.exe") if enabled else None
            if enabled and preset:
                dictation_modes.reformat_text("hola", preset)
            mock_chat.assert_not_called()


# ---------------------------------------------------------------------------
# (e) Timeout / fallo del LLM -> None (fallback silencioso: pega el original)
# ---------------------------------------------------------------------------
class TestReformatTextFailureModes:
    def test_unknown_preset_returns_none(self):
        from core.dictation_modes import reformat_text
        assert reformat_text("hola", "preset_que_no_existe") is None

    def test_llm_exception_returns_none(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", MagicMock(side_effect=RuntimeError("boom")))
        result = dictation_modes.reformat_text("hola mundo", "chat")
        assert result is None

    def test_llm_timeout_returns_none(self, monkeypatch):
        from core import dictation_modes, insights as _insights

        def _slow_chat(*args, **kwargs):
            time.sleep(0.3)
            return "reformateado"

        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", _slow_chat)
        result = dictation_modes.reformat_text("hola mundo", "chat", timeout=0.05)
        assert result is None

    def test_llm_timeout_returns_at_timeout_not_at_backend_sleep(self, monkeypatch):
        """F11: reformat_text debe retornar cerca del `timeout` inyectado, NO
        esperar a que el backend colgado termine su sleep (bug del
        ThreadPoolExecutor cuyo __exit__ bloqueaba en shutdown(wait=True))."""
        from core import dictation_modes, insights as _insights

        backend_sleep_s = 2.0
        injected_timeout_s = 0.1

        def _slow_chat(*args, **kwargs):
            time.sleep(backend_sleep_s)
            return "reformateado"

        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", _slow_chat)

        start = time.monotonic()
        result = dictation_modes.reformat_text("hola mundo", "chat", timeout=injected_timeout_s)
        elapsed = time.monotonic() - start

        assert result is None
        # Margen generoso (el hilo daemon del backend colgado sigue vivo en
        # background, pero NO debe bloquear el retorno de esta función).
        assert elapsed < backend_sleep_s / 2, (
            f"reformat_text tardó {elapsed:.2f}s — parece estar esperando al "
            f"backend colgado ({backend_sleep_s}s) en vez de respetar el timeout "
            f"inyectado ({injected_timeout_s}s)."
        )

    def test_timeout_worker_thread_is_daemon(self, monkeypatch):
        """El hilo que ejecuta la llamada al LLM debe ser daemon=True: así un
        backend colgado no impide que el proceso termine (atexit no lo espera)."""
        from core import dictation_modes, insights as _insights
        import threading

        captured_threads = []
        _orig_thread_init = threading.Thread.__init__

        def _capturing_init(self, *args, **kwargs):
            _orig_thread_init(self, *args, **kwargs)
            captured_threads.append(self)

        def _slow_chat(*args, **kwargs):
            time.sleep(0.3)
            return "reformateado"

        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", _slow_chat)
        monkeypatch.setattr(threading.Thread, "__init__", _capturing_init)

        result = dictation_modes.reformat_text("hola mundo", "chat", timeout=0.05)
        assert result is None
        assert len(captured_threads) == 1
        assert captured_threads[0].daemon is True

    def test_llm_empty_response_returns_none(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", lambda *a, **k: "   ")
        result = dictation_modes.reformat_text("hola mundo", "chat")
        assert result is None

    def test_llm_success_returns_reformatted_text(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", lambda *a, **k: "Hola, ¿cómo estás?")
        result = dictation_modes.reformat_text("hola como estas", "email")
        assert result == "Hola, ¿cómo estás?"


# ---------------------------------------------------------------------------
# (f) claude-cli como backend batch -> se salta el reformateo
# ---------------------------------------------------------------------------
class TestClaudeCliSkip:
    def test_claude_cli_backend_skips_reformat(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "claude-cli")
        mock_chat = MagicMock()
        monkeypatch.setattr(_insights, "_chat", mock_chat)
        result = dictation_modes.reformat_text("hola mundo", "chat")
        assert result is None
        mock_chat.assert_not_called()


# ---------------------------------------------------------------------------
# (g) Captura de exe best-effort (mock de ctypes: fallo -> None)
# ---------------------------------------------------------------------------
class TestGetForegroundExeName:
    def test_capture_failure_returns_none(self, monkeypatch):
        from core import clipboard
        monkeypatch.setattr(
            clipboard._user32, "GetWindowThreadProcessId",
            MagicMock(side_effect=OSError("boom")),
        )
        assert clipboard._get_foreground_exe_name(12345) is None

    def test_zero_pid_returns_none(self, monkeypatch):
        from core import clipboard
        import ctypes

        def _fake_get_pid(hwnd, pid_ref):
            pid_ref._obj.value = 0

        monkeypatch.setattr(clipboard._user32, "GetWindowThreadProcessId", _fake_get_pid)
        assert clipboard._get_foreground_exe_name(12345) is None

    def test_openprocess_failure_returns_none(self, monkeypatch):
        from core import clipboard

        def _fake_get_pid(hwnd, pid_ref):
            pid_ref._obj.value = 4242

        monkeypatch.setattr(clipboard._user32, "GetWindowThreadProcessId", _fake_get_pid)
        monkeypatch.setattr(clipboard._kernel32, "OpenProcess", lambda *a, **k: 0)
        assert clipboard._get_foreground_exe_name(12345) is None

    def test_save_frontmost_app_sets_saved_exe_none_on_failure(self, monkeypatch):
        from core import clipboard
        monkeypatch.setattr(clipboard._user32, "GetForegroundWindow", lambda: 999)
        monkeypatch.setattr(clipboard, "_get_foreground_exe_name", lambda hwnd: None)
        clipboard.save_frontmost_app()
        assert clipboard.get_saved_exe() is None

    def test_save_frontmost_app_captures_exe_on_success(self, monkeypatch):
        from core import clipboard
        monkeypatch.setattr(clipboard._user32, "GetForegroundWindow", lambda: 999)
        monkeypatch.setattr(clipboard, "_get_foreground_exe_name", lambda hwnd: "outlook.exe")
        clipboard.save_frontmost_app()
        assert clipboard.get_saved_exe() == "outlook.exe"

    def test_get_saved_exe_not_consumed(self, monkeypatch):
        """A diferencia del HWND, get_saved_exe() NO se limpia tras leerlo."""
        from core import clipboard
        monkeypatch.setattr(clipboard._user32, "GetForegroundWindow", lambda: 999)
        monkeypatch.setattr(clipboard, "_get_foreground_exe_name", lambda hwnd: "slack.exe")
        clipboard.save_frontmost_app()
        assert clipboard.get_saved_exe() == "slack.exe"
        assert clipboard.get_saved_exe() == "slack.exe"  # segunda lectura, sigue igual


# ---------------------------------------------------------------------------
# (h) raw_text conserva el MAS crudo (diccionario Y reformateo cambian)
# ---------------------------------------------------------------------------
class TestRawTextMostRaw:
    def _simulate_post_transcribe_reformat(self, text, raw_full, translate, source,
                                            modes_on, preset, reformatted):
        """Replica la lógica añadida en main.py::_transcribe_final (unidad 6.3)
        para verificar la regla de raw_text sin depender de PyQt6."""
        from core import dictation_modes as _dm
        with patch.object(_dm, "modes_enabled", return_value=modes_on), \
             patch.object(_dm, "preset_for_exe", return_value=preset), \
             patch.object(_dm, "reformat_text", return_value=reformatted):
            if raw_full is not None and raw_full.strip() == text:
                raw_full = None
            if not translate and source != "system" and _dm.modes_enabled():
                p = _dm.preset_for_exe("dummy.exe")
                if p:
                    new_text = _dm.reformat_text(text, p)
                    if new_text and new_text != text:
                        if raw_full is None:
                            raw_full = text
                        text = new_text
            return text, raw_full

    def test_dictionary_and_reformat_both_change_keeps_most_raw(self):
        """El diccionario ya cambió 'Johan'->'Johann' (raw_full = crudo pre-diccionario).
        El reformateo cambia aún más el texto. raw_full debe seguir siendo el
        crudo PRE-diccionario (el más crudo de los dos), no el pre-reformateo."""
        text, raw_full = self._simulate_post_transcribe_reformat(
            text="hola Johann como estas",
            raw_full="hola Johan como estas",
            translate=False, source="mic",
            modes_on=True, preset="email", reformatted="Hola Johann, ¿cómo estás?",
        )
        assert text == "Hola Johann, ¿cómo estás?"
        assert raw_full == "hola Johan como estas"

    def test_only_reformat_changes_raw_becomes_pre_reformat_text(self):
        """Sin cambio de diccionario (raw_full None de entrada), el reformateo
        SÍ cambia el texto -> el crudo pasa a ser el texto pre-reformateo."""
        text, raw_full = self._simulate_post_transcribe_reformat(
            text="hola como estas",
            raw_full=None,
            translate=False, source="mic",
            modes_on=True, preset="email", reformatted="Hola, ¿cómo estás?",
        )
        assert text == "Hola, ¿cómo estás?"
        assert raw_full == "hola como estas"

    def test_no_reformat_no_dictionary_raw_stays_none(self):
        text, raw_full = self._simulate_post_transcribe_reformat(
            text="hola mundo", raw_full=None,
            translate=False, source="mic",
            modes_on=False, preset=None, reformatted=None,
        )
        assert text == "hola mundo"
        assert raw_full is None

    def test_translate_mode_never_reformats(self):
        text, raw_full = self._simulate_post_transcribe_reformat(
            text="hello world", raw_full=None,
            translate=True, source="mic",
            modes_on=True, preset="email", reformatted="Should not apply",
        )
        assert text == "hello world"
        assert raw_full is None

    def test_system_audio_source_never_reformats(self):
        text, raw_full = self._simulate_post_transcribe_reformat(
            text="algo del video", raw_full=None,
            translate=False, source="system",
            modes_on=True, preset="chat", reformatted="Should not apply",
        )
        assert text == "algo del video"
        assert raw_full is None


# ---------------------------------------------------------------------------
# (i) Pipeline semi-real: save_exe -> map -> prompt -> reformat (LLM mockeado)
# ---------------------------------------------------------------------------
class TestFullPipelineSemiReal:
    def test_full_pipeline_save_exe_to_reformat(self, monkeypatch):
        from core import clipboard, dictation_modes as _dm, insights as _insights

        # 1. Simula el momento de save_frontmost_app(): la app en foco es Outlook.
        monkeypatch.setattr(clipboard._user32, "GetForegroundWindow", lambda: 777)
        monkeypatch.setattr(clipboard, "_get_foreground_exe_name", lambda hwnd: "OUTLOOK.EXE")
        clipboard.save_frontmost_app()

        # 2. El worker de post-transcripción lee el exe capturado.
        exe_name = clipboard.get_saved_exe()
        assert exe_name == "OUTLOOK.EXE"

        # 3. Resuelve el preset vía el mapa configurado (custom, vía env).
        monkeypatch.setenv("DICTATION_MODE_MAP", "outlook.exe:email")
        monkeypatch.setenv("DICTATION_MODES_ENABLED", "true")
        preset = _dm.preset_for_exe(exe_name)
        assert preset == "email"
        assert _dm.modes_enabled() is True

        # 4. LLM mockeado (sin tocar GROQ_API_KEY real) responde reformateado.
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", lambda *a, **k: "Buenas tardes equipo, quedo atento.")

        dictated_text = "buenas tardes equipo quedo atento"
        reformatted = _dm.reformat_text(dictated_text, preset)
        assert reformatted == "Buenas tardes equipo, quedo atento."


# ---------------------------------------------------------------------------
# (k) Ola 2 (unidad 2a): 5 presets, parseo de los 2 nuevos, y la regla de
# respetar saltos de línea explícitos presente en los 5 prompts.
# ---------------------------------------------------------------------------
class TestOla2CincoPresets:
    """Ola 2 (unidad 2a) de docs/PLAN-DICTADO-2026-07-31.md: añade `lista` y
    `notas` a los 3 presets existentes (`email`/`chat`/`codigo`), y mete en
    los 5 prompts la regla de respetar los saltos de línea explícitos que
    puso smart commands (CLAUDE.md sección 19, Eje 1)."""

    def test_five_presets_exist_and_are_valid(self):
        from core.dictation_modes import PRESETS, _VALID_PRESETS
        assert set(PRESETS.keys()) == {"email", "chat", "codigo", "lista", "notas"}
        # _VALID_PRESETS se deriva de PRESETS.keys(); si algún día se
        # desincronizan (alguien la hardcodea aparte), esto lo cazaría.
        assert _VALID_PRESETS == frozenset(PRESETS.keys())

    def test_parse_mode_map_accepts_lista_and_notas(self):
        """`parse_mode_map` hoy rechaza cualquier preset desconocido (ver
        test_malformed_entries_ignored más arriba): si `lista`/`notas` no
        quedaran cableados en `PRESETS`, este roundtrip fallaría en silencio
        (la entrada se descartaría como 'preset inválido')."""
        from core.dictation_modes import parse_mode_map
        result = parse_mode_map("notas.exe:lista, obsidian.exe:notas")
        assert result == {"notas.exe": "lista", "obsidian.exe": "notas"}

    def test_preset_for_exe_resolves_lista_and_notas(self):
        from core.dictation_modes import preset_for_exe
        mode_map = {"a.exe": "lista", "b.exe": "notas"}
        assert preset_for_exe("a.exe", mode_map) == "lista"
        assert preset_for_exe("b.exe", mode_map) == "notas"

    def test_all_five_prompts_contain_explicit_linebreak_rule(self):
        """Recorre `PRESETS` (no una lista escrita a mano con los 5 nombres):
        si mañana alguien agrega un sexto preset sin la regla de respetar los
        saltos de línea explícitos, este test tiene que fallar señalando ESE
        preset nuevo, no quedarse verde porque ya cubrió los 5 de hoy."""
        from core.dictation_modes import PRESETS
        assert len(PRESETS) >= 5, "se esperaban al menos los 5 presets de la Ola 2"
        for name, prompt in PRESETS.items():
            lowered = prompt.lower()
            assert "salto" in lowered and "línea" in lowered, (
                f"el preset '{name}' no menciona los saltos de línea en su "
                "prompt (CLAUDE.md sección 19, Eje 1: el reformateo LLM debe "
                "respetar los saltos de línea explícitos que puso smart "
                "commands, p. ej. tras 'nueva línea' o 'punto y aparte')."
            )
            assert "consérv" in lowered, (
                f"el preset '{name}' menciona 'salto'/'línea' pero no trae "
                "la instrucción de CONSERVARLOS (con tilde: 'consérv...'); "
                "puede ser una mención incidental y no la regla completa "
                "del Eje 1."
            )


# ---------------------------------------------------------------------------
# (k, cont.) reformat_text sigue funcionando con los presets nuevos
# ---------------------------------------------------------------------------
class TestReformatTextNewPresets:
    def test_reformat_text_lista_success(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", lambda *a, **k: "- leche\n- pan\n- huevos")
        result = dictation_modes.reformat_text("leche pan y huevos", "lista")
        assert result == "- leche\n- pan\n- huevos"

    def test_reformat_text_notas_success(self, monkeypatch):
        from core import dictation_modes, insights as _insights
        monkeypatch.setattr(_insights, "_resolve_backend", lambda task: "groq")
        monkeypatch.setattr(_insights, "_chat", lambda *a, **k: "Hoy revisamos el presupuesto.")
        result = dictation_modes.reformat_text("hoy revisamos el presupuesto", "notas")
        assert result == "Hoy revisamos el presupuesto."

    def test_reformat_text_still_none_for_unknown_preset(self):
        """Repite el caso de TestReformatTextFailureModes a propósito: con 5
        presets válidos ahora en la tabla, un nombre inexistente sigue sin
        colar por accidente (p. ej. por un match parcial de string)."""
        from core.dictation_modes import reformat_text
        assert reformat_text("hola", "listaaa") is None
        assert reformat_text("hola", "notas_invalido") is None


# ---------------------------------------------------------------------------
# (l) Ola 2 (unidad 2b): elección MANUAL del preset para el siguiente
# dictado (menú de bandeja), que GANA sobre el mapeo automático por .exe.
# ---------------------------------------------------------------------------
class TestManualPresetPrecedence:
    """`core.dictation_modes._manual_preset` es estado de PROCESO (no DB, no
    archivo): cada test lo limpia antes y después para no contaminar a los
    demás, incluidos los de otras clases de este archivo."""

    @pytest.fixture(autouse=True)
    def _reset_manual_preset(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset(None)
        yield
        _dm.set_manual_preset(None)

    def test_manual_preset_wins_over_exe_mapping(self):
        """La precedencia del contrato de la unidad 2b: manual > automático.
        Un exe mapeado a 'email' no le gana a un preset armado a mano."""
        from core import dictation_modes as _dm
        mode_map = {"outlook.exe": "email"}
        _dm.set_manual_preset("lista")
        assert _dm.resolve_preset("outlook.exe", mode_map) == "lista"

    def test_no_manual_preset_falls_back_to_exe_mapping(self):
        from core import dictation_modes as _dm
        mode_map = {"outlook.exe": "email"}
        assert _dm.get_manual_preset() is None
        assert _dm.resolve_preset("outlook.exe", mode_map) == "email"

    def test_manual_preset_wins_even_with_no_exe_match(self):
        """El preset manual no depende de que haya un mapeo automático: es
        justo el caso de uso que originó la unidad ('dictar una lista
        estando en CUALQUIER app', incluida una sin mapear)."""
        from core import dictation_modes as _dm
        _dm.set_manual_preset("lista")
        assert _dm.resolve_preset("notepad.exe", {}) == "lista"
        _dm.set_manual_preset("lista")
        assert _dm.resolve_preset(None, {}) == "lista"


class TestManualPresetSingleUse:
    """Decisión de diseño [un solo uso, NO pegajoso] de la unidad 2b: se
    consume tras UN `resolve_preset`. Ver el docstring de
    `consume_manual_preset` en core/dictation_modes.py para el porqué."""

    @pytest.fixture(autouse=True)
    def _reset_manual_preset(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset(None)
        yield
        _dm.set_manual_preset(None)

    def test_manual_preset_is_consumed_after_one_resolve_then_reverts(self):
        from core import dictation_modes as _dm
        mode_map = {"outlook.exe": "email"}
        _dm.set_manual_preset("lista")
        first_dictation = _dm.resolve_preset("outlook.exe", mode_map)
        second_dictation = _dm.resolve_preset("outlook.exe", mode_map)
        assert first_dictation == "lista"
        assert second_dictation == "email", (
            "el preset manual NO debe seguir vivo para el segundo dictado: "
            "es un uso único ('el siguiente dictado'), no un modo pegajoso "
            "que el usuario tendría que acordarse de apagar."
        )

    def test_get_manual_preset_is_read_only_does_not_consume(self):
        """Pintar el estado (tooltip/menú) con `get_manual_preset()` no
        puede gastar el uso único — a diferencia de `resolve_preset`."""
        from core import dictation_modes as _dm
        _dm.set_manual_preset("notas")
        assert _dm.get_manual_preset() == "notas"
        assert _dm.get_manual_preset() == "notas"  # segunda lectura, sigue armado
        assert _dm.resolve_preset(None) == "notas"  # aquí sí se gasta
        assert _dm.get_manual_preset() is None

    def test_consume_manual_preset_clears_state_even_when_returning_none(self):
        """Sin nada armado, consumir no debe dejar basura ni lanzar."""
        from core import dictation_modes as _dm
        assert _dm.consume_manual_preset() is None
        assert _dm.get_manual_preset() is None


class TestManualPresetBackToAutomatic:
    """'Cómo se vuelve al automático' tiene que tener una salida explícita,
    no solo una entrada: `set_manual_preset(None)` es esa salida (la elige
    el usuario desde el ítem 'Automático (según la app en foco)' del menú)."""

    @pytest.fixture(autouse=True)
    def _reset_manual_preset(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset(None)
        yield
        _dm.set_manual_preset(None)

    def test_setting_none_explicitly_clears_an_armed_preset(self):
        from core import dictation_modes as _dm
        mode_map = {"outlook.exe": "email"}
        _dm.set_manual_preset("lista")
        _dm.set_manual_preset(None)
        assert _dm.get_manual_preset() is None
        assert _dm.resolve_preset("outlook.exe", mode_map) == "email"


class TestManualPresetInvalidDoesNotBreak:
    """Un preset manual inválido nunca debería originarse desde el menú (que
    solo ofrece nombres de `PRESETS`), pero `resolve_preset` se defiende
    igual: no rompe nada, se descarta y cae al automático."""

    @pytest.fixture(autouse=True)
    def _reset_manual_preset(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset(None)
        yield
        _dm.set_manual_preset(None)

    def test_invalid_manual_preset_falls_back_to_exe_mapping(self):
        from core import dictation_modes as _dm
        mode_map = {"outlook.exe": "email"}
        _dm.set_manual_preset("preset_que_no_existe")
        assert _dm.resolve_preset("outlook.exe", mode_map) == "email"
        # También se consumió/descartó: no queda repitiéndose en el próximo dictado.
        assert _dm.get_manual_preset() is None

    def test_invalid_manual_preset_with_no_automatic_match_returns_none(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset("otro_invalido")
        assert _dm.resolve_preset("notepad.exe", {}) is None

    def test_reformat_text_never_sees_the_invalid_manual_value(self):
        """Fin a fin: si `resolve_preset` devolviera el string inválido tal
        cual (bug), `reformat_text` lo rechazaría igual (`preset not in
        PRESETS`) — pero no debería llegar a intentarlo."""
        from core import dictation_modes as _dm
        _dm.set_manual_preset("preset_que_no_existe")
        preset = _dm.resolve_preset("notepad.exe", {})
        assert preset is None
        assert _dm.reformat_text("hola", preset or "preset_que_no_existe") is None


class TestManualPresetRespectsGlobalFlag:
    """La elección manual NO se salta `DICTATION_MODES_ENABLED`: con el flag
    global apagado (el default), un preset armado a mano sigue sin
    reformatear nada. Esto replica el bloque de gates de
    main.py::_transcribe_final para probar el ALGORITMO sin depender de
    PyQt6 (mismo patrón que TestRawTextMostRaw más arriba); el guardián de
    que main.py REALMENTE tiene este cableado es
    TestCableadoManualPresetEnMainPy, más abajo."""

    def _simulate_dictation_modes_block(
        self, text, translate, source, modes_on, resolved_preset, reformatted,
    ):
        from core import dictation_modes as _dm
        resolve_mock = MagicMock(return_value=resolved_preset)
        reformat_mock = MagicMock(return_value=reformatted)
        with patch.object(_dm, "modes_enabled", return_value=modes_on), \
             patch.object(_dm, "resolve_preset", resolve_mock), \
             patch.object(_dm, "reformat_text", reformat_mock):
            if not translate and source != "system" and _dm.modes_enabled():
                preset = _dm.resolve_preset("dummy.exe")
                if preset:
                    new_text = _dm.reformat_text(text, preset)
                    if new_text and new_text != text:
                        text = new_text
        return text, resolve_mock, reformat_mock

    def test_disabled_flag_skips_resolve_even_with_a_preset_that_would_apply(self):
        """Con el flag global apagado, `resolve_preset` ni siquiera se
        llama — así que tampoco se consume el preset manual armado (queda
        vivo para cuando el usuario encienda el flag en Ajustes)."""
        text, resolve_mock, reformat_mock = self._simulate_dictation_modes_block(
            text="leche pan huevos", translate=False, source="mic",
            modes_on=False, resolved_preset="lista", reformatted="- leche\n- pan\n- huevos",
        )
        assert text == "leche pan huevos"
        resolve_mock.assert_not_called()
        reformat_mock.assert_not_called()

    def test_enabled_flag_resolves_and_reformats(self):
        text, resolve_mock, reformat_mock = self._simulate_dictation_modes_block(
            text="leche pan huevos", translate=False, source="mic",
            modes_on=True, resolved_preset="lista", reformatted="- leche\n- pan\n- huevos",
        )
        assert text == "- leche\n- pan\n- huevos"
        resolve_mock.assert_called_once()
        reformat_mock.assert_called_once()

    def test_translate_mode_never_resolves_even_if_enabled(self):
        text, resolve_mock, reformat_mock = self._simulate_dictation_modes_block(
            text="hello world", translate=True, source="mic",
            modes_on=True, resolved_preset="lista", reformatted="should not apply",
        )
        assert text == "hello world"
        resolve_mock.assert_not_called()

    def test_system_audio_source_never_resolves_even_if_enabled(self):
        text, resolve_mock, reformat_mock = self._simulate_dictation_modes_block(
            text="algo del video", translate=False, source="system",
            modes_on=True, resolved_preset="lista", reformatted="should not apply",
        )
        assert text == "algo del video"
        resolve_mock.assert_not_called()


class TestTrayTooltipReflectsManualPreset:
    """'Cómo ve el usuario el preset activo sin abrir el menú': el tooltip
    de la bandeja (`main._tray_tooltip_text`), reusando la superficie que
    ya existía en vez de inventar una nueva. Son llamadas REALES a main.py,
    no una réplica de su lógica — main.py es importable sin levantar
    QApplication (ver tests/test_chunk_assembly.py, que ya hace `from main
    import ...`)."""

    @pytest.fixture(autouse=True)
    def _reset_manual_preset(self):
        from core import dictation_modes as _dm
        _dm.set_manual_preset(None)
        yield
        _dm.set_manual_preset(None)

    def test_tooltip_is_the_base_text_with_no_manual_preset(self):
        from main import _tray_tooltip_text
        from config import APP_VERSION
        assert _tray_tooltip_text() == f"Vflow v{APP_VERSION} - Voice to Text"

    def test_tooltip_shows_the_armed_preset_short_name(self):
        from main import _tray_tooltip_text
        from core import dictation_modes as _dm
        _dm.set_manual_preset("lista")
        tooltip = _tray_tooltip_text()
        assert "Próximo dictado: Lista" in tooltip

    def test_tooltip_reverts_to_base_once_the_preset_is_consumed(self):
        """El tooltip no debe seguir anunciando un preset que ya se usó: tras
        `resolve_preset` (lo que main.py llama al terminar un dictado), debe
        volver a verse igual que si nunca se hubiera armado nada."""
        from main import _tray_tooltip_text
        from core import dictation_modes as _dm
        from config import APP_VERSION
        _dm.set_manual_preset("notas")
        assert "Próximo dictado" in _tray_tooltip_text()
        _dm.resolve_preset(None)  # simula el fin del dictado que lo consume
        assert _tray_tooltip_text() == f"Vflow v{APP_VERSION} - Voice to Text"

    def test_all_five_presets_plus_automatic_have_a_short_name(self):
        """`_DICTATION_PRESET_SHORT_NAMES` tiene que cubrir los 5 presets de
        PRESETS más `None` (automático); si un preset nuevo se agrega a
        `PRESETS` sin agregar su nombre corto, el tooltip mostraría el
        nombre técnico crudo en vez de uno legible."""
        from main import _DICTATION_PRESET_SHORT_NAMES
        from core.dictation_modes import PRESETS
        assert set(_DICTATION_PRESET_SHORT_NAMES.keys()) == set(PRESETS.keys()) | {None}


# ---------------------------------------------------------------------------
# (l, cont.) Guardián ESTRUCTURAL: main.py de verdad tiene el cableado, no
# solo el algoritmo de arriba. Mismo patrón que
# tests/test_smart_commands.py::TestCableadoExisteEnMainPy — un test que
# solo re-implementa la lógica de main.py no sirve de guardián si el
# cableado real desaparece (p. ej. alguien revierte main.py a llamar
# `preset_for_exe` directo, sin pasar por la precedencia manual).
# ---------------------------------------------------------------------------
class TestCableadoManualPresetEnMainPy:
    @staticmethod
    def _transcribe_final_source() -> str:
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        source = (repo_root / "main.py").read_text(encoding="utf-8")
        start = source.index("def _transcribe_final(")
        next_def = source.index("\n    def ", start)
        return source[start:next_def]

    @staticmethod
    def _setup_tray_source() -> str:
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        source = (repo_root / "main.py").read_text(encoding="utf-8")
        start = source.index("def _setup_tray(")
        next_def = source.index("\n# ---", start)
        return source[start:next_def]

    def test_full_source_declares_the_manual_preset_consumed_signal(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        source = (repo_root / "main.py").read_text(encoding="utf-8")
        assert "dictation_manual_preset_consumed = pyqtSignal()" in source, (
            "VflowApp ya no declara la señal dictation_manual_preset_consumed. "
            "Sin ella, el tooltip de la bandeja no se entera cuando un "
            "dictado consume el preset manual armado (unidad 2b)."
        )

    def test_transcribe_final_calls_resolve_preset_not_preset_for_exe_direct(self):
        """El cableado tiene que pasar por `resolve_preset` (que aplica la
        precedencia manual>automático), no por `preset_for_exe` directo —
        eso saltaría la elección manual por completo."""
        body = self._transcribe_final_source()
        assert "dictation_modes.resolve_preset(" in body, (
            "main.py::_transcribe_final ya NO llama a "
            "dictation_modes.resolve_preset(...). La precedencia manual>"
            "automático de la unidad 2b desapareció del dictado real, aunque "
            "toda tests/test_dictation_modes.py::TestManualPreset* siga en "
            "verde (esos tests prueban el ALGORITMO en core/dictation_modes.py, "
            "no que main.py lo ejecute)."
        )
        assert "dictation_modes.preset_for_exe(" not in body, (
            "main.py::_transcribe_final llama a dictation_modes.preset_for_exe(...) "
            "directo otra vez — eso resuelve SOLO el mapeo automático por .exe y "
            "se salta por completo el preset manual armado en la bandeja "
            "(unidad 2b). Debe pasar por dictation_modes.resolve_preset(...), "
            "que aplica la precedencia manual>automático."
        )

    def test_manual_preset_state_is_read_before_resolving(self):
        """`get_manual_preset()` (lectura, sin consumir) tiene que leerse
        ANTES de `resolve_preset()` (que sí consume): así main.py sabe si
        hubo un preset manual armado para avisarle a la bandeja, sin
        depender de adivinar el resultado de resolve_preset después de que
        ya lo gastó."""
        body = self._transcribe_final_source()
        idx_get = body.find("dictation_modes.get_manual_preset()")
        idx_resolve = body.find("dictation_modes.resolve_preset(")
        assert idx_get != -1 and idx_resolve != -1, (
            "No se encontraron ambas llamadas (get_manual_preset y "
            "resolve_preset) dentro de main.py::_transcribe_final."
        )
        assert idx_get < idx_resolve, (
            "dictation_modes.get_manual_preset() debe leerse ANTES de "
            "dictation_modes.resolve_preset() en main.py::_transcribe_final: "
            "resolve_preset() CONSUME el valor, así que leerlo después ya "
            "vería el estado post-consumo (siempre None)."
        )

    def test_dictation_manual_preset_consumed_emitted_after_resolving(self):
        body = self._transcribe_final_source()
        idx_resolve = body.find("dictation_modes.resolve_preset(")
        idx_emit = body.find("self.dictation_manual_preset_consumed.emit()")
        assert idx_resolve != -1 and idx_emit != -1, (
            "No se encontró la llamada a resolve_preset o la emisión de "
            "dictation_manual_preset_consumed dentro de "
            "main.py::_transcribe_final."
        )
        assert idx_resolve < idx_emit, (
            "self.dictation_manual_preset_consumed.emit() aparece ANTES de "
            "dictation_modes.resolve_preset(...) — la señal debe emitirse "
            "DESPUÉS de resolver (que es cuando el valor manual, si había "
            "uno, ya se consumió de verdad)."
        )

    def test_resolve_preset_block_conserva_los_mismos_gates_que_smart_commands(self):
        body = self._transcribe_final_source()
        idx_resolve = body.find("dictation_modes.resolve_preset(")
        assert idx_resolve != -1
        idx_if = body.rfind("if (", 0, idx_resolve)
        assert idx_if != -1, (
            "No se encontró un 'if (' antes de dictation_modes.resolve_preset(...) "
            "en main.py. Sin un bloque de gates explícito, no hay forma de "
            "confirmar que translate/source siguen protegiendo la pasada "
            "(CLAUDE.md sección 19, Eje 2)."
        )
        gate_block = body[idx_if:idx_resolve]
        for gate in ("translate", 'source != "system"', "modes_enabled()"):
            assert gate in gate_block, (
                f"El gate '{gate}' ya no está en el bloque 'if (' que envuelve "
                "dictation_modes.resolve_preset(...) en main.py::_transcribe_final."
            )

    def test_tray_menu_ofrece_el_submenu_proximo_dictado(self):
        body = self._setup_tray_source()
        assert 'QMenu("Próximo dictado"' in body, (
            "_setup_tray ya no construye el submenú 'Próximo dictado'. Sin "
            "él, no hay forma de armar el preset manual desde la bandeja "
            "(unidad 2b)."
        )
        assert "dictation_modes.set_manual_preset(" in body, (
            "_setup_tray ya no llama a dictation_modes.set_manual_preset(...): "
            "el submenú puede seguir dibujándose sin que elegir un ítem arme "
            "de verdad el preset manual."
        )

    def test_tray_menu_incluye_salida_explicita_a_automatico(self):
        """'Cómo se vuelve al automático' necesita una salida explícita: la
        lista de opciones del submenú (`_DICTATION_PRESET_LABELS`, a nivel
        de módulo, iterada dentro de `_setup_tray`) debe traer una entrada
        `None` que arme "automático" — no solo los 5 presets concretos."""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        full_source = (repo_root / "main.py").read_text(encoding="utf-8")
        assert "(None, " in full_source and "Automático" in full_source, (
            "main.py ya no define una entrada (None, \"Automático...\") en "
            "_DICTATION_PRESET_LABELS. Sin ella, el usuario no tiene una "
            "salida clara del preset manual, solo entradas."
        )
        tray_body = self._setup_tray_source()
        assert "_DICTATION_PRESET_LABELS" in tray_body, (
            "_setup_tray ya no itera _DICTATION_PRESET_LABELS: aunque la "
            "lista siga trayendo la entrada None de vuelta a automático, el "
            "submenú de la bandeja podría no estar ofreciéndola de verdad."
        )


# ---------------------------------------------------------------------------
# (j) Panel de settings: roundtrip GET/POST + CSRF
# ---------------------------------------------------------------------------
try:
    import flask as _flask  # noqa: F401
    _has_flask = True
except ImportError:
    _has_flask = False


@pytest.mark.skipif(not _has_flask, reason="flask not installed")
class TestSettingsEndpoint:
    @pytest.fixture(autouse=True)
    def client(self, tmp_path):
        from web.server import app, _db
        _db.db_path = str(tmp_path / "test.db")
        _db._init_db()
        self.app = app
        self.client = app.test_client()

    def test_get_settings_has_dictation_fields_with_defaults(self, monkeypatch):
        monkeypatch.delenv("DICTATION_MODES_ENABLED", raising=False)
        monkeypatch.delenv("DICTATION_MODE_MAP", raising=False)
        resp = self.client.get("/api/settings")
        data = resp.get_json()
        assert data["dictation_modes_enabled"] is False
        assert "outlook.exe:email" in data["dictation_mode_map"]

    def test_post_settings_roundtrip(self):
        resp = self.client.post(
            "/api/settings",
            json={
                "dictation_modes_enabled": "true",
                "dictation_mode_map": "notepad.exe:chat",
            },
        )
        assert resp.status_code == 200
        resp2 = self.client.get("/api/settings")
        data = resp2.get_json()
        assert data["dictation_modes_enabled"] is True
        assert data["dictation_mode_map"] == "notepad.exe:chat"

    def test_post_settings_csrf_rejected_bad_origin(self):
        resp = self.client.post(
            "/api/settings",
            json={"dictation_modes_enabled": "true"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403
