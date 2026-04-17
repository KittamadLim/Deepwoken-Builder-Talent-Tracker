@echo off
title Deepwoken Builder Overlay — Build
echo ============================================================
echo  Building Deepwoken Builder Overlay
echo ============================================================
echo.

:: ── Locate Python ─────────────────────────────────────────────
:: Try "python" on PATH first; fall back to common install locations.
set "PY="
where python >nul 2>&1 && set "PY=python" && goto :found_py
where python3 >nul 2>&1 && set "PY=python3" && goto :found_py
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" (
    set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
    goto :found_py
)
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    goto :found_py
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :found_py
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :found_py
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :found_py
)
echo [ERROR] Python not found. Install Python 3.11+ and try again.
echo         https://www.python.org/downloads/
pause
exit /b 1

:found_py
echo  Using Python: %PY%
"%PY%" --version
echo.

:: Install / upgrade PyInstaller into the same environment
"%PY%" -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] pip install pyinstaller failed.
    pause
    exit /b 1
)

:: Clean previous build artefacts and rebuild
"%PY%" -m PyInstaller DeepwokenOverlay.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed — see output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete!
echo  Output folder:  dist\DeepwokenOverlay\
echo  Executable:     dist\DeepwokenOverlay\DeepwokenOverlay.exe
echo.
echo  Share the entire dist\DeepwokenOverlay\ folder.
echo  No Python or Tesseract needed on the target PC.
echo  The .exe must be run as Administrator (for global hotkeys).
echo ============================================================
echo.
pause
