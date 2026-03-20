@echo off
REM build.bat — Build SFlow.exe from source (Windows)
setlocal

echo === SFlow Build ===
echo.

REM --- Step 1: Check for icon ---
echo [1/4] Icono...
if not exist "SFlow.ico" (
    echo    AVISO: SFlow.ico no encontrado. Crea un .ico desde logo.png antes de construir.
    echo    Puedes usar: magick logo.png -resize 256x256 SFlow.ico
)

REM --- Step 2: Install PyInstaller ---
echo [2/4] Instalando PyInstaller...
pip install pyinstaller --quiet 2>nul

REM --- Step 3: Clean ---
echo [3/4] Limpiando builds anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM --- Step 4: Build ---
echo [4/4] Construyendo .exe (esto toma ~1-2 min)...
pyinstaller sflow.spec --noconfirm

echo.
echo === BUILD COMPLETO ===
echo.
echo   Archivo: %cd%\dist\SFlow\SFlow.exe
echo.
echo   Para ejecutar:
echo     dist\SFlow\SFlow.exe
echo.

REM Open dist folder
explorer dist\SFlow
