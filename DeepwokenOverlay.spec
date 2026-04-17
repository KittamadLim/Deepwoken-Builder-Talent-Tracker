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
# The built .exe is fully self-contained — no Python, Tesseract, or any other
# external software is required on the user's PC.
#   - RapidOCR (ONNX-based) handles all OCR in-process
#   - Visual C++ 2015-2022 Redistributable (x64) — usually already present
#   - Run as Administrator (required by the 'keyboard' global-hotkey library)

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

block_cipher = None

# ── RapidOCR: small package, collect everything ──────────────────────────
rapidocr_datas, rapidocr_binaries, rapidocr_hidden = collect_all("rapidocr_onnxruntime")

# ── onnxruntime: only DLLs + data files for inference (skip tools/transformers/training) ──
onnxruntime_datas    = collect_data_files("onnxruntime")
onnxruntime_binaries = collect_dynamic_libs("onnxruntime")

# Filter out data from bloated sub-packages we don't need for inference
_ort_skip = ("tools", "transformers", "quantization", "training")
onnxruntime_datas = [
    (src, dst) for src, dst in onnxruntime_datas
    if not any(part in _ort_skip for part in dst.replace("\\", "/").split("/"))
]

# ── PyQt5: we only use QtCore, QtGui, QtWidgets — let PyInstaller hooks ──
# handle the Qt DLLs + platform plugin automatically.  Do NOT collect_all.

# pytesseract is optional — only include if installed on the build machine.
try:
    import pytesseract as _pt_check  # noqa: F401
    _pytesseract_imports = ["pytesseract"]
except ImportError:
    _pytesseract_imports = []

a = Analysis(
    ["main.py"],
    pathex=[".", "src"],   # src/ added so all module imports resolve correctly
    binaries=(
        onnxruntime_binaries +
        rapidocr_binaries
    ),
    datas=(
        [("config.json", ".")]  +   # ship a default config alongside the exe
        rapidocr_datas          +
        onnxruntime_datas
    ),
    hiddenimports=(
        rapidocr_hidden +
        [
            # onnxruntime – only the C-API inference entry-points
            "onnxruntime",
            "onnxruntime.capi",
            "onnxruntime.capi._pybind_state",
            "onnxruntime.capi.onnxruntime_pybind11_state",
            # PyQt5 – only the three modules we actually import
            "PyQt5",
            "PyQt5.QtCore",
            "PyQt5.QtGui",
            "PyQt5.QtWidgets",
            "PyQt5.sip",
        ] +
        _pytesseract_imports +
        [
            "keyboard",
            "mss",
            "mss.windows",
            "rapidfuzz",
            "rapidfuzz.fuzz",
            "rapidfuzz.process",
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
    excludes=[
        "tkinter", "matplotlib", "scipy", "onnx",
        # onnxruntime sub-packages for model conversion / training
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.training",
        "onnxruntime.transformers",
        # PyQt5 modules we do NOT use — prevents hooks from pulling in DLLs
        "PyQt5.QtBluetooth",    "PyQt5.QtDBus",         "PyQt5.QtDesigner",
        "PyQt5.QtHelp",         "PyQt5.QtLocation",     "PyQt5.QtMultimedia",
        "PyQt5.QtMultimediaWidgets", "PyQt5.QtNfc",     "PyQt5.QtOpenGL",
        "PyQt5.QtPositioning",  "PyQt5.QtPrintSupport", "PyQt5.QtQml",
        "PyQt5.QtQuick",        "PyQt5.QtQuick3D",      "PyQt5.QtQuickWidgets",
        "PyQt5.QtRemoteObjects","PyQt5.QtSensors",      "PyQt5.QtSerialPort",
        "PyQt5.QtSql",          "PyQt5.QtSvg",          "PyQt5.QtTest",
        "PyQt5.QtTextToSpeech", "PyQt5.QtWebChannel",   "PyQt5.QtWebSockets",
        "PyQt5.QtWinExtras",    "PyQt5.QtXml",          "PyQt5.QtXmlPatterns",
        "PyQt5.QAxContainer",   "PyQt5.uic",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Post-analysis cleanup: strip known-bloated binaries ──────────────────
_strip_dlls = {s.lower() for s in [
    # Software OpenGL / DirectX — overlay doesn't need 3D rendering
    "opengl32sw.dll", "d3dcompiler_47.dll", "libGLESv2.dll", "libEGL.dll",
    # Qt modules we excluded above (hooks may still drag in the DLLs via deps)
    "Qt5Designer.dll", "Qt5Quick.dll", "Qt5Quick3D.dll",
    "Qt5Quick3DRender.dll", "Qt5Quick3DRuntimeRender.dll",
    "Qt5Quick3DUtils.dll", "Qt5Quick3DAssetImport.dll",
    "Qt5QuickControls2.dll", "Qt5QuickParticles.dll",
    "Qt5QuickShapes.dll", "Qt5QuickTemplates2.dll",
    "Qt5QuickTest.dll", "Qt5QuickWidgets.dll",
    "Qt5Qml.dll", "Qt5QmlModels.dll", "Qt5QmlWorkerScript.dll",
    "Qt5Bluetooth.dll", "Qt5Location.dll", "Qt5Positioning.dll",
    "Qt5PositioningQuick.dll", "Qt5Multimedia.dll",
    "Qt5MultimediaWidgets.dll", "Qt5MultimediaQuick.dll",
    "Qt5Nfc.dll", "Qt5RemoteObjects.dll", "Qt5Sensors.dll",
    "Qt5SerialPort.dll", "Qt5Sql.dll", "Qt5Svg.dll", "Qt5Test.dll",
    "Qt5TextToSpeech.dll", "Qt5WebChannel.dll", "Qt5WebSockets.dll",
    "Qt5WebView.dll", "Qt5WinExtras.dll", "Qt5XmlPatterns.dll",
    "Qt5Xml.dll", "Qt5Help.dll", "Qt5DBus.dll", "Qt5OpenGL.dll",
    "Qt5PrintSupport.dll",
]}

def _should_keep_binary(name):
    base = os.path.basename(name).lower()
    if base in _strip_dlls:
        return False
    # OpenCV ships a 27 MB ffmpeg DLL we don't use (no video capture)
    if "opencv_videoio_ffmpeg" in base:
        return False
    return True

a.binaries = [b for b in a.binaries if _should_keep_binary(b[0])]

# Strip unnecessary Qt data: QML files, plugin dirs we don't need
_strip_data_dirs = {
    "qml", "translations",
    "geoservices", "sqldrivers", "audio", "mediaservice",
    "sceneparsers", "assetimporters", "geometryloaders", "renderers",
    "texttospeech", "webview", "sensorgestures", "sensors",
    "bearer", "position", "playlistformats",
}

def _should_keep_data(dest):
    parts = dest.replace("\\", "/").lower().split("/")
    return not any(p in _strip_data_dirs for p in parts)

a.datas = [d for d in a.datas if _should_keep_data(d[1])]

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
