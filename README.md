# Deepwoken Builder Overlay

A real-time talent tracker overlay for [Deepwoken](https://deepwoken.co).  
Load your build from deepwoken.co/builder, then let the overlay watch which talent cards appear on screen and highlight the ones you still need.

[VirusTotal](https://www.virustotal.com/gui/file/c0f2d83ba643360b7d6038f87a979b1ddeb86a2fce64be9a57d9ca91475dd3d7)
---

## Features

- Paste your builder URL once — the overlay remembers it between sessions
- Continuous card scanner (F6) highlights talent cards you still need in-game
- One-shot owned-panel scan (F7) reads your character's talent list automatically
- Click a highlighted card to instantly mark the talent as owned
- Stat priority order shown for pre-shrine and post-shrine phases
- Fast in-process OCR via RapidOCR (ONNX) — no external tools needed

---

## Quick Start — Pre-built .exe (plug & play)

> **Requirements:**
> - Windows 10/11 (64-bit)
> - A CPU with AVX2 support (most CPUs from 2013 onwards — Intel Haswell / AMD Excavator and newer)
> - [Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) — required for the OCR engine. Most PCs already have this; install it if the overlay logs a DLL error on startup.

### Step 1 — Download

Download the latest release ZIP from the [Releases](../../releases) page and extract the entire `DeepwokenOverlay` folder anywhere (Desktop, Documents, etc.).

### Step 2 — Launch

Right-click **`DeepwokenOverlay.exe`** → **Run as Administrator**.  
*(Administrator is required for the global hotkeys to work.)*

### Step 3 — Load your build

A dialog appears on first launch. Paste your deepwoken.co builder URL, e.g.:  
`https://deepwoken.co/builder?id=XXXXXXXX`  
Click **Load Build**. The overlay appears with your talent list.

### Step 4 — Pick the card area

1. Open the in-game talent card selection screen (where you choose new talents).
2. In the overlay, click **⚙ Settings**.
3. Under **Card Auto-Detect ★ recommended**, click **📌 Pick Card Area**.
4. Drag a rectangle that covers **all talent cards** on screen — their full height from the top banner to the bottom edge. The detector automatically finds each card.
5. Click **OK** to save.

### Step 5 — Pick the owned-talents panel

1. Open your in-game talent list (the right-side panel showing all your talents).
2. In Settings → **Owned Talents Panel**, click **📌 Pick on Screen**.
3. Drag around the talent list panel.
4. Click **OK** to save.

### Step 6 — Start scanning

- Press **F6** to toggle the continuous card scanner on/off.
- Press **F7** to do a one-shot scan of your owned talents panel.
- Cards you still need will be highlighted with colored borders.
- Click a highlighted card to mark that talent as owned.

> Regions are saved in `config.json` and persist across restarts.  
> You only need to calibrate once per monitor resolution.

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

## Run from source (for development)

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11 + | [python.org](https://www.python.org/downloads/) |

### Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### Run

```powershell
# Must run as Administrator for global hotkeys
python main.py
```

---

## Building the .exe yourself

```powershell
# From the project folder:
build.bat
```

Output is in `dist\DeepwokenOverlay\`. Distribute the entire folder — the `.exe` needs the DLLs next to it.

---

## Project structure

```
deepwoken-builder-overlay/
├── src/                        # All Python source modules
│   ├── api.py                  # Deepwoken build API client
│   ├── highlight_overlay.py    # Transparent card-highlight window
│   ├── ocr.py                  # RapidOCR scanner + card detection
│   ├── optimizer.py            # Stat priority calculator
│   ├── overlay.py              # Main overlay UI + settings dialog
│   ├── region_picker.py        # Screen-region selector tool
│   └── utils.py                # Config load/save, logging setup
├── main.py                     # Entry point
├── config.json                 # User settings (edit via ⚙ Settings)
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
The `keyboard` library requires Administrator privileges. Right-click the `.exe` → **Run as Administrator**.

**OCR misses talent names / poor accuracy**  
- Re-pick the card area in Settings — make sure it covers the full card height.
- Enable `"debug_images": true` in `config.json` and check the `debug/` folder for preprocessed OCR images.
- Lower `fuzzy_threshold` in Settings if too many near-matches are discarded (default: 72).

**Overlay appears behind the game**  
Press **F8** and check `overlay.log` for the Z-order diagnostics report.

**Build data not loading**  
Ensure you copied the full URL including the `?id=` parameter, e.g.:  
`https://deepwoken.co/builder?id=XXXXXXXX`

---

## License

MIT

