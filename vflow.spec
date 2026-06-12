# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Vflow — Windows voice-to-text app."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

block_cipher = None

# --- Collect sounddevice portaudio binary ---
sounddevice_datas = collect_data_files('_sounddevice_data')

# --- Collect pynput backends ---
pynput_hidden = collect_submodules('pynput')

# --- faster-whisper / ctranslate2 (backend local opcional) ---
# collect_all recoge binarios, datos y submódulos de ctranslate2, que usa
# extensiones C (.pyd) y DLLs de MKL/OpenMP que PyInstaller no detecta solo.
ct2_datas, ct2_binaries, ct2_hidden = collect_all('ctranslate2')
fw_datas, fw_binaries, fw_hidden = collect_all('faster_whisper')
# huggingface_hub, tokenizers, av y onnxruntime también son necesarios en runtime.
# av usa collect_all (no collect_submodules) porque trae DLLs de FFmpeg.
# onnxruntime lo requiere faster-whisper para el VAD (vad_filter=True).
hf_hidden = collect_submodules('huggingface_hub')
tok_hidden = collect_submodules('tokenizers')
av_datas, av_binaries, av_hidden = collect_all('av')
ort_datas, ort_binaries, ort_hidden = collect_all('onnxruntime')

# --- Data files ---
datas = [
    ('logo_small.png', '.'),
    ('logo.png', '.'),
]
datas += sounddevice_datas
datas += ct2_datas
datas += fw_datas
datas += av_datas
datas += ort_datas

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ct2_binaries + fw_binaries + av_binaries + ort_binaries,
    datas=datas,
    hiddenimports=[
        # pynput
        *pynput_hidden,
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        # PyQt6
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        # Flask
        'flask',
        'jinja2',
        'markupsafe',
        'werkzeug',
        # sounddevice
        '_sounddevice',
        'sounddevice',
        '_cffi_backend',
        # groq + httpx
        'groq',
        'httpx',
        'httpcore',
        'h11',
        'anyio',
        'sniffio',
        'certifi',
        'idna',
        # numpy
        'numpy',
        # dotenv
        'dotenv',
        # faster-whisper y dependencias (backend local opcional)
        *ct2_hidden,
        *fw_hidden,
        *hf_hidden,
        *tok_hidden,
        *av_hidden,
        *ort_hidden,
        'ctranslate2',
        'faster_whisper',
        'huggingface_hub',
        'tokenizers',
        'av',
        'onnxruntime',
        'tqdm',
        'filelock',
        'fsspec',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'test',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Vflow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon='Vflow.ico',
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Vflow',
)
