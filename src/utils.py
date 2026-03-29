import json
import logging
from pathlib import Path

# Project root is one level above this file (src/ → project root).
# config.json, overlay.log and debug/ all live at the project root.
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
    "ocr_regions": _DEFAULT_CARD_REGIONS,
    "num_cards": 5,
    # Optional wide horizontal strip covering all card-name banners.
    # When configured, the scanner grabs this one rect and runs a single
    # Tesseract call (PSM 11) instead of one call per card — much faster
    # and naturally handles 5 OR 6 cards.
    # Set x/y/w/h after clicking "Pick Name Strip" in Settings.
    "name_strip_region": None,
    # Region for the right-side owned-talents panel (F7 scan)
    "talents_panel_region": {"x": 1060, "y": 100, "w": 300, "h": 650},
    "ocr_interval_ms": 1000,
    "fuzzy_threshold": 72,
    # Expansion around the narrow OCR title strip for the full-card highlight box
    "card_highlight_above": 30,
    "card_highlight_below": 185,
    # Lower threshold specifically for the owned-panel one-shot scan
    "owned_fuzzy_threshold": 60,
    # Set true to dump preprocessed OCR images into debug/ for calibration
    "debug_images": False,
    # Maximum number of debug images kept per image-type label.
    # Oldest files are deleted automatically after each save.
    "debug_max_files": 20,
    # Set true to emit [Z] diagnostic logs every 2 s (checks Z-order & click-through state)
    "debug_zorder": False,
    # Persisted list of build talents the user has confirmed they own
    "known_owned_talents": [],
    # Global hotkeys — edit here to change bindings
    "hotkey_toggle":     "F6",
    "hotkey_scan_owned": "F7",
    "hotkey_diag":       "F8",   # dumps Z-order + Win32 state to overlay.log
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
    # Migrate: if saved region count doesn't match num_cards, pad or trim.
    # Never reset to defaults — that would discard the user's calibrated positions.
    expected = data.get("num_cards", 5)
    current_regions = data.get("ocr_regions", [])
    if len(current_regions) != expected:
        while len(current_regions) < expected:
            current_regions.append({"x": 0, "y": 0, "w": 158, "h": 42})
        data["ocr_regions"] = current_regions[:expected]
        save_config(data)
    return data


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
