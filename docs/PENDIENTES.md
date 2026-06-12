# Pendientes y opciones futuras de Vflow

Última actualización: 2026-06-12. Estado del proyecto: backend local + diccionario + VAD + lock de seguridad commiteados en `windows-variant` y en uso diario vía `run.bat` (modo dev).

## 1. Build del .exe (POSPUESTO deliberadamente)

**Qué es:** generar `Vflow.exe` con `build.bat` (PyInstaller) para usar la app sin venv/consola, con arranque con Windows.

**Cuándo retomarlo** (cualquiera de estas señales):
- (a) Querer usar Vflow en otra máquina.
- (b) Querer compartirlo con alguien.
- (c) El proyecto se estabilice y convenga congelar una versión "de uso diario" separada del código en desarrollo.

**El terreno ya está preparado — checklist al retomarlo:**
1. `build.bat` ya corre `pip-audit` como paso previo; instalar deps con `pip install --require-hashes -r requirements.lock` para build reproducible.
2. `vflow.spec` ya incluye lo del backend local: `collect_all` de `ctranslate2`, `faster_whisper`, `av` (DLLs FFmpeg) y `onnxruntime` (VAD). **Nunca probado en build real** — validar primero.
3. El .exe pesará ~900 MB con las libs locales (antes ~50-80 MB). El modelo NO va dentro: se descarga on-demand a `%APPDATA%\Vflow\models` desde el dashboard.
4. Validar en máquina limpia (o usuario Windows limpio): arranque, modo Groq, descarga de modelo, modo local (el crash de OpenMP se mitigó importando `ctranslate2` antes de PyQt6 en `main.py` — verificar que PyInstaller preserva ese orden), VAD, diccionario.
5. El salto de tamaño puede disparar SmartScreen: mantener `version_info.txt` actualizado; considerar firma de código si se distribuye.
6. Subir `APP_VERSION` en `config.py`.

## 2. Diccionario v2 (cuando haya volumen real de entradas)

El esquema ya está preparado (`source`, `hit_count` en la tabla `dictionary`):
- Orden por frecuencia de uso en la UI (hit_count ya se registra).
- Sugerencias semi-automáticas: detectar candidatos y proponerlos como `source='suggested'` en una bandeja de revisión — **sugerir, nunca auto-aplicar**.
- Selección múltiple / borrado en lote.
- Decidido NUNCA: tags/categorías, selección de vocabulario por contexto/app activa.

## 3. Otras opciones identificadas (sin compromiso)

- **sherpa-onnx** como backend alternativo más ligero si algún día el target es hardware modesto (~800 MB menos de RAM que faster-whisper con small).
- **Modelo `medium`** local: ya soportado por el selector del dashboard; solo descargar si `small` falla con vocabulario difícil (probar antes el diccionario, que suele bastar).
- **Regenerar `requirements.lock`** al añadir cualquier dependencia: `pip-compile --generate-hashes --allow-unsafe --output-file requirements.lock requirements.in` (política: paquetes con >30 días en PyPI).

## Decisiones de diseño que NO revisar sin motivo

- En modo local nada sale a internet; el fallback a Groq es opt-in explícito (`GROQ_FALLBACK`, default false).
- Traducción local solo →inglés (límite de Whisper); otros idiomas requieren Groq.
- Filtro de alucinaciones vive en `core/transcriber.py`, agnóstico al backend, ANTES de los reemplazos del diccionario.
- `import ctranslate2` antes de PyQt6 en `main.py` — no mover (evita crash nativo OpenMP 0xC0000005).
