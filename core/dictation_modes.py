"""Modos de dictado por app activa (unidad 6.3; ampliado a 5 presets en la Ola 2 de
``docs/PLAN-DICTADO-2026-07-31.md``, unidad 2a).

5 presets de reformateo post-dictado (email formal / chat casual / código / lista /
notas), aplicados vía LLM SOLO si el usuario lo activó (opt-in,
``DICTATION_MODES_ENABLED``, default "false"). Elegidos según la app en foco al
dictar (nombre del .exe capturado en ``core/clipboard.save_frontmost_app``), vía un
mapa configurable ``DICTATION_MODE_MAP`` (exe:preset,exe:preset,...).

Sin builder de modos custom (lección superwhisper): un conjunto FIJO y CURADO de
presets hardcodeados en este módulo — el usuario solo edita el MAPA exe→preset,
nunca el contenido de un preset. Pasar de 3 a 5 no rompe esa invariante (nunca fue
"exactamente 3"; ver CLAUDE.md sección 16 y la tabla de la Ola 2 del plan): cada
preset nuevo tiene que poder justificarse en UNA línea frente a los demás, o no
entra. `lista` y `notas` la pasan; un sexto candidato tendría que pasarla también.

Regla común de los 5 prompts, NO negociable: el LLM SOLO reformatea, nunca
añade contenido que no esté en el dictado. Salida = solo el texto, sin
comentarios ni explicaciones. Y — contrato del pipeline de texto, CLAUDE.md
sección 19, Eje 1 — el reformateo RESPETA los saltos de línea explícitos que
vengan de smart commands (Ola 1): un ``\\n`` que el usuario pidió con la voz
("nueva línea", "punto y aparte") no es un accidente de formato que un preset
pueda re-fluir. Ver ``_COMMON_RULE`` abajo, que es donde se aplica a los 5.
"""
import logging
import os

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Los 5 presets (contenido hardcoded, no editable desde la UI)
# ---------------------------------------------------------------------------

_COMMON_RULE = (
    "REGLA ABSOLUTA: solo reformatea el texto dictado. NUNCA agregues información, "
    "ideas, frases o datos que no estén ya presentes en el dictado. No inventes saludos, "
    "cierres, ni contenido adicional. Responde ÚNICAMENTE con el texto reformateado, "
    "sin comentarios, sin explicaciones, sin comillas envolventes. "
    "Si el texto dictado que recibes ya contiene saltos de línea (\\n), CONSÉRVALOS "
    "exactamente en su misma posición: el usuario los pidió a propósito con la voz "
    "(por ejemplo diciendo 'nueva línea' o 'punto y aparte' antes de que este texto "
    "llegara a ti), no son un accidente de formato. Nunca los borres ni fusiones dos "
    "líneas o párrafos separados por un salto en uno solo para 're-fluir' el texto."
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
    "lista": (
        "Eres un editor que reformatea un dictado de voz en una lista de viñetas, una por "
        "ítem, sin prosa alrededor. Cada idea o cosa distinta que el usuario dictó se "
        "convierte en UNA viñeta propia (empieza la línea con '- '); no escribas ninguna "
        "frase introductoria antes de la primera viñeta ni ningún cierre después de la "
        "última. REGLA DURA, específica de este preset: el número de viñetas de salida "
        "tiene que ser EXACTAMENTE el número de ítems distintos que el usuario dictó. "
        "Nunca fusiones dos ítems en una sola viñeta, nunca partas un ítem dictado como uno "
        "solo en dos viñetas, y nunca agregues un ítem que el usuario no dijo (ni siquiera "
        "uno que 'falte' o que parezca obvio completar). Puedes limpiar la redacción de cada "
        "ítem (ortografía, quitar muletillas, ordenar las palabras) pero no puedes cambiar su "
        "contenido ni inventar detalle nuevo. " + _COMMON_RULE
    ),
    "notas": (
        "Eres un editor que reformatea un dictado de voz en prosa limpia y neutra. Este es "
        "el preset MENOS invasivo de los cinco: tu único trabajo es arreglar la puntuación, "
        "las mayúsculas y, si hace falta, separar en párrafos donde el dictado cambia de "
        "idea. NO reescribas las frases del usuario, no le cambies el orden de las palabras "
        "salvo error evidente, y no le cambies el registro en ninguna dirección: nada de la "
        "formalidad de correo (sin saludo, sin cierre, sin 'estimado/a') y nada de las "
        "relajaciones de chat (sin minúscula inicial deliberada, sin omitir puntos finales). "
        "Si el usuario quería una reescritura más profunda tiene el preset de correo o el de "
        "chat; aquí, ante la duda entre tocar algo o dejarlo como está, se deja como está. "
        "Conserva las palabras que el usuario dictó. " + _COMMON_RULE
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
