@echo off
title Deepwoken Builder Overlay — Build
echo ============================================================
echo  Building Deepwoken Builder Overlay
echo ============================================================
echo.

:: Install / upgrade PyInstaller into the same environment
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] pip install pyinstaller failed. Make sure Python is on PATH.
    pause
    exit /b 1
)

:: Clean previous build artefacts and rebuild
python -m PyInstaller DeepwokenOverlay.spec --clean --noconfirm
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
echo  The .exe must be run as Administrator (for global hotkeys).
echo ============================================================
echo.
pause
