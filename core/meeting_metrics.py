"""Métricas de conversación Yo/Ellos para el modo reunión (unidad 3.1, Ola 3).

Funciones PURAS y sin estado: reciben segmentos de voz ya anclados al eje de
audio de su canal (ver core/meeting.py::_process_window) y los segmentos de
TEXTO cronológicos del transcript, y devuelven un dict de métricas listo para
persistir como ``metrics_json`` y para el panel del dashboard (unidad 3.2).

Modelo de tiempo (decisión de debate, obligatoria):
    Cada canal vive en SU PROPIO eje de audio, anclado por MUESTRAS acumuladas
    procesadas, nunca por wall-clock de la ventana. El loopback ("Ellos") se
    salta silencios (WASAPI no entrega buffers en silencio total), así que su
    eje diverge del reloj de pared; talk-time/pct/monólogo/WPM son exactos en
    el eje del canal porque cuentan muestras reales de audio.

Sobre ``turns_approx`` (por qué es aproximado):
    Las interrupciones/solapes reales entre canales NO se calculan en v1: los
    ejes de audio de mic y loopback divergen (el loopback comprime silencios),
    por lo que comparar solapes entre ambos ejes produciría ficción. En su
    lugar, ``turns_approx`` cuenta las ALTERNANCIAS de speaker en los segmentos
    de texto ordenados cronológicamente (granularidad: ventana de ~12-22 s),
    lo que subestima turnos rápidos dentro de una misma ventana. El JSON lo
    marca con ``"approx": true``.
"""


def merge_segments(segs: list, max_gap: float = 0.3, min_dur: float = 0.2) -> list:
    """Fusiona segmentos de voz cercanos y descarta los demasiado cortos.

    Args:
        segs: [{"start": s, "end": s}] en segundos, en el eje del canal.
              No necesita venir ordenado; se ordena por "start".
        max_gap: gaps ENTRE segmentos menores que esto (s) se fusionan.
        min_dur: segmentos (ya fusionados) más cortos que esto (s) se descartan.

    Returns:
        Lista nueva de segmentos {"start", "end"} ordenada y disjunta.
    """
    if not segs:
        return []
    ordered = sorted(segs, key=lambda s: (s["start"], s["end"]))
    merged = [dict(ordered[0])]
    for seg in ordered[1:]:
        last = merged[-1]
        if seg["start"] - last["end"] < max_gap:
            last["end"] = max(last["end"], seg["end"])
        else:
            merged.append(dict(seg))
    return [s for s in merged if (s["end"] - s["start"]) >= min_dur]


def longest_monologue(segs: list) -> float:
    """Duración (s) del segmento de voz continuo más largo. 0.0 si no hay voz.

    Espera segmentos ya fusionados (merge_segments); sobre segmentos sin
    fusionar devuelve la duración del trozo individual más largo.
    """
    if not segs:
        return 0.0
    return max(s["end"] - s["start"] for s in segs)


# Umbral de monólogo largo (s): definición de Fireflies (≥90 s seguidos hablando).
MONOLOGUE_FLAG_SECONDS = 90.0


def _talk_seconds(segs: list) -> float:
    return sum(s["end"] - s["start"] for s in segs)


def compute_metrics(speech: dict, text_segments: list, duration_s: float) -> dict:
    """Calcula las métricas de conversación de una reunión terminada.

    Args:
        speech: {"Yo": [{"start","end"}...], "Ellos": [...]} en segundos, cada
                canal en SU eje de audio (anclaje por muestras). Se re-fusionan
                aquí (merge_segments) para unir voz partida en la frontera
                entre ventanas de ~20 s.
        text_segments: segmentos de texto cronológicos del transcript, dicts
                con al menos {"speaker": "Yo"|"Ellos", "text": str}.
        duration_s: duración real de la reunión (reloj de pared sin pausas).

    Returns:
        dict con: talk_yo_s, talk_ellos_s, pct_yo, pct_ellos (sobre el total
        hablado; 0.0 si nadie habló), talk_to_listen (Yo/Ellos; None si Ellos
        no habló), longest_monologue_yo_s, longest_monologue_ellos_s,
        longest_monologue_flag (≥90 s), turns_approx + approx=true (ver nota
        del módulo), questions_yo/questions_ellos (conteo de '?'), wpm_yo/
        wpm_ellos (None si el canal habló <1 s), duration_s. Redondeo a 1
        decimal en tiempos/porcentajes/wpm.
    """
    speech = speech or {}
    yo = merge_segments(list(speech.get("Yo") or []))
    ellos = merge_segments(list(speech.get("Ellos") or []))

    talk_yo = _talk_seconds(yo)
    talk_ellos = _talk_seconds(ellos)
    total_talk = talk_yo + talk_ellos

    pct_yo = (talk_yo / total_talk * 100.0) if total_talk > 0 else 0.0
    pct_ellos = (talk_ellos / total_talk * 100.0) if total_talk > 0 else 0.0
    talk_to_listen = (talk_yo / talk_ellos) if talk_ellos > 0 else None

    mono_yo = longest_monologue(yo)
    mono_ellos = longest_monologue(ellos)

    # Turnos aproximados: alternancia de speaker en el transcript cronológico.
    turns = 0
    prev_speaker = None
    words = {"Yo": 0, "Ellos": 0}
    questions = {"Yo": 0, "Ellos": 0}
    for seg in text_segments or []:
        spk = seg.get("speaker")
        text = str(seg.get("text") or "")
        if spk in words:
            words[spk] += len(text.split())
            questions[spk] += text.count("?")
        if spk != prev_speaker:
            turns += 1
            prev_speaker = spk

    def _wpm(word_count: int, talk_s: float):
        # Blindaje 0/0 y N/0: sin ≥1 s de voz medida no hay ritmo que reportar.
        if talk_s < 1.0:
            return None
        return round(word_count / (talk_s / 60.0), 1)

    return {
        "talk_yo_s": round(talk_yo, 1),
        "talk_ellos_s": round(talk_ellos, 1),
        "pct_yo": round(pct_yo, 1),
        "pct_ellos": round(pct_ellos, 1),
        "talk_to_listen": round(talk_to_listen, 2) if talk_to_listen is not None else None,
        "longest_monologue_yo_s": round(mono_yo, 1),
        "longest_monologue_ellos_s": round(mono_ellos, 1),
        "longest_monologue_flag": max(mono_yo, mono_ellos) >= MONOLOGUE_FLAG_SECONDS,
        "turns_approx": turns,
        "approx": True,  # turns_approx es por alternancia de texto, no solapes reales
        "questions_yo": questions["Yo"],
        "questions_ellos": questions["Ellos"],
        "wpm_yo": _wpm(words["Yo"], talk_yo),
        "wpm_ellos": _wpm(words["Ellos"], talk_ellos),
        "duration_s": round(float(duration_s or 0.0), 1),
    }
