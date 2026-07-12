"""Modos de dictado por app activa (unidad 6.3).

3 presets de reformateo post-dictado (email formal / chat casual / código),
aplicados vía LLM SOLO si el usuario lo activó (opt-in, ``DICTATION_MODES_ENABLED``,
default "false"). Elegidos según la app en foco al dictar (nombre del .exe capturado
en ``core/clipboard.save_frontmost_app``), vía un mapa configurable
``DICTATION_MODE_MAP`` (exe:preset,exe:preset,...).

Sin builder de modos custom (lección superwhisper): exactamente 3 presets
hardcodeados en este módulo. El usuario solo edita el MAPA exe→preset, nunca
el contenido de un preset.

Regla común de los 3 prompts, NO negociable: el LLM SOLO reformatea, nunca
añade contenido que no esté en el dictado. Salida = solo el texto, sin
comentarios ni explicaciones.
"""
import logging
import os

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Los 3 presets (contenido hardcoded, no editable desde la UI)
# ---------------------------------------------------------------------------

_COMMON_RULE = (
    "REGLA ABSOLUTA: solo reformatea el texto dictado. NUNCA agregues información, "
    "ideas, frases o datos que no estén ya presentes en el dictado. No inventes saludos, "
    "cierres, ni contenido adicional. Responde ÚNICAMENTE con el texto reformateado, "
    "sin comentarios, sin explicaciones, sin comillas envolventes."
)

PRESETS = {
    "email": (
        "Eres un editor que reformatea dictados de voz a prosa de correo electrónico formal. "
        "Aplica puntuación impecable y párrafos bien separados. Usa registro formal. "
        "Agrega un saludo o un cierre SOLO si el dictado ya los insinúa (p. ej. si el usuario "
        "dictó 'hola equipo' o 'saludos', consérvalo con formato de correo; si no dictó nada "
        "parecido, no inventes ninguno). " + _COMMON_RULE
    ),
    "chat": (
        "Eres un editor que reformatea dictados de voz a un mensaje de chat casual. "
        "Sé conciso y directo. Se permite minúsculas relajadas al inicio de frase y omitir "
        "puntos finales innecesarios. Nada de formalidad ni de párrafos largos. " + _COMMON_RULE
    ),
    "codigo": (
        "Eres un editor que reformatea dictados de voz relacionados con código o términos "
        "técnicos. Deja los términos técnicos intactos. Si el usuario dictó un identificador "
        "en snake_case o camelCase (o lo describió dictando las palabras separadas), escríbelo "
        "literal tal como corresponde al estilo dictado, sin traducir ni embellecer. No agregues "
        "comentarios de código, explicaciones ni formato adicional. " + _COMMON_RULE
    ),
}

# Preset por defecto para el textarea editable del panel Configuración.
DEFAULT_MODE_MAP = (
    "outlook.exe:email,thunderbird.exe:email,"
    "slack.exe:chat,discord.exe:chat,teams.exe:chat,whatsapp.exe:chat,"
    "code.exe:codigo,devenv.exe:codigo,pycharm64.exe:codigo,windowsterminal.exe:codigo"
)

_VALID_PRESETS = frozenset(PRESETS.keys())


def parse_mode_map(raw: str) -> dict:
    """Parsea ``DICTATION_MODE_MAP`` en un dict ``{basename_exe_lower: preset}``.

    Tolerante a formato malformado: entradas sin ':', con preset desconocido,
    con exe vacío, espacios extra, líneas/comas mezcladas, etc. se ignoran
    silenciosamente (best-effort, nunca lanza).
    """
    result: dict[str, str] = {}
    if not raw:
        return result
    # Tolerar tanto comas como saltos de línea como separador de entradas.
    raw = raw.replace("\n", ",").replace("\r", ",")
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        exe_part, _, preset_part = chunk.partition(":")
        exe = exe_part.strip().lower()
        preset = preset_part.strip().lower()
        if not exe or preset not in _VALID_PRESETS:
            continue
        result[exe] = preset
    return result


def preset_for_exe(exe_name: "str | None", mode_map: "dict | None" = None) -> "str | None":
    """Devuelve el preset ('email'/'chat'/'codigo') para el basename de exe dado,
    o None si no hay match (app no mapeada → sin reformateo)."""
    if not exe_name:
        return None
    if mode_map is None:
        mode_map = parse_mode_map(os.getenv("DICTATION_MODE_MAP", DEFAULT_MODE_MAP))
    basename = os.path.basename(exe_name).strip().lower()
    return mode_map.get(basename)


def modes_enabled() -> bool:
    return os.getenv("DICTATION_MODES_ENABLED", "false").strip().lower() == "true"


def reformat_text(text: str, preset: str, *, timeout: float = 8.0) -> "str | None":
    """Reformatea ``text`` con el preset dado vía la capa de insights (backend batch).

    Decisión [claude-cli]: si el backend batch resuelto es 'claude-cli', SE SALTA
    el reformateo (no se llama al LLM) — la latencia de arranque del CLI headless
    (~5-15s, ver core/insights.py) es inaceptable en el hot-path de un dictado
    normal (el usuario espera pegado casi instantáneo, muy distinto al análisis de
    reunión en background). Si INSIGHTS_FALLBACK está activo y hay un backend de
    respaldo (groq/openrouter) disponible, `_chat` ya reintenta automáticamente con
    ese backend por su cuenta — aquí solo evitamos INICIAR con claude-cli.

    Devuelve el texto reformateado, o None si se debe abortar (backend no apto,
    error, o timeout) — el caller pega el texto original en ese caso (fallback
    silencioso).

    ``timeout`` es un límite REAL: se implementa con un ``threading.Thread``
    propio marcado ``daemon=True`` (no un ``ThreadPoolExecutor``). Un
    ThreadPoolExecutor usado como context manager bloquea en su
    ``__exit__`` (``shutdown(wait=True)``) hasta que el worker termine,
    incluso si ``future.result(timeout=...)`` ya lanzó ``TimeoutError`` —
    eso convertía el "timeout duro de 8s" en una espera de decenas de
    segundos con el backend colgado (F11). Con un hilo daemon propio, si el
    backend no responde a tiempo, esta función retorna igual (fallback al
    texto original) y el hilo huérfano muere solo cuando el backend
    responda o el proceso termine — nunca bloquea el pegado ni el exit del
    intérprete.
    """
    if preset not in PRESETS:
        return None

    from core import insights as _insights  # noqa: PLC0415 — import perezoso (evita ciclo)

    backend = _insights._resolve_backend("batch")
    if backend == "claude-cli":
        logger.info(
            "dictation_modes: backend batch es 'claude-cli' — se salta el reformateo "
            "(latencia de arranque ~5-15s inaceptable en el hot-path del dictado)."
        )
        return None

    system_prompt = PRESETS[preset]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    import threading

    done = threading.Event()
    outcome: dict = {}

    def _call():
        try:
            outcome["result"] = _insights._chat(
                messages, task="batch", json_mode=False, temperature=0.2, max_tokens=2000
            )
        except Exception as e:  # noqa: BLE001 — se propaga al hilo llamador vía outcome
            outcome["error"] = e
        finally:
            done.set()

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()

    if not done.wait(timeout=timeout):
        logger.warning("dictation_modes: timeout (%.1fs) reformateando con preset '%s'", timeout, preset)
        return None

    if "error" in outcome:
        logger.warning("dictation_modes: fallo reformateando con preset '%s': %s", preset, outcome["error"])
        return None

    result = (outcome.get("result") or "").strip()
    if not result:
        return None
    return result
