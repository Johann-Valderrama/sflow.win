"""Tests de la unidad 3.1 'Motor de conversation intelligence Yo/Ellos' (Ola 3).

Cubre:
  i.   Funciones puras de core/meeting_metrics.py (merge, monólogo, turns,
       questions, wpm con 0/0 y N/0, talk_to_listen, pct con total=0) +
       round-trip de metrics_json en una DB temporal + meetings_recent.
  ii.  Fixture de AUDIO real (TTS offline con Windows SAPI): pipeline completo
       speech_timestamps + anclaje por muestras + merge + compute_metrics con
       proporciones conocidas (Yo = 20s voz + 40s silencio; Ellos = 40s de voz
       sin silencios, simulando el dropout del loopback WASAPI).
  iii. Reorden de pause(): _paused ya es True cuando _flush_window corre.

No llama a ningún LLM ni arranca audio en vivo. El TTS se genera on-the-fly en
un tmpdir de sesión (requiere Windows con System.Speech; si PowerShell/SAPI no
está disponible, los tests de audio se saltan con skip explícito).
"""

import json
import subprocess
import time
import wave

import numpy as np
import pytest

from core import meeting_metrics as mm

SR = 16000


# ---------------------------------------------------------------------------
# i. Funciones puras
# ---------------------------------------------------------------------------

class TestMergeSegments:
    def test_fuses_gaps_below_max_gap(self):
        segs = [{"start": 0.0, "end": 1.0}, {"start": 1.2, "end": 2.0}]
        out = mm.merge_segments(segs, max_gap=0.3, min_dur=0.2)
        assert out == [{"start": 0.0, "end": 2.0}]

    def test_keeps_gaps_at_or_above_max_gap(self):
        segs = [{"start": 0.0, "end": 1.0}, {"start": 1.3, "end": 2.0}]
        out = mm.merge_segments(segs, max_gap=0.3, min_dur=0.2)
        assert len(out) == 2

    def test_drops_short_segments_after_merge(self):
        segs = [{"start": 0.0, "end": 0.1}, {"start": 5.0, "end": 5.15}]
        assert mm.merge_segments(segs) == []

    def test_short_pieces_that_merge_into_long_survive(self):
        # Dos trozos de 0.15s con gap 0.1s → fusionados 0.4s ≥ min_dur.
        segs = [{"start": 0.0, "end": 0.15}, {"start": 0.25, "end": 0.4}]
        out = mm.merge_segments(segs)
        assert out == [{"start": 0.0, "end": 0.4}]

    def test_unsorted_input_and_empty(self):
        assert mm.merge_segments([]) == []
        segs = [{"start": 3.0, "end": 4.0}, {"start": 0.0, "end": 1.0}]
        out = mm.merge_segments(segs)
        assert out[0]["start"] == 0.0 and out[1]["start"] == 3.0


class TestLongestMonologue:
    def test_empty(self):
        assert mm.longest_monologue([]) == 0.0

    def test_max_duration(self):
        segs = [{"start": 0.0, "end": 5.0}, {"start": 10.0, "end": 25.0}]
        assert mm.longest_monologue(segs) == pytest.approx(15.0)


class TestComputeMetrics:
    def _speech(self, yo=None, ellos=None):
        return {"Yo": yo or [], "Ellos": ellos or []}

    def test_all_zero_no_speech_no_text(self):
        m = mm.compute_metrics(self._speech(), [], 0.0)
        assert m["talk_yo_s"] == 0.0 and m["talk_ellos_s"] == 0.0
        assert m["pct_yo"] == 0.0 and m["pct_ellos"] == 0.0  # total=0 → 0, no ZeroDivision
        assert m["talk_to_listen"] is None                    # ellos=0 → None
        assert m["wpm_yo"] is None and m["wpm_ellos"] is None  # 0/0 blindado
        assert m["turns_approx"] == 0
        assert m["longest_monologue_flag"] is False
        assert m["approx"] is True

    def test_wpm_none_when_talk_below_1s(self):
        # N palabras / ~0 s de voz (N/0): voz de 0.5s → wpm None aunque haya texto.
        speech = self._speech(yo=[{"start": 0.0, "end": 0.5}])
        text = [{"speaker": "Yo", "text": "muchas palabras aqui presentes ahora"}]
        m = mm.compute_metrics(speech, text, 10.0)
        assert m["wpm_yo"] is None

    def test_wpm_computed_when_talk_at_least_1s(self):
        speech = self._speech(yo=[{"start": 0.0, "end": 60.0}])
        text = [{"speaker": "Yo", "text": " ".join(["palabra"] * 120)}]
        m = mm.compute_metrics(speech, text, 60.0)
        assert m["wpm_yo"] == pytest.approx(120.0)

    def test_talk_to_listen_none_if_ellos_zero(self):
        m = mm.compute_metrics(self._speech(yo=[{"start": 0, "end": 10}]), [], 10.0)
        assert m["talk_to_listen"] is None
        assert m["pct_yo"] == 100.0

    def test_pct_and_ratio(self):
        speech = self._speech(yo=[{"start": 0, "end": 20}], ellos=[{"start": 0, "end": 40}])
        m = mm.compute_metrics(speech, [], 60.0)
        assert m["pct_yo"] == pytest.approx(33.3, abs=0.1)
        assert m["pct_ellos"] == pytest.approx(66.7, abs=0.1)
        assert m["talk_to_listen"] == pytest.approx(0.5)

    def test_turns_approx_by_alternation(self):
        text = [
            {"speaker": "Yo", "text": "hola"},
            {"speaker": "Yo", "text": "sigo yo"},
            {"speaker": "Ellos", "text": "respuesta"},
            {"speaker": "Yo", "text": "cierre"},
        ]
        m = mm.compute_metrics(self._speech(), text, 30.0)
        assert m["turns_approx"] == 3  # Yo → Ellos → Yo (alternancias + arranque)

    def test_questions_per_channel(self):
        text = [
            {"speaker": "Yo", "text": "¿como estas? ¿todo bien?"},
            {"speaker": "Ellos", "text": "bien. ¿y tu?"},
        ]
        m = mm.compute_metrics(self._speech(), text, 5.0)
        assert m["questions_yo"] == 2
        assert m["questions_ellos"] == 1

    def test_monologue_flag_at_90s(self):
        speech = self._speech(ellos=[{"start": 0.0, "end": 90.0}])
        m = mm.compute_metrics(speech, [], 100.0)
        assert m["longest_monologue_flag"] is True
        assert m["longest_monologue_ellos_s"] == 90.0

    def test_cross_window_segments_remerged_for_monologue(self):
        # Voz partida en la frontera de dos ventanas (gap 0.1s) cuenta como un monólogo.
        speech = self._speech(yo=[{"start": 0.0, "end": 19.95}, {"start": 20.05, "end": 40.0}])
        m = mm.compute_metrics(speech, [], 40.0)
        assert m["longest_monologue_yo_s"] == pytest.approx(40.0, abs=0.1)


class TestDbRoundTrip:
    def test_metrics_json_roundtrip_and_recent_column(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test.db"))
        metrics = mm.compute_metrics(
            {"Yo": [{"start": 0.0, "end": 20.0}], "Ellos": [{"start": 0.0, "end": 40.0}]},
            [{"speaker": "Yo", "text": "hola ¿que tal?"},
             {"speaker": "Ellos", "text": " ".join(["palabra"] * 80)}],
            60.0,
        )
        mid = db.meeting_insert(
            title="Reunión test", transcript="[00:00 Yo] hola",
            segments_json="[]", duration_seconds=60.0,
            metrics_json=json.dumps(metrics, ensure_ascii=False),
        )
        row = db.meeting_get(mid)
        assert json.loads(row["metrics_json"]) == metrics

        recent = db.meetings_recent(limit=5)
        assert recent and "metrics_json" in recent[0]
        assert json.loads(recent[0]["metrics_json"])["talk_ellos_s"] == 40.0

    def test_insert_without_metrics_leaves_null(self, tmp_path):
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=str(tmp_path / "test2.db"))
        mid = db.meeting_insert(title="x", transcript="y", segments_json="[]",
                                duration_seconds=1.0)
        assert db.meeting_get(mid)["metrics_json"] is None


# ---------------------------------------------------------------------------
# iii. Reorden de pause(): _paused=True ANTES del flush (corrección 4)
# ---------------------------------------------------------------------------

class TestPauseOrdering:
    def test_paused_is_true_when_flush_window_runs(self, monkeypatch):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = time.monotonic()

        seen = {}

        def fake_flush(window_start):
            seen["paused_at_flush"] = m._paused

        monkeypatch.setattr(m, "_flush_window", fake_flush)
        res = m.pause()
        assert res["ok"] is True and res["paused"] is True
        assert seen["paused_at_flush"] is True, (
            "_flush_window corrió con _paused=False: frames pre-pausa pueden "
            "colarse en la ventana post-pausa"
        )


# ---------------------------------------------------------------------------
# ii. Fixture de audio real (TTS Windows SAPI) — pipeline completo
# ---------------------------------------------------------------------------

_TTS_TEXT = (
    "Buenos días a todos, vamos a comenzar la reunión de seguimiento del proyecto. "
    "El primer punto de la agenda es el estado del desarrollo de la plataforma, "
    "donde el equipo ha completado la integración del módulo de reportes y está "
    "trabajando en las pruebas automatizadas del panel de control. "
    "El segundo punto es la planificación del próximo trimestre, incluyendo los "
    "objetivos de crecimiento, la contratación de dos personas para el equipo de "
    "producto, y la revisión del presupuesto de infraestructura en la nube. "
    "Finalmente hablaremos de los comentarios de los clientes sobre la última "
    "versión, que en general han sido muy positivos aunque hay algunas peticiones "
    "de mejora en el rendimiento de la búsqueda y en la exportación de documentos. "
    "Recuerden que la próxima semana tenemos la demostración con el cliente "
    "principal, así que necesitamos tener todos los entregables listos el jueves."
)


def _tts_to_wav(path: str) -> bool:
    """Genera voz TTS offline (Windows SAPI, System.Speech) a WAV 16k mono int16."""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        f"$s.SetOutputToWaveFile('{path}', $fmt); "
        f"$s.Speak('{_TTS_TEXT}'); $s.Dispose()"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=180,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def voice_pool(tmp_path_factory):
    """Pool de VOZ PURA int16 16k: TTS con los silencios recortados por VAD.

    Recortar los silencios del TTS con speech_timestamps da un pool donde la
    proporción voz/duración es conocida por construcción (≈100% voz), lo que
    permite fabricar canales con talk-time exacto. Se tilea si hace falta para
    alcanzar 60s de voz.
    """
    from core import vad as vad_mod

    wav_path = tmp_path_factory.mktemp("tts") / "voice.wav"
    if not _tts_to_wav(str(wav_path)):
        pytest.skip("TTS Windows SAPI no disponible (PowerShell/System.Speech)")

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == SR and wf.getnchannels() == 1 and wf.getsampwidth() == 2
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16)

    segs = vad_mod.speech_timestamps(audio.astype(np.float32) / 32768.0)
    if not segs:
        pytest.skip("VAD no detectó voz en el TTS generado")
    pool = np.concatenate([audio[int(s["start"] * SR):int(s["end"] * SR)] for s in segs])

    needed = 60 * SR
    if len(pool) < needed:
        pool = np.tile(pool, int(np.ceil(needed / len(pool))))
    return pool[:needed]


def _run_channel_pipeline(audio_int16: np.ndarray, window_s: float = 20.0):
    """Replica el pipeline de _process_window para UN canal: ventanas de ~20s,
    speech_timestamps fuera de locks, anclaje por muestras acumuladas, merge.

    Devuelve (speech_segs, tiempos_de_inferencia_por_ventana)."""
    from core import vad as vad_mod

    speech, infer_times = [], []
    base_samples = 0
    w = int(window_s * SR)
    for i in range(0, len(audio_int16), w):
        chunk = audio_int16[i:i + w]
        t0 = time.perf_counter()
        rel = vad_mod.speech_timestamps(chunk.astype(np.float32) / 32768.0)
        infer_times.append(time.perf_counter() - t0)
        base_s = base_samples / SR
        speech.extend(mm.merge_segments(
            [{"start": base_s + s["start"], "end": base_s + s["end"]} for s in rel]
        ))
        base_samples += len(chunk)
    return speech, infer_times


class TestAudioFixturePipeline:
    def test_known_proportions_and_vad_speed(self, voice_pool):
        from core import vad as vad_mod

        # Medir la 1ª carga del modelo aparte (ya puede estar caliente por el fixture).
        t0 = time.perf_counter()
        vad_mod._get_cached_vad_model()
        load_s = time.perf_counter() - t0

        # Yo (eje mic, continuo): 20s de voz + 40s de silencio = 60s.
        yo_audio = np.concatenate([voice_pool[:20 * SR],
                                   np.zeros(40 * SR, dtype=np.int16)])
        # Ellos (eje loopback): SOLO la voz, SIN silencios (dropout WASAPI) = 40s.
        ellos_audio = voice_pool[20 * SR:60 * SR]

        yo_speech, yo_times = _run_channel_pipeline(yo_audio)
        ellos_speech, ellos_times = _run_channel_pipeline(ellos_audio)

        metrics = mm.compute_metrics(
            {"Yo": yo_speech, "Ellos": ellos_speech}, [], duration_s=60.0
        )

        all_times = yo_times + ellos_times
        print(
            f"\n[3.1 fixture] talk_yo={metrics['talk_yo_s']}s (target 20±5%), "
            f"talk_ellos={metrics['talk_ellos_s']}s (target 40±5%), "
            f"pct_yo={metrics['pct_yo']}%, pct_ellos={metrics['pct_ellos']}%; "
            f"VAD por ventana: max={max(all_times)*1000:.0f}ms "
            f"avg={sum(all_times)/len(all_times)*1000:.0f}ms "
            f"(n={len(all_times)}); carga modelo={load_s*1000:.0f}ms"
        )

        assert metrics["talk_yo_s"] == pytest.approx(20.0, rel=0.05)
        assert metrics["talk_ellos_s"] == pytest.approx(40.0, rel=0.05)
        assert metrics["pct_yo"] == pytest.approx(33.3, abs=5.0)
        assert metrics["pct_ellos"] == pytest.approx(66.7, abs=5.0)
        # Con el modelo cacheado, la inferencia por ventana de ~20s debe ser <500ms.
        assert max(all_times) < 0.5, f"VAD lento: {max(all_times)*1000:.0f}ms por ventana"

    def test_metrics_json_shape_from_real_fixture(self, voice_pool, tmp_path):
        """Round-trip completo con el fixture real: la FORMA exacta del metrics_json
        que consumirá la unidad 3.2."""
        from db.database import TranscriptionDB

        yo_audio = np.concatenate([voice_pool[:20 * SR], np.zeros(40 * SR, dtype=np.int16)])
        ellos_audio = voice_pool[20 * SR:60 * SR]
        yo_speech, _ = _run_channel_pipeline(yo_audio)
        ellos_speech, _ = _run_channel_pipeline(ellos_audio)
        text_segments = [
            {"speaker": "Yo", "text": "Buenos días, ¿empezamos con la agenda?"},
            {"speaker": "Ellos", "text": " ".join(["palabra"] * 100)},
            {"speaker": "Yo", "text": "Perfecto, gracias."},
        ]
        metrics = mm.compute_metrics(
            {"Yo": yo_speech, "Ellos": ellos_speech}, text_segments, 60.0
        )
        print("\n[3.1 metrics_json ejemplo] " + json.dumps(metrics, ensure_ascii=False))

        expected_keys = {
            "talk_yo_s", "talk_ellos_s", "pct_yo", "pct_ellos", "talk_to_listen",
            "longest_monologue_yo_s", "longest_monologue_ellos_s",
            "longest_monologue_flag", "turns_approx", "approx",
            "questions_yo", "questions_ellos", "wpm_yo", "wpm_ellos", "duration_s",
        }
        assert set(metrics.keys()) == expected_keys
        assert metrics["questions_yo"] == 1
        assert metrics["turns_approx"] == 3

        db = TranscriptionDB(db_path=str(tmp_path / "fixture.db"))
        mid = db.meeting_insert(title="fixture", transcript="t", segments_json="[]",
                                duration_seconds=60.0,
                                metrics_json=json.dumps(metrics, ensure_ascii=False))
        assert json.loads(db.meeting_get(mid)["metrics_json"]) == metrics
