"""Capa inteligente del modo reunión: Insight Stream + acta post-reunión.

Dos funciones sobre un LLM (Groq, modelos Llama):

  - ``update_state(state, delta)``: el **Insight Stream** con rolling state. El LLM
    recibe el estado anterior ``{temas, pendientes, propuestas}`` + el delta de
    transcript nuevo y devuelve el estado actualizado. Contexto acotado (no
    re-resume todo): barato y estable. El prompt PREFIERE CALLAR a inventar, y
    separa lo robusto (temas/pendientes) de lo frágil (propuestas, con confianza).

  - ``generate_minutes(transcript)``: el **acta** post-reunión, una sola llamada
    que produce resumen + decisiones + temas + pendientes (Meeting Wiki).

Backend: por ahora Groq (``INSIGHTS_BACKEND=groq``, modelos Llama vía el SDK
``groq`` que ya usa la transcripción). El diseño deja la puerta abierta a
``local`` (llama-cpp-python) y ``endpoint`` (servidor OpenAI-compatible on-prem)
como siguiente paso — ver ``_chat``.

Todo es fail-safe: si el LLM no está disponible o falla, las funciones devuelven
el estado anterior / un acta vacía sin romper la reunión.
"""
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()
_last_error: str | None = None  # último error real de llamada (para surfacing en la UI)


def last_error() -> "str | None":
    """Último error de una llamada al backend de insights (None si la última fue OK)."""
    return _last_error


def _extract_json(text: str):
    """Extrae un objeto JSON de la respuesta del LLM, tolerante con modelos locales.

    Los modelos locales (vía LM Studio) a veces envuelven el JSON en ```fences```,
    añaden texto, o (si son de razonamiento) emiten un bloque <think>...</think>
    antes. Esta función limpia eso y devuelve el dict, o None si no hay JSON válido.
    """
    if not text:
        return None
    # Quitar bloques de razonamiento <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Quitar fences markdown ```json ... ```
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Último recurso: extraer el primer objeto {...} balanceado por extremos
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


class InsightsUnavailable(Exception):
    """El backend de insights no está disponible (sin clave, backend no soportado…)."""


def empty_state() -> dict:
    """Estado inicial vacío del Insight Stream."""
    return {"temas": [], "pendientes": [], "propuestas": [], "citas": []}


def is_available() -> bool:
    """¿Se puede usar la capa de insights con la configuración actual?"""
    if os.getenv("INSIGHTS_ENABLED", "true").lower().strip() != "true":
        return False
    backend = os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
    if backend == "groq":
        return bool(os.getenv("GROQ_API_KEY", "").strip())
    if backend == "endpoint":
        # Servidor OpenAI-compatible (LM Studio / on-prem). Disponible si hay URL
        # (siempre hay default). Si el servidor está caído, el fail-safe lo cubre.
        return bool(os.getenv("INSIGHTS_ENDPOINT_URL", "http://localhost:1234/v1").strip())
    # local (llama-cpp embebido): camino A, siguiente fase
    return False


def _model() -> str:
    """Modelo LLM según el backend activo.

    Groq y el endpoint (LM Studio) usan nombres distintos, así que cada uno tiene su
    propia variable: conmutar nube↔local desde el dashboard no rompe la configuración.
    """
    backend = os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
    if backend == "endpoint":
        return os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b").strip()
    return os.getenv("INSIGHTS_MODEL", "llama-3.3-70b-versatile").strip()


def _get_groq_client():
    """Lazy init del cliente Groq (reutiliza GROQ_API_KEY)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from groq import Groq  # noqa: PLC0415
                key = os.getenv("GROQ_API_KEY", "").strip()
                if not key:
                    raise InsightsUnavailable("GROQ_API_KEY no configurada")
                _client = Groq(api_key=key, timeout=30.0)
    return _client


def _chat(messages: list, *, json_mode: bool = False, temperature: float = 0.2,
          max_tokens: int = 1024) -> str:
    """Llama al LLM de insights y devuelve el contenido de texto.

    Dispatch por ``INSIGHTS_BACKEND``. Hoy solo 'groq'; 'local'/'endpoint' lanzan
    InsightsUnavailable (siguiente fase). El endpoint OpenAI-compatible será casi
    idéntico a esto cambiando base_url + api_key.
    """
    backend = os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()

    if backend == "endpoint":
        return _chat_endpoint(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)

    if backend != "groq":
        raise InsightsUnavailable(f"Backend de insights '{backend}' aún no implementado")

    kwargs = dict(model=_model(), messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _get_groq_client().chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _chat_endpoint(messages: list, *, json_mode: bool, temperature: float, max_tokens: int) -> str:
    """Llamada a un servidor OpenAI-compatible (LM Studio en local, o on-prem).

    Camino B: probar modelos locales sin descargar nada (LM Studio ya los sirve).
    Timeout de lectura generoso: la generación local puede tardar decenas de segundos.
    """
    import requests  # noqa: PLC0415
    base = os.getenv("INSIGHTS_ENDPOINT_URL", "http://localhost:1234/v1").rstrip("/")
    key = os.getenv("INSIGHTS_ENDPOINT_KEY", "lm-studio")
    # Dar holgura para modelos de razonamiento (Qwen3/R1): si no, gastan todos los
    # tokens "pensando" y devuelven vacío. Los no-razonadores paran antes igualmente.
    floor = int(os.getenv("INSIGHTS_ENDPOINT_MAX_TOKENS", "2500"))
    payload = {
        "model": _model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max(max_tokens, floor),
    }
    # NB: no enviamos response_format={"type":"json_object"} a propósito. LM Studio
    # (y otros servidores) lo rechazan: solo aceptan 'json_schema' o 'text'. Para máxima
    # compatibilidad confiamos en el prompt ("responde SOLO con JSON") + _extract_json,
    # que tolera fences, texto extra y bloques <think> de modelos de razonamiento.
    _ = json_mode  # parámetro conservado por compatibilidad de firma
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=(10, 180),  # (conexión, lectura) — la generación local puede ser lenta
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise InsightsUnavailable(f"Endpoint no disponible ({base}): {exc}") from exc
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


# ---------------------------------------------------------------------------
# Insight Stream (rolling state)
# ---------------------------------------------------------------------------

_INSIGHTS_SYSTEM = (
    "Eres un analista de reuniones. Recibes el ESTADO actual de la reunión (JSON con "
    "temas, pendientes y propuestas) y un fragmento NUEVO de la transcripción. "
    "Devuelve el estado ACTUALIZADO como objeto JSON con exactamente estas claves:\n"
    '  "temas": lista de strings (asuntos tratados; AMPLIOS y no redundantes, fusiona '
    "micro-temas relacionados; idealmente no más de ~8)\n"
    '  "pendientes": lista de objetos {"texto": string, "responsable": string|null, '
    '"fecha": string|null, "hora": string|null} — compromisos/tareas acordados; incluye '
    'responsable, fecha y hora SOLO si se mencionan (p. ej. "viernes", "15:00")\n'
    '  "propuestas": lista de objetos {"texto": string, "confianza": "alta"|"media"}\n'
    '  "citas": lista de objetos {"texto": string, "fecha": string|null, "hora": string|null} '
    "— próximas REUNIONES/citas agendadas (cuándo se vuelve a hablar y de qué); [] si no se acordó ninguna\n\n"
    "REGLAS ESTRICTAS:\n"
    "- Solo añade un pendiente o una propuesta si hay EVIDENCIA EXPLÍCITA en el texto nuevo. "
    "Es preferible OMITIR a inventar. No infieras intenciones no dichas.\n"
    "- Un pendiente es un COMPROMISO/tarea (alguien hará algo), no un simple tema. "
    "No inventes responsable, fecha ni hora: usa null si no se dijeron.\n"
    "- Una propuesta solo existe si un PARTICIPANTE sugiere explícitamente una acción. "
    "Describir, narrar, analizar o comentar un tema NO genera pendientes ni propuestas. "
    "NUNCA crees ítems del tipo 'investigar/analizar X' a partir de contenido descriptivo o "
    "informativo (ver un vídeo, comentar una noticia). Si nadie se compromete ni sugiere algo, "
    "deja pendientes y propuestas como listas VACÍAS.\n"
    "- Mantén lo que ya estaba en el estado (no borres temas/pendientes previos salvo que se "
    "resuelvan explícitamente). Acumula, no reescribas.\n"
    "- Evita duplicados: si algo ya está, no lo repitas.\n"
    "- Las propuestas son sugerencias accionables; marca 'media' si no estás seguro.\n"
    "- Responde SOLO con el objeto JSON, sin texto adicional. Todo en español."
)


def update_state(state: dict, delta_text: str) -> dict:
    """Actualiza el rolling state con el delta de transcript. Fail-safe.

    Devuelve el nuevo estado, o el anterior sin cambios si el LLM no está
    disponible, falla, o devuelve JSON inválido.
    """
    global _last_error
    if not delta_text.strip() or not is_available():
        return state
    prev = json.dumps(state, ensure_ascii=False)
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _INSIGHTS_SYSTEM},
                {"role": "user", "content": f"ESTADO ACTUAL:\n{prev}\n\nTEXTO NUEVO:\n{delta_text}"},
            ],
            json_mode=True,
            temperature=0.1,
            max_tokens=1200,
        )
        new_state = _extract_json(content)
        # Validar forma mínima; si falla, conservar el estado anterior (fail-safe)
        if not isinstance(new_state, dict) or "temas" not in new_state:
            logger.debug("Insights: JSON con forma inesperada, conservando estado previo")
            _last_error = "el modelo no devolvió un JSON válido"
            return state
        _last_error = None  # llamada OK
        return {
            "temas": new_state.get("temas", []) or [],
            "pendientes": new_state.get("pendientes", []) or [],
            "propuestas": new_state.get("propuestas", []) or [],
            "citas": new_state.get("citas", []) or [],
        }
    except InsightsUnavailable as exc:
        _last_error = str(exc)
        return state
    except Exception as exc:  # noqa: BLE001
        logger.warning("Insights: error actualizando estado (se conserva el previo): %s", exc)
        _last_error = str(exc)
        return state


# ---------------------------------------------------------------------------
# Consolidación por evento (pasada con contexto completo)
# ---------------------------------------------------------------------------

_CONSOLIDATE_SYSTEM = (
    "Eres un analista de reuniones. Recibes la TRANSCRIPCIÓN COMPLETA de la reunión hasta "
    "ahora y un BORRADOR del análisis acumulado (construido de forma incremental, puede tener "
    "temas mal nombrados, duplicados o cosas que faltan). Devuelve el análisis CONSOLIDADO como "
    "objeto JSON con exactamente estas claves:\n"
    '  "temas": lista de strings (AMPLIOS y no redundantes; fusiona micro-temas; ~8 máx)\n'
    '  "pendientes": lista de objetos {"texto": string, "responsable": string|null, '
    '"fecha": string|null, "hora": string|null} — compromisos con responsable/fecha/hora si se dijeron\n'
    '  "propuestas": lista de objetos {"texto": string, "confianza": "alta"|"media"}\n'
    '  "citas": lista de objetos {"texto": string, "fecha": string|null, "hora": string|null} '
    "— próximas reuniones/citas agendadas\n\n"
    "REGLAS:\n"
    "- Corrige y mejora con la visión completa: fusiona duplicados y temas relacionados, "
    "renombra temas confusos, añade lo importante que el borrador haya omitido.\n"
    "- Básate solo en la transcripción; no inventes. Conserva responsables ya identificados.\n"
    "- Pendientes/propuestas SOLO si un participante se compromete o sugiere una acción explícita. "
    "Describir o analizar un tema NO los genera; nunca inventes 'investigar/analizar X'.\n"
    "- Es preferible omitir a inventar. Responde SOLO con el objeto JSON. Todo en español."
)


def consolidate(transcript: str, current: dict) -> dict:
    """Pasada de consolidación: revisa el análisis con el transcript completo.

    UNA sola llamada (no iterativa). Fail-safe: si el LLM no está disponible o falla,
    devuelve el estado actual sin cambios.
    """
    if not transcript.strip() or not is_available():
        return current
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _CONSOLIDATE_SYSTEM},
                {"role": "user", "content": f"TRANSCRIPCIÓN COMPLETA:\n{transcript}\n\nBORRADOR ACTUAL:\n{json.dumps(current, ensure_ascii=False)}"},
            ],
            json_mode=True,
            temperature=0.1,
            max_tokens=1500,
        )
        data = _extract_json(content)
        if not isinstance(data, dict) or "temas" not in data:
            return current
        return {
            "temas": data.get("temas", []) or [],
            "pendientes": data.get("pendientes", []) or [],
            "propuestas": data.get("propuestas", []) or [],
            "citas": data.get("citas", []) or [],
        }
    except InsightsUnavailable:
        return current
    except Exception as exc:  # noqa: BLE001
        logger.warning("Consolidación: error (se conserva el estado actual): %s", exc)
        return current


# ---------------------------------------------------------------------------
# Acta post-reunión (Meeting Wiki)
# ---------------------------------------------------------------------------

_MINUTES_SYSTEM = (
    "Eres un asistente que redacta el ACTA de una reunión a partir de su transcripción "
    "(con hablantes 'Yo' = quien graba y 'Ellos' = los demás). También recibes el ANÁLISIS "
    "EN VIVO que se detectó durante la reunión (temas, pendientes y propuestas); úsalo como "
    "guía e incorpóralo si sigue siendo válido. Devuelve un objeto JSON con exactamente estas claves:\n"
    '  "resumen": string (2-4 frases con lo esencial)\n'
    '  "decisiones": lista de strings (decisiones tomadas; [] si no hubo)\n'
    '  "temas": lista de strings (asuntos tratados; amplios, no redundantes)\n'
    '  "pendientes": lista de objetos {"texto": string, "responsable": string|null, '
    '"fecha": string|null, "hora": string|null} — compromisos/tareas con responsable, fecha y hora si se mencionaron\n'
    '  "propuestas": lista de strings (sugerencias/ideas accionables planteadas)\n'
    '  "citas": lista de objetos {"texto": string, "fecha": string|null, "hora": string|null} '
    "— próximas reuniones/citas agendadas; [] si no hubo\n\n"
    "REGLAS:\n"
    "- Básate en la transcripción y el análisis en vivo; no inventes.\n"
    "- Conserva los pendientes y propuestas detectados en vivo si la transcripción los respalda.\n"
    "- Pendientes/propuestas SOLO si un participante se compromete o sugiere una acción explícita. "
    "Describir, narrar o analizar un tema NO los genera; NUNCA inventes 'investigar/analizar X' a "
    "partir de contenido informativo (p. ej. comentar un vídeo). Si no los hay, déjalos vacíos.\n"
    "- Si una sección no tiene contenido real, devuélvela como lista vacía.\n"
    "- Responde SOLO con el objeto JSON. Todo en español."
)


def generate_minutes(transcript: str, insights: dict | None = None) -> dict:
    """Genera el acta de la reunión (una sola llamada LLM). Fail-safe.

    Recibe opcionalmente el análisis en vivo (temas/pendientes/propuestas) para que el
    acta sea consistente con lo que vio el usuario. Devuelve un dict con claves
    resumen/decisiones/temas/pendientes/propuestas, o un acta vacía si el LLM falla.
    """
    empty = {"resumen": "", "decisiones": [], "temas": [], "pendientes": [], "propuestas": [], "citas": []}
    if not transcript.strip() or not is_available():
        return empty
    user = f"TRANSCRIPCIÓN:\n{transcript}"
    if insights:
        user += f"\n\nANÁLISIS EN VIVO DETECTADO:\n{json.dumps(insights, ensure_ascii=False)}"
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _MINUTES_SYSTEM},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            temperature=0.2,
            max_tokens=1600,
        )
        data = _extract_json(content)
        if not isinstance(data, dict):
            return empty
        return {
            "resumen": data.get("resumen", "") or "",
            "decisiones": data.get("decisiones", []) or [],
            "temas": data.get("temas", []) or [],
            "pendientes": data.get("pendientes", []) or [],
            "propuestas": data.get("propuestas", []) or [],
            "citas": data.get("citas", []) or [],
        }
    except InsightsUnavailable:
        return empty
    except Exception as exc:  # noqa: BLE001
        logger.warning("Acta: error generando el acta: %s", exc)
        return empty
