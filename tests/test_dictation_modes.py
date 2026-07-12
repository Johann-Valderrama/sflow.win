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
