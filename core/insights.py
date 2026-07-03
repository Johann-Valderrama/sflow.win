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

Backends POR TAREA: ``INSIGHTS_BACKEND_LIVE`` (update_state, consolidate) y
``INSIGHTS_BACKEND_BATCH`` (generate_minutes, chat_memory=Asistente de reuniones). Si no se
setean, heredan ``INSIGHTS_BACKEND``; si tampoco, "groq". Esto permite, p. ej.,
usar Groq para el análisis en vivo (velocidad) y OpenRouter para el acta/Asistente de reuniones
(más inteligencia y contexto).

Todo es fail-safe: si el LLM no está disponible o falla, las funciones devuelven
el estado anterior / un acta vacía sin romper la reunión.

Fallback automático (``INSIGHTS_FALLBACK``, default "true"): si el backend primario
de una tarea falla, ``_chat`` reintenta UNA vez con groq u openrouter (el primero
disponible, nunca el mismo que falló) y abre un circuit breaker por
``INSIGHTS_FALLBACK_COOLDOWN`` segundos (default 300) para no golpear un backend
roto en cada llamada — durante ese tiempo se va directo al fallback. Ver ``_chat``,
``_fallback_backend`` y ``_breaker``.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()
_anthropic_client = None
_anthropic_client_lock = threading.Lock()
_last_error: str | None = None  # último error real de llamada (para surfacing en la UI)

# ---------------------------------------------------------------------------
# Circuit breaker de fallback (ver _chat / _fallback_backend)
# ---------------------------------------------------------------------------
_breaker: dict[str, float] = {}  # backend → time.monotonic() del último fallo
_breaker_lock = threading.Lock()


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


def _resolve_backend(task: str = "live") -> str:
    """Resuelve el backend de insights para la tarea dada.

    Cadena de fallback:
      - task="batch" → INSIGHTS_BACKEND_BATCH || INSIGHTS_BACKEND || "groq"
      - task="live"  → INSIGHTS_BACKEND_LIVE  || INSIGHTS_BACKEND || "groq"
    """
    global_fallback = os.getenv("INSIGHTS_BACKEND", "groq").strip().lower() or "groq"
    if task == "batch":
        per_task = os.getenv("INSIGHTS_BACKEND_BATCH", "").strip().lower()
    else:
        per_task = os.getenv("INSIGHTS_BACKEND_LIVE", "").strip().lower()
    return per_task or global_fallback


def _backend_available(backend: str) -> bool:
    """¿Este backend concreto tiene lo que necesita para funcionar (key/CLI/URL)?

    Extraído de ``is_available`` para poder evaluar backends distintos del primario
    (p. ej. el candidato a fallback) sin duplicar la lógica por backend.
    """
    if backend == "groq":
        return bool(os.getenv("GROQ_API_KEY", "").strip())
    if backend == "endpoint":
        # Servidor OpenAI-compatible (LM Studio / on-prem). Disponible si hay URL
        # (siempre hay default). Si el servidor está caído, el fail-safe lo cubre.
        return bool(os.getenv("INSIGHTS_ENDPOINT_URL", "http://localhost:1234/v1").strip())
    if backend == "openrouter":
        return bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    if backend == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    if backend == "claude-cli":
        # Sirve para vivo Y batch: el análisis en vivo se dispara ~1 vez/min
        # (INSIGHTS_INTERVAL_SECONDS) en un hilo daemon con candado de un solo
        # escritor, así que el arranque del CLI (~5-15s) solo retrasa el insight,
        # no bloquea la captura. Coste real: consume la cuota de la suscripción.
        return _claude_cli_path() is not None
    # local (llama-cpp embebido): camino A, siguiente fase
    return False


def _fallback_enabled() -> bool:
    return os.getenv("INSIGHTS_FALLBACK", "true").strip().lower() == "true"


def _fallback_backend(exclude: str) -> "str | None":
    """Primer backend de fallback disponible, excluyendo ``exclude``.

    Orden fijo: groq (si hay GROQ_API_KEY) → openrouter (si hay OPENROUTER_API_KEY).
    NUNCA devuelve 'endpoint' (ventana de contexto local demasiado chica para ser
    un fallback útil) ni 'anthropic'/'claude-cli' (backends de intención explícita
    del usuario, no candidatos automáticos de respaldo).
    """
    candidates = []
    if os.getenv("GROQ_API_KEY", "").strip():
        candidates.append("groq")
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        candidates.append("openrouter")
    for c in candidates:
        if c != exclude:
            return c
    return None


def is_available(task: str = "live") -> bool:
    """¿Se puede usar la capa de insights con la configuración actual para la tarea dada?

    True si el backend primario está listo, o si el fallback automático está
    activado y hay un backend de respaldo disponible (aunque el primario no lo esté).
    """
    if os.getenv("INSIGHTS_ENABLED", "true").lower().strip() != "true":
        return False
    backend = _resolve_backend(task)
    if _backend_available(backend):
        return True
    if _fallback_enabled() and _fallback_backend(backend) is not None:
        return True
    return False


def _model(task: str = "live", backend: "str | None" = None) -> str:
    """Modelo LLM según el backend dado (o el activo para la tarea si no se pasa).

    Groq, el endpoint (LM Studio), OpenRouter, Anthropic y claude-cli usan nombres
    distintos, así que cada uno tiene su propia variable: conmutar entre backends
    desde el dashboard no rompe la config. Cada ``_chat_*`` pasa su propio backend
    para pedir el modelo correcto incluso cuando se ejecuta como fallback de otro.
    """
    if backend is None:
        backend = _resolve_backend(task)
    if backend == "endpoint":
        return os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b").strip()
    if backend == "openrouter":
        return os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite").strip()
    if backend == "anthropic":
        if task == "batch":
            return os.getenv("ANTHROPIC_MODEL_BATCH", "claude-sonnet-5").strip()
        return os.getenv("ANTHROPIC_MODEL_LIVE", "claude-haiku-4-5").strip()
    if backend == "claude-cli":
        return os.getenv("CLAUDE_CLI_MODEL_BATCH", "sonnet").strip()
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


def _get_anthropic_client():
    """Lazy init del cliente Anthropic oficial (reutiliza ANTHROPIC_API_KEY)."""
    global _anthropic_client
    if _anthropic_client is None:
        with _anthropic_client_lock:
            if _anthropic_client is None:
                from anthropic import Anthropic  # noqa: PLC0415
                key = os.getenv("ANTHROPIC_API_KEY", "").strip()
                if not key:
                    raise InsightsUnavailable("ANTHROPIC_API_KEY no configurada")
                _anthropic_client = Anthropic(api_key=key, timeout=60.0)
    return _anthropic_client


def _claude_cli_path() -> "str | None":
    """Resuelve la ruta al binario de Claude Code CLI (claude / claude.cmd).

    Orden: CLAUDE_CLI_PATH (env) → shutil.which("claude") → ruta típica de
    instalación npm global en Windows (%APPDATA%\\npm\\claude.cmd). None si no
    se encuentra ninguno.
    """
    override = os.getenv("CLAUDE_CLI_PATH", "").strip()
    if override and os.path.isfile(override):
        return override
    found = shutil.which("claude")
    if found:
        return found
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        npm_cmd = os.path.join(appdata, "npm", "claude.cmd")
        if os.path.isfile(npm_cmd):
            return npm_cmd
    return None


def _dispatch(backend: str, messages: list, *, task: str, json_mode: bool,
              temperature: float, max_tokens: int, reasoning: bool) -> str:
    """Enruta una llamada a la función del backend dado. Sin lógica de fallback:
    eso vive en ``_chat``. Cada rama pide su modelo con ``_model(task, backend)``
    para que un backend usado como fallback nunca resuelva el modelo del primario.
    """
    if backend == "endpoint":
        return _chat_endpoint(messages, task=task, json_mode=json_mode,
                              temperature=temperature, max_tokens=max_tokens)

    if backend == "openrouter":
        return _chat_openrouter(messages, task=task, json_mode=json_mode,
                                temperature=temperature, max_tokens=max_tokens,
                                reasoning=reasoning)

    if backend == "anthropic":
        return _chat_anthropic(messages, task=task, json_mode=json_mode,
                               temperature=temperature, max_tokens=max_tokens)

    if backend == "claude-cli":
        return _chat_claude_cli(messages, task=task, json_mode=json_mode,
                                max_tokens=max_tokens)

    if backend != "groq":
        raise InsightsUnavailable(f"Backend de insights '{backend}' aún no implementado")

    kwargs = dict(model=_model(task, "groq"), messages=messages, temperature=temperature,
                  max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _get_groq_client().chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _breaker_open(backend: str) -> bool:
    """¿El breaker de este backend sigue abierto (falló hace menos de cooldown)?"""
    with _breaker_lock:
        failed_at = _breaker.get(backend)
    if failed_at is None:
        return False
    cooldown = float(os.getenv("INSIGHTS_FALLBACK_COOLDOWN", "300") or 300)
    return (time.monotonic() - failed_at) < cooldown


def _breaker_trip(backend: str, fallback: "str | None", err: Exception) -> None:
    """Abre el breaker de ``backend`` (registra el timestamp del fallo).

    Solo se llama cuando REALMENTE se intentó el primario y falló — no cuando el
    breaker ya estaba abierto y se lo saltó (eso no debe refrescar el timestamp,
    o el primario nunca se reintentaría tras el cooldown).
    """
    cooldown = os.getenv("INSIGHTS_FALLBACK_COOLDOWN", "300") or "300"
    with _breaker_lock:
        already_open = backend in _breaker
        _breaker[backend] = time.monotonic()
    if not already_open:
        msg = str(err)[:200]
        if fallback:
            logger.warning(
                "insights: backend '%s' falló (%s) — usando '%s' durante %ss",
                backend, msg, fallback, cooldown,
            )
        else:
            logger.warning("insights: backend '%s' falló (%s) — sin fallback disponible", backend, msg)


def _chat(messages: list, *, task: str = "live", json_mode: bool = False,
          temperature: float = 0.2, max_tokens: int = 1024,
          reasoning: bool = False) -> str:
    """Llama al LLM de insights y devuelve el contenido de texto.

    Dispatch por backend resuelto para la tarea: 'groq' (default), 'endpoint'
    (LM Studio / on-prem OpenAI-compatible), 'openrouter' (nube multi-modelo),
    'anthropic' (API oficial de Anthropic) o 'claude-cli' (suscripción Claude vía
    Claude Code headless, solo tareas batch).
    El parámetro ``task`` ("live" o "batch") determina qué variable de entorno
    se usa para seleccionar el backend.
    El parámetro ``reasoning`` activa razonamiento extendido en backends que lo
    soportan (openrouter). Ignorado en groq, endpoint, anthropic y claude-cli.

    Fallback automático (INSIGHTS_FALLBACK, default "true"): si el backend primario
    falla con InsightsUnavailable, se reintenta UNA vez con un backend de respaldo
    (groq u openrouter, nunca endpoint/anthropic/claude-cli — ver _fallback_backend)
    y se abre un circuit breaker de INSIGHTS_FALLBACK_COOLDOWN segundos (default 300)
    para no reintentar el primario roto en cada llamada.
    """
    global _last_error
    backend = _resolve_backend(task)
    fallback_on = _fallback_enabled()

    # Breaker ya abierto para el primario: saltar directo al fallback sin reintentarlo
    # (no se refresca el timestamp — así el primario se reintenta tras el cooldown).
    if fallback_on and _breaker_open(backend):
        fb = _fallback_backend(backend)
        if fb is not None:
            logger.debug("insights: breaker abierto para '%s', usando fallback '%s' directo", backend, fb)
            result = _dispatch(fb, messages, task=task, json_mode=json_mode,
                               temperature=temperature, max_tokens=max_tokens, reasoning=reasoning)
            _last_error = None
            return result

    try:
        result = _dispatch(backend, messages, task=task, json_mode=json_mode,
                           temperature=temperature, max_tokens=max_tokens, reasoning=reasoning)
        _last_error = None
        return result
    except InsightsUnavailable as exc:
        _last_error = str(exc)
        fb = _fallback_backend(backend) if fallback_on else None
        _breaker_trip(backend, fb, exc)
        if fb is None:
            raise
        # Única llamada al fallback; si también falla, se propaga tal cual.
        result = _dispatch(fb, messages, task=task, json_mode=json_mode,
                           temperature=temperature, max_tokens=max_tokens, reasoning=reasoning)
        _last_error = None
        return result


def _chat_endpoint(messages: list, *, task: str = "live", json_mode: bool,
                   temperature: float, max_tokens: int) -> str:
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
        "model": _model(task, "endpoint"),
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


def _chat_openrouter(messages: list, *, task: str = "live", json_mode: bool,
                     temperature: float, max_tokens: int,
                     reasoning: bool = False) -> str:
    """Llamada a OpenRouter (nube multi-modelo, API OpenAI-compatible).

    Requiere OPENROUTER_API_KEY. Modelo configurable vía OPENROUTER_MODEL.
    No envía response_format (misma razón que _chat_endpoint: compatibilidad máxima);
    confía en el prompt + _extract_json para JSON mode.
    Timeout (10, 120): conexión rápida, generación puede tardar con modelos grandes.
    Si ``reasoning`` es True, activa razonamiento extendido vía el campo "reasoning"
    de OpenRouter (effort configurable con OPENROUTER_REASONING_EFFORT, default "medium").
    """
    import requests  # noqa: PLC0415
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise InsightsUnavailable("Falta OPENROUTER_API_KEY para el backend OpenRouter")
    base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    payload = {
        "model": _model(task, "openrouter"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning:
        effort = os.getenv("OPENROUTER_REASONING_EFFORT", "medium")
        payload["reasoning"] = {"enabled": True, "effort": effort}
    # json_mode: igual que _chat_endpoint, confiamos en prompt + _extract_json
    _ = json_mode
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "Vflow",
            },
            timeout=(10, 120),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        # Incluir el cuerpo de la respuesta (p.ej. "X is not a valid model ID") para que
        # un slug/parametro invalido no quede como fallo silencioso.
        body = ""
        resp_obj = getattr(exc, "response", None)
        if resp_obj is not None:
            try:
                body = f" — {resp_obj.text[:300]}"
            except Exception:  # noqa: BLE001
                body = ""
        raise InsightsUnavailable(f"OpenRouter no disponible: {exc}{body}") from exc
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _flatten_anthropic_content(content_blocks) -> str:
    """Concatena el texto de una lista de bloques de respuesta de Anthropic.

    Aislado en su propia función (en vez de inline en _chat_anthropic) para poder
    testearlo con objetos fake (basta con que cada bloque tenga .type y .text).
    """
    return "".join(
        b.text for b in content_blocks if getattr(b, "type", None) == "text"
    ).strip()


def _chat_anthropic(messages: list, *, task: str = "live", json_mode: bool = False,
                    temperature: float = 0.2, max_tokens: int = 1024) -> str:
    """Llamada a la API oficial de Anthropic (backend 'anthropic').

    Modelos por tarea: live → ANTHROPIC_MODEL_LIVE (default claude-haiku-4-5),
    batch → ANTHROPIC_MODEL_BATCH (default claude-sonnet-5).

    NUNCA envía temperature/top_p/top_k (claude-sonnet-5 rechaza con 400 cualquier
    parámetro de sampling no-default) ni el campo thinking (Sonnet 5 corre thinking
    adaptativo por defecto sin necesidad de configurarlo; Haiku no lo necesita).
    El parámetro ``temperature`` de la firma se recibe por compatibilidad con el
    resto de backends pero se ignora deliberadamente.
    """
    _ = temperature  # ignorado a propósito — ver docstring
    _ = json_mode  # sin response_format nativo aquí; se confía en el prompt + _extract_json

    system = ""
    user_messages = []
    for m in messages:
        if m.get("role") == "system":
            system = (system + "\n\n" + m.get("content", "")) if system else m.get("content", "")
        else:
            user_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    # El thinking adaptativo de Sonnet 5 consume presupuesto de salida: darle
    # holgura para que no trunque el JSON antes de terminar de razonar.
    floor = 6000 if task == "batch" else 1024
    effective_max_tokens = max(max_tokens, floor)

    from anthropic import APIConnectionError, APIStatusError  # noqa: PLC0415

    try:
        client = _get_anthropic_client()
        resp = client.messages.create(
            model=_model(task, "anthropic"),
            max_tokens=effective_max_tokens,
            system=system or None,
            messages=user_messages,
        )
    except InsightsUnavailable:
        raise
    except APIStatusError as exc:
        raise InsightsUnavailable(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except APIConnectionError as exc:
        raise InsightsUnavailable(f"Anthropic no disponible: {exc}") from exc

    if getattr(resp, "stop_reason", None) == "refusal":
        return ""

    return _flatten_anthropic_content(resp.content)


def _chat_claude_cli(messages: list, *, task: str = "live", json_mode: bool = False,
                     max_tokens: int = 1024) -> str:
    """Llamada a Claude Code headless (`claude -p`) usando la suscripción del usuario.

    Sirve para vivo Y batch. En vivo los insights se disparan ~1 vez/min en un hilo
    daemon con candado de un solo escritor (core/meeting.py), así que el arranque del
    CLI solo retrasa el insight, no bloquea la captura. Modelo por tarea: vivo →
    CLAUDE_CLI_MODEL_LIVE (default "haiku"), batch → CLAUDE_CLI_MODEL_BATCH ("sonnet").

    El prompt completo (system + user concatenados) se pasa por STDIN, nunca por
    argv (argv es visible para otros procesos del sistema — privacidad).
    """
    _ = json_mode  # sin flag de JSON nativo; se confía en el prompt + _extract_json
    _ = max_tokens  # el CLI no expone un límite de tokens de salida configurable

    cli_path = _claude_cli_path()
    if cli_path is None:
        raise InsightsUnavailable(
            "Claude Code CLI no encontrado — instala Claude Code y haz login (claude login)"
        )

    parts = []
    for m in messages:
        content = m.get("content", "")
        if content:
            parts.append(content)
    prompt = "\n\n".join(parts)

    if task == "batch":
        model = os.getenv("CLAUDE_CLI_MODEL_BATCH", "sonnet").strip()
    else:
        model = os.getenv("CLAUDE_CLI_MODEL_LIVE", "haiku").strip()

    # cwd: SIEMPRE un directorio neutro (%APPDATA%\Vflow), NUNCA el repo ni el
    # data dir de dev (que en dev ES la raíz del repo): si cwd cayera en el repo,
    # claude cargaría el CLAUDE.md del proyecto y el .mcp.json en cada llamada
    # (tokens y latencia desperdiciados + un servidor MCP arrancado por acta).
    appdata = os.environ.get("APPDATA", "")
    _cwd = os.path.join(appdata, "Vflow") if appdata else os.path.expanduser("~")
    try:
        os.makedirs(_cwd, exist_ok=True)
    except OSError:
        _cwd = os.path.expanduser("~")

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # Vivo: timeout corto (90s) — si el CLI cuelga, el candado de insights se
    # libera dentro de ~1.5 cadencias en vez de congelar el panel 3 min. Batch
    # (acta) puede tardar más: 180s.
    timeout = 90 if task != "batch" else 180

    try:
        result = subprocess.run(
            [cli_path, "-p", "--model", model, "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=_cwd,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise InsightsUnavailable("claude-cli superó el timeout") from exc
    except OSError as exc:
        raise InsightsUnavailable(f"claude-cli no se pudo ejecutar: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise InsightsUnavailable(f"claude-cli falló (code {result.returncode}): {stderr}")

    return (result.stdout or "").strip()


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
    if not delta_text.strip() or not is_available(task="live"):
        return state
    prev = json.dumps(state, ensure_ascii=False)
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _INSIGHTS_SYSTEM},
                {"role": "user", "content": f"ESTADO ACTUAL:\n{prev}\n\nTEXTO NUEVO:\n{delta_text}"},
            ],
            task="live",
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
    if not transcript.strip() or not is_available(task="live"):
        return current
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _CONSOLIDATE_SYSTEM},
                {"role": "user", "content": f"TRANSCRIPCIÓN COMPLETA:\n{transcript}\n\nBORRADOR ACTUAL:\n{json.dumps(current, ensure_ascii=False)}"},
            ],
            task="live",
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


def chat_memory(messages: list, *, max_tokens: int = 1024, temperature: float = 0.3,
                reasoning: bool = False) -> str:
    """Chat genérico multi-turno sobre la memoria de reuniones (Asistente de reuniones). Reusa el dispatch backend.

    Si ``reasoning`` es True, se asegura holgura de tokens (mínimo 2048) porque los
    tokens de razonamiento cuentan en el límite de salida y sin holgura se trunca la respuesta.
    """
    if reasoning:
        max_tokens = max(max_tokens, 2048)
    return _chat(messages, task="batch", json_mode=False, temperature=temperature,
                 max_tokens=max_tokens, reasoning=reasoning)


def _mmss_to_seconds(s: str) -> float:
    """Convierte 'mm:ss' o 'h:mm:ss' a segundos. Defensivo: devuelve 0 ante cualquier entrada inválida."""
    try:
        parts = str(s).strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


_CHAPTERS_SYSTEM = (
    "Eres un asistente que analiza reuniones. Recibes la transcripción de una reunión "
    "(con marcadores de tiempo [mm:ss] y hablantes 'Yo'/'Ellos') y debes identificar sus "
    "MOMENTOS CLAVE / capítulos en orden cronológico.\n\n"
    "INSTRUCCIONES:\n"
    "- Divide la reunión en sus momentos clave (típicamente 3 a 8; nunca más de 10).\n"
    "- 'inicio': copia EXACTAMENTE un timestamp [mm:ss] que APAREZCA en la transcripción "
    "(búscalo en los marcadores, NO lo inventes ni lo aproximes). Formato: 'mm:ss'.\n"
    "- 'titulo': etiqueta corta (3-6 palabras) que describa DE QUÉ se habló en ese tramo, "
    "basada SOLO en el contenido real. No inventes temas.\n"
    "- 'resumen': una frase opcional que resume el tramo. Omítela o déjala vacía si no "
    "hay suficiente contenido. No inventes datos, nombres ni decisiones.\n"
    "- Si la reunión es muy corta o sin estructura clara, devuelve 1 o 2 capítulos.\n\n"
    "Responde SOLO con un objeto JSON con esta forma exacta (sin texto extra):\n"
    '{"capitulos": [{"inicio": "mm:ss", "titulo": "...", "resumen": "..."}, ...]}\n\n'
    "REGLA ANTI-ALUCINACIÓN: si no encuentras un timestamp real para un capítulo, no lo incluyas."
)


def generate_chapters(transcript: str, segments: list | None = None) -> list:
    """Genera la línea de tiempo de momentos clave (capítulos) de la reunión. Fail-safe.

    Devuelve lista de dicts {t: float, inicio: 'mm:ss', titulo: str, resumen: str}
    ordenada por t ascendente, o [] si el LLM no está disponible o falla.
    """
    if not transcript.strip() or not is_available(task="batch"):
        return []
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _CHAPTERS_SYSTEM},
                {"role": "user", "content": f"TRANSCRIPCIÓN:\n{transcript}"},
            ],
            task="batch",
            json_mode=True,
            temperature=0.2,
            max_tokens=900,
        )
        data = _extract_json(content)
        # Acepta {"capitulos": [...]} o {"chapters": [...]} o lista directa
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = data.get("capitulos") or data.get("chapters") or []
        else:
            return []

        if not isinstance(raw_list, list):
            return []

        segs = segments or []

        result = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            titulo = (item.get("titulo") or "").strip()
            if not titulo:
                continue
            inicio_raw = str(item.get("inicio") or "0:00").strip()
            resumen = (item.get("resumen") or "").strip()

            secs = _mmss_to_seconds(inicio_raw)

            if segs:
                # Snap al segmento más cercano
                best = min(segs, key=lambda s: abs(float(s.get("t", 0)) - secs))
                t = float(best.get("t", secs))
                inicio = best.get("time", inicio_raw)
            else:
                t = secs
                inicio = inicio_raw

            result.append({"t": t, "inicio": inicio, "titulo": titulo, "resumen": resumen})

        # Ordenar por t y limitar a 10
        result.sort(key=lambda x: x["t"])
        return result[:10]

    except InsightsUnavailable:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Capítulos: error generando línea de tiempo: %s", exc)
        return []


def generate_minutes(transcript: str, insights: dict | None = None,
                     reasoning: bool = False) -> dict:
    """Genera el acta de la reunión (una sola llamada LLM). Fail-safe.

    Recibe opcionalmente el análisis en vivo (temas/pendientes/propuestas) para que el
    acta sea consistente con lo que vio el usuario. Devuelve un dict con claves
    resumen/decisiones/temas/pendientes/propuestas, o un acta vacía si el LLM falla.
    """
    empty = {"resumen": "", "decisiones": [], "temas": [], "pendientes": [], "propuestas": [], "citas": []}
    if not transcript.strip() or not is_available(task="batch"):
        return empty
    user = f"TRANSCRIPCIÓN:\n{transcript}"
    if insights:
        user += f"\n\nANÁLISIS EN VIVO DETECTADO:\n{json.dumps(insights, ensure_ascii=False)}"
    minutes_max_tokens = max(1600, 2400) if reasoning else 1600
    try:
        content = _chat(
            messages=[
                {"role": "system", "content": _MINUTES_SYSTEM},
                {"role": "user", "content": user},
            ],
            task="batch",
            json_mode=True,
            temperature=0.2,
            max_tokens=minutes_max_tokens,
            reasoning=reasoning,
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
