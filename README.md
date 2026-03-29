# Deepwoken Builder Overlay

A real-time talent tracker overlay for [Deepwoken](https://deepwoken.co).  
Load your build from deepwoken.co/builder, then let the overlay watch which talent cards appear on screen and highlight the ones you still need.

---

## Features

- Paste your builder URL once — the overlay remembers it between sessions
- Continuous card scanner (F6) highlights talent cards you still need in-game
- One-shot owned-panel scan (F7) reads your character's talent list automatically
- Click a highlighted card to instantly mark the talent as owned
- Stat priority order shown for pre-shrine and post-shrine phases
- Fast in-process OCR via RapidOCR (ONNX) with Tesseract as fallback

---

## Option A — Run the pre-built .exe

> **Requirements:** Windows 10/11 (64-bit) · Visual C++ 2022 Redistributable ([download](https://aka.ms/vs/17/release/vc_redist.x64.exe))

1. Download the latest release ZIP from the [Releases](../../releases) page.
2. Extract the entire `DeepwokenOverlay` folder anywhere (e.g. `Desktop\DeepwokenOverlay\`).
3. Right-click `DeepwokenOverlay.exe` → **Run as Administrator** (required for global hotkeys).
4. A dialog appears — paste your build URL and click **Load Build**.

> **Note:** The .exe bundles RapidOCR for fast OCR.  
> If you want Tesseract as a fallback, install it separately (see below) and add it to PATH.

---

## Option B — Run from source

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11 + | [python.org](https://www.python.org/downloads/) |
| Tesseract-OCR | 5.x | Optional — only needed if RapidOCR fails |

#### Installing Tesseract (optional fallback)

1. Download the Windows installer from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki).
2. Run the installer — accept the default path (`C:\Program Files\Tesseract-OCR\`).
3. The overlay finds Tesseract automatically at that default path; no PATH change needed.

### Install Python dependencies

Open a terminal in the project folder and run:

```powershell
python -m pip install -r requirements.txt
```

This installs: PyQt5, OpenCV, RapidOCR (ONNX), pytesseract, rapidfuzz, mss, keyboard, requests, and numpy.

### Run

```powershell
# Run as Administrator for global hotkeys (F6/F7/F8/F9)
python main.py
```

A dialog appears — paste your build URL and click **Load Build**.

---

## First-time setup — Calibrating capture regions

The overlay needs to know where on your screen the talent cards and the owned-talents panel appear.  
These regions depend on your resolution and where the game window is positioned.

1. Launch the overlay and load a build.
2. Click **⚙ Settings** in the overlay.
3. For each **Card Region**, click **📌 Pick on Screen**, then drag a box over that card's name banner.
4. For the **Owned Talents Panel**, pick the right-side panel that lists all your talents in-game.
5. *(Optional)* If you prefer speed over per-card accuracy, pick a **Card Name Strip** spanning all cards at once — this runs a single OCR call instead of one per card.
6. Click **OK** to save.

Regions are saved in `config.json` and survive restarts.

---

## Hotkeys

| Key | Action |
|---|---|
| **F6** | Toggle continuous card scanner on / off |
| **F7** | One-shot scan of the in-game owned-talents panel |
| **F8** | Dump Z-order diagnostics to `overlay.log` |
| **F9** | Reset all owned-talent marks |

Hotkeys can be changed in `config.json` (`hotkey_toggle`, `hotkey_scan_owned`, `hotkey_diag`, `hotkey_reset_owned`).

---

## Talent label colours

| Colour | Meaning |
|---|---|
| **Green [✓]** | Confirmed owned (from F7 scan or by clicking a highlighted card) |
| **Yellow [→]** | Visible on screen right now — card is being highlighted |
| **Red [✗]** | Not yet owned, not currently visible |
| **Grey [ ]** | Tracking paused — last known state |

---

## Building the .exe yourself

Requires the source dependencies plus PyInstaller:

```powershell
# From the project folder, double-click or run:
build.bat
```

Output is in `dist\DeepwokenOverlay\`.  Distribute the entire folder — the `.exe` alone won't work without the DLLs next to it.

---

## Project structure

```
deepwoken-builder-overlay/
├── src/                        # All Python source modules
│   ├── api.py                  # Deepwoken build API client
│   ├── highlight_overlay.py    # Transparent card-highlight window
│   ├── ocr.py                  # RapidOCR / Tesseract scanner threads
│   ├── optimizer.py            # Stat priority calculator
│   ├── overlay.py              # Main overlay UI + settings dialog
│   ├── region_picker.py        # Screen-region selector tool
│   └── utils.py                # Config load/save, logging setup
├── main.py                     # Entry point
├── config.json                 # Default settings (edit via ⚙ Settings)
├── requirements.txt
├── DeepwokenOverlay.spec       # PyInstaller build spec
├── build.bat                   # One-click build script
└── README.md
```

---

## Troubleshooting

**"DLL load failed" on startup**  
Install the [Visual C++ 2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe).

**Global hotkeys (F6/F7) don't work**  
The `keyboard` library requires Administrator privileges on Windows. Right-click the `.exe` (or your terminal) → **Run as Administrator**.

**OCR misses talent names / poor accuracy**  
- Open Settings and re-pick your card regions more tightly around the name banners.
- Try enabling the **Card Name Strip** region for better multi-card accuracy.
- Check `debug/` folder — enable `"debug_images": true` in `config.json` to save pre-processed OCR images for inspection.
- Lower `fuzzy_threshold` in Settings if too many near-matches are being discarded (default: 72).

**Overlay appears behind the game**  
Click **🔍 Diag** in the overlay and check the Z-order report, or press F8 and open `overlay.log`.

**Build data not loading**  
Ensure you copied the full URL including the `?id=` parameter, e.g.:  
`https://deepwoken.co/builder?id=XXXXXXXX`

---

## License

MIT

