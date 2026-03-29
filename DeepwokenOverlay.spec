# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Deepwoken Builder Overlay
# Build with:  pyinstaller DeepwokenOverlay.spec --clean
#
# Output: dist\DeepwokenOverlay\DeepwokenOverlay.exe  (+ supporting files)
#
# Prerequisites for building (not for running):
#   pip install pyinstaller
#
# Prerequisites for the built exe to work on a target PC:
#   - Tesseract-OCR installed and on PATH  (if RapidOCR fails)
#   - Visual C++ 2015-2022 Redistributable (x64)  — usually already present
#   - Run as Administrator (required by the 'keyboard' global-hotkey library)

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# Collect everything (Python sources + DLLs + data files) for these heavy packages
# so no runtime ImportError / DLL-not-found surprises.
rapidocr_datas,    rapidocr_binaries,    rapidocr_hidden    = collect_all("rapidocr_onnxruntime")
onnxruntime_datas, onnxruntime_binaries, onnxruntime_hidden = collect_all("onnxruntime")
pyqt5_datas,       pyqt5_binaries,       pyqt5_hidden       = collect_all("PyQt5")

a = Analysis(
    ["main.py"],
    pathex=[".", "src"],   # src/ added so all module imports resolve correctly
    binaries=(
        onnxruntime_binaries +
        pyqt5_binaries +
        rapidocr_binaries
    ),
    datas=(
        [("config.json", ".")]  +   # ship a default config alongside the exe
        rapidocr_datas          +
        onnxruntime_datas       +
        pyqt5_datas
    ),
    hiddenimports=(
        rapidocr_hidden +
        onnxruntime_hidden +
        pyqt5_hidden +
        [
            "keyboard",
            "mss",
            "mss.windows",
            "rapidfuzz",
            "rapidfuzz.fuzz",
            "rapidfuzz.process",
            "pytesseract",
            "cv2",
            "numpy",
            "requests",
            "Shapely",
            "shapely.geometry",
            "pyclipper",
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy"],
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
    name="DeepwokenOverlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # no black console window
    uac_admin=True,     # request Administrator via UAC (needed by 'keyboard')
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["onnxruntime*.dll", "Qt*.dll"],   # don't UPX-compress large DLLs
    name="DeepwokenOverlay",
)
