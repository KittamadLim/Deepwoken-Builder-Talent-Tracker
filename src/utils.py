import json
import logging
import sys
from pathlib import Path

# When running as a PyInstaller-frozen .exe, __file__ points inside a
# temporary extraction folder (_MEIPASS).  User-facing files (config.json,
# overlay.log, debug/) must live next to the .exe so they persist across runs.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    # Dev layout: src/utils.py → parent = src/ → parent = project root.
    BASE_DIR = Path(__file__).parent.parent

# Default card-name capture regions for 1920×1080.
# Each region covers roughly the title strip at the top of one talent card.
# Use the "📌 Pick on Screen" button in Settings to re-calibrate.
_DEFAULT_CARD_REGIONS = [
    {"x": 283, "y": 235, "w": 158, "h": 42},   # card 1
    {"x": 451, "y": 235, "w": 158, "h": 42},   # card 2
    {"x": 618, "y": 235, "w": 158, "h": 42},   # card 3
    {"x": 786, "y": 235, "w": 158, "h": 42},   # card 4
    {"x": 954, "y": 235, "w": 158, "h": 42},   # card 5
]

DEFAULT_CONFIG: dict = {
    # Region covering the full card display area (all cards, full height).
    # The scanner detects individual card title banners automatically —
    # no per-card calibration needed.
    # Pick via the "Card Auto-Detect" section in Settings.
    "card_detect_region": None,
    # HSV filter parameters for the gold-border fallback detector.
    # Defaults target the warm gold/amber Deepwoken card frame.
    # Adjust if detection misses cards on unusual monitors/color profiles.
    "card_detect_hsv": {
        "h_lo": 12, "h_hi": 38,
        "s_lo": 80, "s_hi": 255,
        "v_lo": 90, "v_hi": 255,
    },
    # Region for the right-side owned-talents panel (F7 scan)
    "talents_panel_region": {"x": 1060, "y": 100, "w": 300, "h": 650},
    "ocr_interval_ms": 500,
    "fuzzy_threshold": 72,
    # Expansion around the narrow OCR title strip for the full-card highlight box
    "card_highlight_above": 30,
    "card_highlight_below": 185,
    # Lower threshold specifically for the owned-panel one-shot scan
    "owned_fuzzy_threshold": 75,
    # Set true to dump preprocessed OCR images into debug/ for calibration
    "debug_images": False,
    # Set true to emit [Z] diagnostic logs every 2 s (checks Z-order & click-through state)
    "debug_zorder": False,
    # Persisted list of build talents the user has confirmed they own
    "known_owned_talents": [],
    # Global hotkeys — edit here to change bindings
    "hotkey_toggle":      "F6",
    "hotkey_scan_owned":  "F7",
    "hotkey_diag":        "F8",   # dumps Z-order + Win32 state to overlay.log
    "hotkey_reset_owned": "F9",   # clear all owned marks
    # When true, the card-highlight overlay draws colored boxes showing the
    # exact OCR scan regions, raw detected text per slot, and match scores.
    # Toggle with the "OCR Debug" button on the main overlay.
    "debug_ocr_highlight": False,
    # --- Legacy fallback settings (used when card_detect_region is unset) ---
    "ocr_regions": _DEFAULT_CARD_REGIONS,
    # Optional wide horizontal strip covering all card-name banners.
    "name_strip_region": None,
    # Calibrated region sets per card count (strip mode templates).
    "ocr_region_sets": {},
}

CONFIG_PATH = BASE_DIR / "config.json"


def setup_logging() -> None:
    log_path = BASE_DIR / "overlay.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key, value in DEFAULT_CONFIG.items():
        data.setdefault(key, value)
    # Migrate: remove obsolete keys that are no longer in DEFAULT_CONFIG
    _obsolete = ("num_cards", "debug_max_files")
    if any(k in data for k in _obsolete):
        for k in _obsolete:
            data.pop(k, None)
        save_config(data)
    return data


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
