import hashlib
import logging
import os
import re

# onnxruntime must be imported BEFORE cv2/PyQt5 — both ship conflicting DLLs
# on Windows and whichever loads second gets an initialization failure.
# A bare try/except here is intentional: the module is optional.
try:
    import onnxruntime as _ort  # noqa: F401
except Exception:
    pass

import cv2
import mss
import numpy as np
try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore[assignment]
from PyQt5.QtCore import QThread, pyqtSignal
from rapidfuzz import fuzz, process as fuzz_process

from utils import load_config

# Maximum debug images kept per label prefix.
# Oldest files are pruned automatically after each save.
_DEBUG_MAX_FILES = 20

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tesseract path resolution
# ---------------------------------------------------------------------------
_TESSERACT_CANDIDATES = [
    os.environ.get("TESSERACT_PATH", ""),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\{}\AppData\Local\Programs\Tesseract-OCR\tesseract.exe".format(
        os.environ.get("USERNAME", "")
    ),
]

_TESSERACT_AVAILABLE = False
if pytesseract is not None:
    for _candidate in _TESSERACT_CANDIDATES:
        if _candidate and os.path.isfile(_candidate):
            pytesseract.pytesseract.tesseract_cmd = _candidate
            _TESSERACT_AVAILABLE = True
            log.info("Tesseract found at: %s", _candidate)
            break
if not _TESSERACT_AVAILABLE:
    log.info("Tesseract not found — RapidOCR will be used as the primary OCR engine")

TESS_CONFIG_LINE  = "--psm 7 --oem 3"   # single text line (card title strip)
TESS_CONFIG_PANEL = "--psm 6 --oem 3"  # uniform block — reads every row top-to-bottom

# ---------------------------------------------------------------------------
# RapidOCR — ONNX-based in-process OCR, ~3-5× faster than Tesseract.
# Install:  pip install rapidocr-onnxruntime
# Falls back to Tesseract silently if the package is absent.
# ---------------------------------------------------------------------------
_rapid_engine: object | None = None
_rapid_available: bool | None = None  # None = not yet probed


def _get_rapid_ocr() -> object | None:
    """
    Lazy-load the RapidOCR engine.  First call loads ONNX models (~500 ms);
    every subsequent call returns the cached singleton immediately.
    Returns None silently when rapidocr-onnxruntime is not installed.
    """
    global _rapid_engine, _rapid_available
    if _rapid_available is False:
        return None
    if _rapid_engine is not None:
        return _rapid_engine
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import]
        _rapid_engine = RapidOCR()
        _rapid_available = True
        log.info("RapidOCR engine loaded (ONNX-based, no subprocess)")
    except Exception as exc:
        _rapid_available = False
        log.info("RapidOCR not available (%s) — falling back to Tesseract", exc)
    return _rapid_engine


# Category headers that appear in the owned-talents panel — never talent names
_PANEL_HEADERS = {
    "power", "outfit", "oath", "quest", "origin", "aspect",
    "mantra", "resonance", "equipment", "ability",
    # UI chrome that the OCR picks up from the panel header area
    "search",
}


def _upscale(img: np.ndarray, factor: int = 3) -> np.ndarray:
    """Upscale by integer factor — greatly improves Tesseract accuracy on small text."""
    h, w = img.shape[:2]
    return cv2.resize(img, (w * factor, h * factor), interpolation=cv2.INTER_CUBIC)


def _binarise(gray: np.ndarray) -> np.ndarray:
    """
    Auto-inverting OTSU threshold.
    If the image is mostly dark (game panel: light text on dark BG),
    invert first so Tesseract always sees *black text on white background*.
    """
    # Invert if background is darker than mid-point
    if float(np.mean(gray)) < 127:
        gray = cv2.bitwise_not(gray)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Light denoise pass
    thresh = cv2.medianBlur(thresh, 3)
    return thresh


def _preprocess_card(img: np.ndarray) -> np.ndarray:
    """For narrow single-line card-title strips."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    gray = _upscale(gray, 3)
    return _binarise(gray)


def _ocr_card_title(img: np.ndarray, debug: bool = False, region_idx: int = -1) -> tuple[str, list[str]]:
    """
    Extract talent name from a card-title image.
    Primary: adaptive threshold + PSM 7 (single line).
    Fallback: adaptive threshold + PSM 6 (uniform block) for wrapped titles
              like \"Old Habits Die Hard\" that may span two lines in the banner.
    """
    # Crop the image inward to exclude ornamental frame elements (pillars,
    # diamond ornament) that surround the text on the parchment banner.
    # These decorations are read as spurious characters (e.g. "I") by OCR.
    h_orig, w_orig = img.shape[:2]
    inset_x = max(4, int(w_orig * 0.10))   # ~10% from each side
    inset_y = max(2, int(h_orig * 0.12))   # ~12% from top/bottom
    img = img[inset_y:h_orig - inset_y, inset_x:w_orig - inset_x]

    # --- RapidOCR path (no subprocess, ~3-5× faster) ---
    rapid = _get_rapid_ocr()
    if rapid is not None:
        # 3× upscale preserving color — the Deepwoken title uses an ornate
        # serif font at ~30-50 px source height; more pixels help the CRNN
        # recognition model significantly.
        # Unsharp mask sharpens text strokes against the parchment background.
        _proc = cv2.resize(img[:, :, :3], (0, 0), fx=3, fy=3,
                           interpolation=cv2.INTER_CUBIC)
        _blur = cv2.GaussianBlur(_proc, (0, 0), 1.5)
        _proc = cv2.addWeighted(_proc, 1.5, _blur, -0.5, 0)  # unsharp mask
        if debug:
            _save_debug(_proc, f"card_r{region_idx}_rapid_input")
        result, _ = rapid(_proc)
        if result:
            # Sort text blocks left-to-right by X coordinate to ensure
            # correct reading order (RapidOCR may return them out of order).
            result.sort(key=lambda item: item[0][0][0])
            raw = " ".join(item[1] for item in result if float(item[2]) >= 0.05)
            cands = _clean_lines(raw)
            if debug:
                log.debug("[RapidOCR] card r%d → raw=%r  cands=%r", region_idx, raw, cands)
            if cands:
                return raw, cands
        # RapidOCR returned nothing usable — fall through to Tesseract.
        # This happens on very ornate/italic fonts where the ONNX model
        # assigns low confidence to every detected glyph.
        if debug:
            log.debug("[RapidOCR] card r%d no candidates — trying Tesseract", region_idx)

    # --- Tesseract fallback ---
    if not _TESSERACT_AVAILABLE:
        if rapid is None:
            log.error(
                "No OCR engine available! Install rapidocr-onnxruntime: "
                "pip install rapidocr-onnxruntime"
            )
        return "", []

    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    gray3x = _upscale(gray, 3)
    adaptive = cv2.adaptiveThreshold(
        gray3x, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
        blockSize=51, C=10,
    )
    if debug:
        _save_debug(adaptive, f"card_r{region_idx}_adaptive")

    # First attempt: single-line mode
    raw = pytesseract.image_to_string(adaptive, config="--psm 7 --oem 3").strip()
    cands = _clean_lines(raw)
    if cands:
        return raw, cands

    # Fallback: block mode — handles titles that wrap to two lines in the banner
    raw = pytesseract.image_to_string(adaptive, config="--psm 6 --oem 3").strip()
    return raw, _clean_lines(raw)


def _preprocess_panel(img: np.ndarray) -> np.ndarray:
    """
    For the tall owned-talents side panel.

    Uses the HSV *Value* channel (= max(R,G,B)) instead of the standard
    weighted-grayscale formula.  Every colour class of talent text is
    brighter than the dark panel background, so the V channel preserves
    their contrast independently of hue:
      - common talents  (white/light-grey) → V high
      - rare talents    (red hue)          → V high  ← fixed vs. luminance formula
      - advanced talents (blue-green hue)  → V high  ← fixed vs. luminance formula
      - dark background                   → V low

    After binarisation a morphological opening with a 2×2 kernel removes
    isolated single/double-pixel specks generated by talent icons without
    eroding the wider character strokes (which are ≥3 px at 3× upscale).
    """
    bgr = img[:, :, :3]  # drop alpha
    # V = max(B, G, R) — equivalent to cv2.COLOR_BGR2HSV [..., 2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    value = _upscale(value, 3)  # 3× gives better glyph resolution than 2×
    # Mild blur before thresholding: smooths the pixelated hard edges of
    # talent icons so they don't leave stray black pixels adjacent to the
    # first letter of each name after binarisation.
    value = cv2.GaussianBlur(value, (3, 3), 0)
    binary = _binarise(value)
    # Morphological opening: erode then dilate with a tiny kernel to break
    # thin connections between icon blobs and neighbouring letter strokes.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return binary


def _extract_text(img: np.ndarray, config: str) -> str:
    if not _TESSERACT_AVAILABLE:
        return ""
    return pytesseract.image_to_string(img, config=config).strip()


# Strip leading ✓/✗/unicode, spaces, pipes, dashes before the talent name
_JUNK_PREFIX = re.compile(r"^[^a-z]+")
# Strip bracket suffixes like [HVY] / [LHT] from talent names before OCR
# matching — these tags appear in build-data but are never produced by the
# in-game OCR, so they would reduce fuzzy scores for talents that carry them.
_BRACKET_SUFFIX = re.compile(r"\s*\[.*?\]")


def _clean_lines(text: str, skip_headers: bool = False) -> list[str]:
    """
    Split OCR output into candidate talent-name lines.
    - Strips leading non-alpha junk (tick marks, pipes, category labels)
    - Keeps only lines with ≥3 alpha characters
    - Optionally skips known category headers
    """
    lines = []
    for raw in text.splitlines():
        s = raw.lower().strip()
        # strip leading non-alpha chars (✓ ✗ | > * etc.)
        s = _JUNK_PREFIX.sub("", s)
        # keep only a-z, space, apostrophe, hyphen
        s = re.sub(r"[^a-z '\-]", "", s).strip()
        if len(s) < 3:
            continue
        if skip_headers and s in _PANEL_HEADERS:
            continue
        lines.append(s)
    return lines


def _tokens_present(talent_tokens: list[str], window_tokens: list[str], min_score: int = 65) -> bool:
    """
    Return True if every talent token has a fuzzy match ≥ min_score against
    at least one window token.

    This guards against shared-prefix false positives such as:
      "thresher scales" vs window "thresher claws"
        → "thresher" matches "thresher" (100 %) ✓
        → "scales"   matches "claws"    (~55 %) ✗  → rejected

    k-1 windows (merged OCR words like "speeddemon") intentionally skip this
    check because the window has fewer tokens than the talent by design.
    """
    for tt in talent_tokens:
        if not any(fuzz.ratio(tt, wt) >= min_score for wt in window_tokens):
            return False
    return True


def _score_talent_in_line(talent_clean: str, candidate: str) -> float:
    """
    Score how well talent_clean appears in a single OCR candidate line by
    testing every n-gram (sliding word window) of the candidate.

    Window sizes tried for a k-word talent:
      k-1 : catches merged OCR tokens  ("speeddemon" → "speed demon")
            — token-presence check skipped (window is intentionally shorter)
      k   : standard exact-width window ("dark rift" inside the long line)
            — token-presence check applied
      k+1 : absorbs one icon-noise prefix/suffix token ("mf dark rift")
            — token-presence check applied

    fuzz.ratio over a fixed-width window keeps the comparison symmetric.
    The token-presence check on win ≥ k prevents shared-prefix inflation:
      "thresher scales" vs "thresher claws" → ratio ~83 % but "scales" has
      no close partner in {"thresher","claws"} → window is rejected.
    """
    t_tokens = talent_clean.split()
    c_tokens = candidate.split()
    k = len(t_tokens)
    if k == 0 or len(c_tokens) == 0:
        return 0.0

    best = 0.0
    for win in range(max(1, k - 1), k + 2):  # k-1, k, k+1
        if win > len(c_tokens):
            continue
        for i in range(len(c_tokens) - win + 1):
            w_tokens = c_tokens[i : i + win]
            window = " ".join(w_tokens)
            s = fuzz.ratio(talent_clean, window)
            if s <= best:
                continue
            # For win >= k every talent token must approximately match some
            # window token — rejects shared-prefix false positives.
            if win >= k and not _tokens_present(t_tokens, w_tokens):
                continue
            # k-1 windows are only valid for merged OCR tokens
            # (e.g. "speeddemon" → "speed demon", ~95 %).
            # Require 85 minimum so a single shared word cannot drive the
            # score for a longer talent:
            #   "resolve"      vs "magical resolve"      = 63.6 % → rejected
            #   "shadowcaster" vs "adept shadowcaster"   = 80.0 % → rejected
            #   "speeddemon"   vs "speed demon"          = 95.2 % → passes
            if win < k and s < 85:
                continue
            best = s
            if best >= 100:
                return best  # can't improve
    return best


def _save_debug(img: np.ndarray, label: str) -> None:
    """Save preprocessed image to debug/ folder when config debug_images=true.
    Automatically prunes to _DEBUG_MAX_FILES most-recent files per label prefix.
    """
    try:
        import time
        from utils import BASE_DIR
        dbg_dir = BASE_DIR / "debug"
        dbg_dir.mkdir(exist_ok=True)
        fname = dbg_dir / f"{label}_{int(time.time()*1000)}.png"
        cv2.imwrite(str(fname), img)
        log.debug("Debug image saved: %s", fname)
        # Keep only the most recent _DEBUG_MAX_FILES files for this label prefix.
        existing = sorted(dbg_dir.glob(f"{label}_*.png"))
        for old in existing[:-_DEBUG_MAX_FILES]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as exc:
        log.debug("Debug image save failed: %s", exc)


def _hash_frame(img: np.ndarray) -> bytes:
    """Fast MD5 hash of a captured frame for change-detection caching."""
    return hashlib.md5(img.tobytes()).digest()

def _capture_region(sct: mss.base.MSSBase, region: dict) -> np.ndarray:
    mon = {"top": int(region["y"]), "left": int(region["x"]),
           "width": int(region["w"]), "height": int(region["h"])}
    return np.array(sct.grab(mon))


# ---------------------------------------------------------------------------
# Card detector — parchment title-banner contour detection (primary)
#                 + gold column-profile fallback
# ---------------------------------------------------------------------------

_CARD_DETECT_HSV_DEFAULT: dict = {
    "h_lo": 12, "h_hi": 38,   # warm gold/amber in OpenCV Hue 0-179
    "s_lo": 80, "s_hi": 255,  # avoid grey/unsaturated areas
    "v_lo": 90, "v_hi": 255,  # avoid very dark areas
}

_VALID_CARD_COUNTS = (3, 5, 6)


def _snap_to_valid_count(result: list[dict]) -> list[dict]:
    """Snap card list to the nearest valid Deepwoken card count (3, 5, 6)."""
    raw_n = len(result)
    if raw_n in _VALID_CARD_COUNTS:
        return result
    target = min(_VALID_CARD_COUNTS, key=lambda c: abs(c - raw_n))
    if raw_n > target:
        result.sort(key=lambda r: r["w"], reverse=True)
        result = result[:target]
        result.sort(key=lambda r: r["x"])
    log.info("[CARDDET] %d cards → snapped to %d (valid: %s)", raw_n, len(result), _VALID_CARD_COUNTS)
    return result


def _detect_parchment_banners(
    bgr: np.ndarray,
    img_h: int,
    img_w: int,
    region_x: int,
    region_y: int,
    debug: bool,
) -> list[dict]:
    """
    Primary card detector: find the parchment/tan-colored title banners.

    Each Deepwoken talent card has a distinctive ribbon/scroll title banner
    at the top.  The banner color (warm beige, low saturation, high value)
    is distinct from the gold frame (high saturation) and the dark game
    background.  Banners are physically separated even when cards are
    adjacent, making contour/connected-component analysis reliable.

    Returns [] if fewer than 3 plausible banners are found.
    """
    # Title banners are in the top ~28% of the card capture region
    banner_zone_h = max(60, min(int(img_h * 0.28), 120))
    banner_zone = bgr[:banner_zone_h]

    hsv_bz = cv2.cvtColor(banner_zone, cv2.COLOR_BGR2HSV)

    # Parchment: warm beige — LOW saturation, HIGH brightness
    # Gold frame: same hue range but HIGH saturation → excluded
    parch_lo = np.array([8, 10, 130], dtype=np.uint8)
    parch_hi = np.array([40, 135, 248], dtype=np.uint8)
    parch_mask = cv2.inRange(hsv_bz, parch_lo, parch_hi)

    # Close horizontally: connect pixles within each banner
    close_w = max(15, img_w // 80)
    kern_close = cv2.getStructuringElement(cv2.MORPH_RECT, (close_w, 5))
    parch_mask = cv2.morphologyEx(parch_mask, cv2.MORPH_CLOSE, kern_close)

    # Open: remove small noise blobs
    kern_open = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    parch_mask = cv2.morphologyEx(parch_mask, cv2.MORPH_OPEN, kern_open)

    if debug:
        _save_debug(cv2.cvtColor(parch_mask, cv2.COLOR_GRAY2BGR), "parchment_mask")

    # Connected component analysis
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(parch_mask)

    # Size filters relative to image width
    min_w = max(50, img_w // 20)   # ~96 px at 1920
    max_w = img_w // 2
    min_h = 8
    min_area = min_w * 6

    banners: list[dict] = []
    for i in range(1, num_labels):
        bx = int(stats[i, cv2.CC_STAT_LEFT])
        by = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        ba = int(stats[i, cv2.CC_STAT_AREA])
        if bw >= min_w and bw <= max_w and bh >= min_h and ba >= min_area:
            aspect = bw / max(bh, 1)
            if aspect >= 1.5:
                banners.append({"x": bx, "y": by, "w": bw, "h": bh, "area": ba})

    banners.sort(key=lambda b: b["x"])

    # Remove outliers: reject banners whose width OR Y-position differs
    # significantly from the median.  This kills false positives from card
    # description text or class-name strips that happen to match parchment hue.
    if len(banners) >= 3:
        ws = sorted(b["w"] for b in banners)
        median_w = ws[len(ws) // 2]
        ys = sorted(b["y"] for b in banners)
        median_y = ys[len(ys) // 2]
        banners = [
            b for b in banners
            if abs(b["w"] - median_w) / max(median_w, 1) < 0.50
            and abs(b["y"] - median_y) <= max(15, median_y * 0.15)
        ]

    if len(banners) < 3:
        log.info("[CARDDET] Parchment found only %d banner(s) (need ≥3)", len(banners))
        return []

    pad_x, pad_y = 4, 4
    result = []
    for b in banners:
        rx = region_x + max(0, b["x"] - pad_x)
        ry = region_y + max(0, b["y"] - pad_y)
        rw = min(b["w"] + 2 * pad_x, img_w - b["x"] + pad_x)
        rh = b["h"] + 2 * pad_y
        result.append({"x": int(rx), "y": int(ry), "w": int(rw), "h": int(rh)})

    result = _snap_to_valid_count(result)
    log.info("[CARDDET] Parchment OK: %d card(s): %s", len(result), result)
    return result


def _detect_gold_fallback(
    bgr: np.ndarray,
    img_h: int,
    img_w: int,
    region_x: int,
    region_y: int,
    hsv_cfg: dict | None,
    parchment_banners: list[dict],
    debug: bool,
) -> list[dict]:
    """
    Fallback card detector: gold column-profile with improvements.

    Uses a mid-band row slice (30%–60% of image height) where the gap
    between card frames is widest.  Also splits overly-wide spans that
    result from adjacent gold frames merging.
    """
    cfg = hsv_cfg if hsv_cfg else _CARD_DETECT_HSV_DEFAULT
    hsv_full = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h_lo"], cfg["s_lo"], cfg["v_lo"]], dtype=np.uint8)
    hi = np.array([cfg["h_hi"], cfg["s_hi"], cfg["v_hi"]], dtype=np.uint8)
    gold_mask = cv2.inRange(hsv_full, lo, hi)

    kern_dil = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    gold_mask = cv2.dilate(gold_mask, kern_dil, iterations=1)

    if debug:
        _save_debug(cv2.cvtColor(gold_mask, cv2.COLOR_GRAY2BGR), "card_detect_mask")

    # Use a mid-band where inter-card gaps are widest
    band_top = int(img_h * 0.30)
    band_bot = int(img_h * 0.60)
    mid_band = gold_mask[band_top:band_bot, :]
    band_h = max(1, band_bot - band_top)

    col_profile = mid_band.sum(axis=0).astype(np.float32) / (255.0 * band_h)
    sw = max(5, img_w // 150)
    col_smooth = np.convolve(col_profile, np.ones(sw, np.float32) / sw, mode="same")

    if float(col_smooth.max()) < 0.01:
        log.debug("[CARDDET] Gold fallback: density too low")
        return [], False

    threshold_val = max(0.015, float(col_smooth.max()) * 0.20)

    spans: list[tuple[int, int]] = []
    in_span = False
    start = 0
    for xi in range(img_w):
        if not in_span and col_smooth[xi] >= threshold_val:
            start = xi
            in_span = True
        elif in_span and col_smooth[xi] < threshold_val:
            spans.append((start, xi))
            in_span = False
    if in_span:
        spans.append((start, img_w))

    # Merge only very small gaps (< 15 px) — reduced from 30 to avoid merging adjacent cards
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < 15:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    widths = [e - s for s, e in merged]
    if widths:
        median_w = sorted(widths)[len(widths) // 2]
        min_w = max(60, int(median_w * 0.35))
    else:
        min_w = 60

    card_spans = [(s, e) for s, e in merged if e - s >= min_w]

    # Split overly-wide spans (> 1.6× median → likely two merged cards)
    if len(card_spans) >= 2:
        expected_w = sorted([e - s for s, e in card_spans])[len(card_spans) // 2]
        split_spans: list[tuple[int, int]] = []
        for s, e in card_spans:
            w = e - s
            if w > expected_w * 1.6:
                n = max(2, round(w / expected_w))
                sub_w = w / n
                for j in range(n):
                    split_spans.append((int(s + j * sub_w), int(s + (j + 1) * sub_w)))
            else:
                split_spans.append((s, e))
        card_spans = split_spans

    if not card_spans:
        log.debug("[CARDDET] Gold fallback: no valid spans")
        return [], False

    # Determine title banner Y/H from parchment banners if any were found,
    # otherwise use a heuristic position near the top of the capture region.
    if parchment_banners:
        avg_y = int(np.mean([b["y"] for b in parchment_banners]))
        avg_h = int(np.mean([b["h"] for b in parchment_banners]))
        title_y = max(0, avg_y - 4)
        title_h = avg_h + 8
    else:
        title_y = max(0, int(img_h * 0.04))
        title_h = max(35, int(img_h * 0.13))

    result: list[dict] = [
        {"x": int(region_x + s), "y": int(region_y + title_y), "w": int(e - s), "h": int(title_h)}
        for s, e in card_spans
    ]

    result = _snap_to_valid_count(result)
    log.info("[CARDDET] Gold fallback: %d card(s): %s", len(result), result)
    return result, False


def detect_cards(
    img: np.ndarray,
    region_x: int = 0,
    region_y: int = 0,
    hsv_cfg: dict | None = None,
    debug: bool = False,
) -> tuple[list[dict], bool]:
    """
    Detect Deepwoken talent card title-strip positions from a screenshot.

    Primary algorithm: **parchment banner detection** — finds the distinctive
    tan/beige title ribbon on each card using HSV color filtering +
    connected-component analysis.  Banners are physically separated between
    cards, giving reliable per-card detection.

    Fallback: **gold border column-profile** — used when parchment detection
    finds fewer than 3 banners (e.g. unusual monitor color profile).

    Parameters
    ----------
    img      : BGRA or BGR screenshot of the card-search region.
    region_x : Screen X offset of img[0,0] — screen-coord results.
    region_y : Screen Y offset of img[0,0].
    hsv_cfg  : HSV range dict for gold filter fallback.
    debug    : Save debug masks to debug/ when True.

    Returns
    -------
    (rects, is_primary) — rects is a list of {x, y, w, h} dicts in screen
    coordinates sorted left-to-right.  is_primary is True when the parchment
    method succeeded, False when the gold fallback was used.
    """
    bgr = img[:, :, :3]
    img_h, img_w = bgr.shape[:2]

    # ── Primary: parchment banner detection ────────────────────────────────
    result = _detect_parchment_banners(bgr, img_h, img_w, region_x, region_y, debug)
    if result:
        return result, True

    # ── Fallback: gold column-profile with improvements ────────────────────
    # Pass any partially-detected parchment banners for Y/H hinting.
    banner_zone_h = max(60, min(int(img_h * 0.28), 120))
    banner_zone = bgr[:banner_zone_h]
    hsv_bz = cv2.cvtColor(banner_zone, cv2.COLOR_BGR2HSV)
    parch_lo = np.array([8, 10, 130], dtype=np.uint8)
    parch_hi = np.array([40, 135, 248], dtype=np.uint8)
    parch_mask = cv2.inRange(hsv_bz, parch_lo, parch_hi)
    close_w = max(15, img_w // 80)
    kern_close = cv2.getStructuringElement(cv2.MORPH_RECT, (close_w, 5))
    parch_mask = cv2.morphologyEx(parch_mask, cv2.MORPH_CLOSE, kern_close)
    kern_open = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    parch_mask = cv2.morphologyEx(parch_mask, cv2.MORPH_OPEN, kern_open)
    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(parch_mask)
    min_bw = max(50, img_w // 20)
    partial_banners: list[dict] = []
    for i in range(1, num_labels):
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        ba = int(stats[i, cv2.CC_STAT_AREA])
        if bw >= min_bw and bh >= 8 and ba >= min_bw * 6:
            partial_banners.append({
                "x": int(stats[i, cv2.CC_STAT_LEFT]),
                "y": int(stats[i, cv2.CC_STAT_TOP]),
                "w": bw, "h": bh,
            })

    return _detect_gold_fallback(bgr, img_h, img_w, region_x, region_y, hsv_cfg, partial_banners, debug)


def _infer_slot_regions(
    cx_list: list[float],
    strip_region: dict,
    configured_regions: list[dict],
) -> list[dict]:
    """
    Derive highlight slot regions dynamically from the X centres of all text
    blobs detected in the strip image.

    Algorithm:
      1. Sort blob centre-X values (screen coordinates).
      2. Find large gaps (> gap_threshold px) between consecutive centres —
         each gap marks a boundary between card slots.
      3. Build one region per cluster: left edge = cluster min-X − margin,
         right edge = cluster max-X + margin, Y/H taken from configured_regions
         or the strip region itself.
      4. If clustering fails (too few blobs, or a very different count than
         expected) fall back to the caller-supplied configured_regions.

    The inferred regions replace the configured per-card regions only for
    highlight-box positioning during this scan cycle — nothing is persisted.
    """
    if not cx_list:
        return configured_regions

    sorted_cx = sorted(cx_list)
    # Gap threshold: treat spaces wider than 60 px (at 1920-wide screen) as
    # inter-card gaps.  Cards are typically 200–230 px wide with ~30 px gaps
    # between them, so 60 px cleanly separates cards without splitting words.
    gap_threshold = 60.0

    clusters: list[list[float]] = [[sorted_cx[0]]]
    for cx in sorted_cx[1:]:
        if cx - clusters[-1][-1] > gap_threshold:
            clusters.append([])
        clusters[-1].append(cx)

    # Sanity: need at least 1 cluster and no more than 8 slots
    if not clusters or len(clusters) > 8:
        return configured_regions

    # Derive Y and H from the first configured region when available,
    # otherwise use the strip region itself.
    ref = configured_regions[0] if configured_regions else strip_region
    slot_y = ref["y"]
    slot_h = ref["h"]
    margin = 20  # px to pad each side of the cluster bounding box

    inferred: list[dict] = []
    for cluster in clusters:
        left = int(min(cluster)) - margin
        right = int(max(cluster)) + margin
        inferred.append({
            "x": max(0, left),
            "y": slot_y,
            "w": max(1, right - left),
            "h": slot_h,
        })

    log.debug(
        "[STRIP] Inferred %d slot region(s) from %d blobs (configured: %d)",
        len(inferred), len(cx_list), len(configured_regions),
    )
    return inferred


def _ocr_strip(
    strip_img: np.ndarray,
    strip_x: int,
    strip_region: dict,
    ocr_regions: list[dict],
    talents_lower_clean: list[str],
    talents: list[str],
    threshold: int,
    debug: bool = False,
) -> tuple[list[str], list[tuple[int, str]]]:
    """
    Single-image OCR across the full card-name banner strip.

    Slot regions are *inferred* from the X positions of detected text blobs
    each scan cycle via _infer_slot_regions.  This means the correct number
    of highlight boxes is generated automatically whether 5 or 6 (or any
    other count) cards are currently showing — no manual reconfiguration
    needed when the game changes card count and repositions them.

    Falls back to the saved ocr_regions when clustering produces no result.
    """
    # ------------------------------------------------------------------
    # Step 1: gather (screen_x, text) pairs from OCR
    # ------------------------------------------------------------------
    raw_words: list[tuple[float, str]] = []   # (screen_x, text)

    # Preprocess the strip: 2× upscale using the original color image.
    # Preserving color (white title text on tan/parchment banner) gives
    # RapidOCR better text/background contrast than grayscale conversion.
    # CLAHE is intentionally NOT used — it enhances background texture
    # as much as text strokes on heavily decorated card banners.
    # 3× upscale + unsharp mask for better RapidOCR accuracy on the
    # ornate Deepwoken card-name serif font.
    _strip_ocr = cv2.resize(strip_img[:, :, :3], (0, 0), fx=3, fy=3,
                            interpolation=cv2.INTER_CUBIC)
    _blur = cv2.GaussianBlur(_strip_ocr, (0, 0), 1.5)
    _strip_ocr = cv2.addWeighted(_strip_ocr, 1.5, _blur, -0.5, 0)  # unsharp mask
    _scale_x = 3  # box coordinates returned by OCR are in 3× space

    rapid = _get_rapid_ocr()
    if rapid is not None:
        if debug:
            _save_debug(_strip_ocr, "strip_preprocessed")
        result, _ = rapid(_strip_ocr)
        _rejected: list[tuple[float, str]] = []
        if result:
            for item in result:
                box, text, conf = item[0], item[1], float(item[2])
                if not text.strip():
                    continue
                if conf < 0.05:
                    _rejected.append((conf, text))
                    continue
                # Scale box coordinates back from 3× image space to screen space
                word_cx = (box[0][0] + box[2][0]) / 2.0 / _scale_x
                raw_words.append((strip_x + word_cx, text))
        # Always log a summary so failures are visible without debug_images=true
        log.info(
            "[STRIP] RapidOCR raw result: %d accepted %s | %d rejected %s",
            len(raw_words),
            [(round(cx - strip_x), t) for cx, t in raw_words],
            len(_rejected),
            [(round(c, 2), t) for c, t in _rejected],
        )
    elif _TESSERACT_AVAILABLE:
        _gray = cv2.cvtColor(strip_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        _gray3x = _upscale(_gray, 3)
        adaptive = cv2.adaptiveThreshold(
            _gray3x, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
            blockSize=51, C=10,
        )
        if debug:
            _save_debug(adaptive, "strip_adaptive")
        data = pytesseract.image_to_data(
            adaptive,
            config="--psm 11 --oem 3",
            output_type=pytesseract.Output.DICT,
        )
        _tess_scale = 3
        for i in range(len(data["text"])):
            conf_val = int(data["conf"][i])
            text = data["text"][i].strip()
            if conf_val < 20 or not text:
                continue
            word_cx_3x = data["left"][i] + data["width"][i] / 2.0
            raw_words.append((strip_x + word_cx_3x / _tess_scale, text))
        log.info(
            "[STRIP] Tesseract raw result: %d words %s",
            len(raw_words),
            [(round(cx - strip_x), t) for cx, t in raw_words],
        )

    # ------------------------------------------------------------------
    # Step 2: infer slot regions from blob X positions this cycle
    # ------------------------------------------------------------------
    active_regions = _infer_slot_regions(
        [cx for cx, _ in raw_words],
        strip_region,
        ocr_regions,
    )

    # Step 2.5: override with calibrated template when one exists for the
    # detected card count.  Prefer exact count; fall back to the nearest
    # configured count within ±1 to survive a single false-positive span
    # (e.g., detector finds 6 but only "5" template is configured).
    _detected_count = len(active_regions)
    if _detected_count > 0:
        _region_sets = load_config().get("ocr_region_sets", {})
        _template = _region_sets.get(str(_detected_count))
        if not (_template and len(_template) == _detected_count):
            # Nearest configured count within ±1
            _avail = sorted((int(k) for k in _region_sets if _region_sets.get(k)),
                            key=lambda c: abs(c - _detected_count))
            for _c in _avail:
                if abs(_c - _detected_count) <= 1:
                    _cand = _region_sets.get(str(_c))
                    if _cand:
                        _template = _cand
                        log.info("[STRIP] Count=%d → using %d-card template (±%d fallback)",
                                 _detected_count, _c, abs(_c - _detected_count))
                        break
        if _template:
            active_regions = _template
            log.debug("[STRIP] Template applied (%d regions)", len(_template))

    # ------------------------------------------------------------------
    # Step 3: assign each word to its inferred slot
    # ------------------------------------------------------------------
    slot_words: dict[int, list[str]] = {}
    for screen_x, text in raw_words:
        slot = -1
        for j, region in enumerate(active_regions):
            if region["x"] <= screen_x < region["x"] + region["w"]:
                slot = j
                break
        if slot < 0:
            slot = len(active_regions)  # overflow bucket
        slot_words.setdefault(slot, []).append(text)

    if debug:
        log.debug("[STRIP] active_regions=%d slot_words=%s", len(active_regions), slot_words)

    # ------------------------------------------------------------------
    # Step 4: fuzzy-match each slot's text against the build talent list
    # ------------------------------------------------------------------
    detected: list[str] = []
    slot_hits: list[tuple[int, str]] = []
    # Per-slot debug info — one dict per active region slot
    slot_debug: list[dict] = [
        {"slot": i, "raw": "", "match": None, "near_miss": None, "score": 0.0}
        for i in range(len(active_regions))
    ]

    for slot, words in slot_words.items():
        candidate = " ".join(words).lower()
        candidate = _JUNK_PREFIX.sub("", candidate)
        candidate = re.sub(r"[^a-z '\-]", "", candidate).strip()
        # Keep a debug entry for this slot (overflow slots appended on demand)
        if slot < len(slot_debug):
            _dbg = slot_debug[slot]
        else:
            _dbg = {"slot": slot, "raw": "", "match": None, "near_miss": None, "score": 0.0}
            slot_debug.append(_dbg)
        _dbg["raw"] = candidate

        if len(candidate) < 3:
            continue

        best_score = 0.0
        best_idx = -1
        for i, talent_clean in enumerate(talents_lower_clean):
            s = _score_talent_in_line(talent_clean, candidate)
            if s > best_score:
                best_score = s
                best_idx = i

        _dbg["score"] = best_score

        if best_idx >= 0 and best_score >= threshold:
            talent_name = talents[best_idx]
            _dbg["match"] = talent_name
            if talent_name not in detected:
                detected.append(talent_name)
            if slot < len(active_regions):
                hit = (slot, talent_name)
                if hit not in slot_hits:
                    slot_hits.append(hit)
            log.info(
                "[STRIP] Slot %d HIT: '%s'  (ocr=%r  score=%.1f)",
                slot, talent_name, candidate, best_score,
            )
        elif best_idx >= 0 and best_score > 40:
            _dbg["near_miss"] = talents[best_idx]
            log.info(
                "[STRIP] Slot %d NEAR-MISS: ocr=%r → '%s'  score=%.1f < thresh %d",
                slot, candidate, talents_lower_clean[best_idx], best_score, threshold,
            )

    return detected, slot_hits, active_regions, slot_debug


class TalentScanner(QThread):
    """
    Continuously scans card regions to detect talent cards.
    Starts PAUSED — call resume() (button or F6) to begin scanning.
    """

    # (detected_names, missing_names, slot_hits: list[tuple[int,str]], active_regions: list[dict])
    results_ready = pyqtSignal(list, list, list, list)
    # Emitted each scan cycle when debug_ocr_highlight=true in config.
    # Payload: {mode, strip_region, active_regions, slot_data}
    debug_ocr_ready = pyqtSignal(dict)

    def __init__(self, build_talents: list[str], parent=None):
        super().__init__(parent)
        self._talents = build_talents
        self._talents_lower = [t.lower() for t in build_talents]
        # Bracket-stripped versions for card OCR matching — the card banner
        # never shows "[HVY]" / "[LHT]" tags so they must be removed before
        # scoring to avoid penalising talents that carry them.
        self._talents_lower_clean = [
            _BRACKET_SUFFIX.sub("", t.lower()).strip() for t in build_talents
        ]
        self._running = False
        self._paused = True  # idle until user starts tracking

        # Frame-hash caches — skip Tesseract when the screen hasn't changed.
        # Strip mode uses one hash for the whole strip image.
        # Per-region mode uses one hash per slot.
        self._strip_hash: bytes | None = None
        self._strip_cached: tuple[list, list] | None = None  # (detected, slot_hits)
        self._parchment_rects: list[dict] | None = None  # last good parchment positions
        self._region_hashes: dict[int, bytes] = {}
        self._region_cached: dict[int, tuple[int, str] | None] = {}  # idx → hit

    def pause(self) -> None:
        self._paused = True
        # Invalidate caches so the next resume starts with a fresh scan.
        self._strip_hash = None
        self._strip_cached = None
        self._parchment_rects = None
        self._region_hashes.clear()
        self._region_cached.clear()
        log.info("TalentScanner paused")

    def resume(self) -> None:
        self._paused = False
        log.info("TalentScanner resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def run(self) -> None:
        self._running = True
        log.info("TalentScanner thread started (paused=%s)", self._paused)

        with mss.mss() as sct:
            while self._running:
                if self._paused:
                    self.msleep(200)
                    continue

                cfg = load_config()
                regions: list[dict] = cfg.get("ocr_regions", [])
                threshold: int = cfg.get("fuzzy_threshold", 72)
                interval: int = cfg.get("ocr_interval_ms", 1000)
                debug_img: bool = cfg.get("debug_images", False)

                if not regions:
                    log.warning("[OCR] No ocr_regions configured — nothing to scan. Open Settings and pick card regions.")

                detected: list[str] = []
                # (region_idx, talent_name) — at most one entry per card slot
                slot_hits: list[tuple[int, str]] = []

                # ----------------------------------------------------------------
                # Mode priority:
                #   1. card_detect_region — parchment banner detection (automatic)
                #   2. name_strip_region  — whole-strip RapidOCR + word clustering
                #   3. per-region fallback
                # ----------------------------------------------------------------
                card_detect_region = cfg.get("card_detect_region")
                use_detect = bool(
                    card_detect_region
                    and card_detect_region.get("w", 0) > 0
                    and card_detect_region.get("h", 0) > 0
                )

                strip_region = cfg.get("name_strip_region")
                use_strip = bool(
                    not use_detect
                    and strip_region
                    and strip_region.get("w", 0) > 0
                    and strip_region.get("h", 0) > 0
                    and regions
                )

                active_regions: list[dict] | None = None
                _slot_debug: list[dict] = []

                if use_detect:
                    # --------------------------------------------------------
                    # Card-detect mode: grab card area → detect title banners
                    # via parchment color → OCR each banner directly.
                    # No templates needed — detection gives exact positions.
                    # --------------------------------------------------------
                    try:
                        detect_img = _capture_region(sct, card_detect_region)
                        if debug_img:
                            _save_debug(detect_img, "card_detect_raw")
                        frame_hash = _hash_frame(detect_img)
                        if frame_hash == self._strip_hash and self._strip_cached is not None:
                            detected, slot_hits, active_regions, _slot_debug = self._strip_cached
                            log.debug("[CARDDET] Frame unchanged — reusing cached results")
                        else:
                            self._strip_hash = frame_hash
                            card_rects, is_primary = detect_cards(
                                detect_img,
                                card_detect_region["x"],
                                card_detect_region["y"],
                                cfg.get("card_detect_hsv"),
                                debug_img,
                            )
                            if is_primary and card_rects:
                                # Parchment succeeded — cache these positions.
                                self._parchment_rects = list(card_rects)
                            elif not is_primary and self._parchment_rects:
                                # Parchment failed (likely overlay interference)
                                # but we have cached positions — reuse them.
                                card_rects = self._parchment_rects
                                log.info(
                                    "[CARDDET] Parchment failed, reusing %d cached positions",
                                    len(card_rects),
                                )
                            if not card_rects:
                                if self._strip_cached is not None:
                                    detected, slot_hits, active_regions, _slot_debug = self._strip_cached
                                    log.debug("[CARDDET] No cards found — holding cached results")
                            else:
                                active_regions = card_rects
                                _slot_debug = []
                                for _si, _sr in enumerate(card_rects):
                                    _timg = _capture_region(sct, _sr)
                                    if debug_img:
                                        _save_debug(_timg, f"card_{_si}_title")
                                    _raw, _cands = _ocr_card_title(_timg, debug_img, _si)
                                    log.info("[CARDDET] Slot %d  raw=%r  cands=%s", _si, _raw, _cands)
                                    _dbg: dict = {
                                        "slot": _si, "raw": _raw,
                                        "match": None, "near_miss": None, "score": 0.0,
                                    }
                                    _slot_debug.append(_dbg)
                                    if not _cands:
                                        continue
                                    cand = " ".join(_cands).lower()
                                    cand = _JUNK_PREFIX.sub("", cand)
                                    cand = re.sub(r"[^a-z '\-]", "", cand).strip()
                                    _dbg["raw"] = cand
                                    if len(cand) < 3:
                                        continue
                                    _bscore, _bidx = 0.0, -1
                                    for _ti, _tc in enumerate(self._talents_lower_clean):
                                        _s = _score_talent_in_line(_tc, cand)
                                        if _s > _bscore:
                                            _bscore, _bidx = _s, _ti
                                    _dbg["score"] = _bscore
                                    if _bidx >= 0 and _bscore >= threshold:
                                        _tn = self._talents[_bidx]
                                        _dbg["match"] = _tn
                                        if _tn not in detected:
                                            detected.append(_tn)
                                        slot_hits.append((_si, _tn))
                                        log.info("[CARDDET] Slot %d HIT: '%s'  score=%.1f",
                                                 _si, _tn, _bscore)
                                    elif _bidx >= 0 and _bscore > 40:
                                        _dbg["near_miss"] = self._talents[_bidx]
                                        log.info(
                                            "[CARDDET] Slot %d NEAR-MISS: ocr=%r → '%s'  score=%.1f",
                                            _si, cand, self._talents_lower_clean[_bidx], _bscore,
                                        )

                                log.info(
                                    "[CARDDET] %d card(s) detected → %d hit(s): %s",
                                    len(card_rects), len(slot_hits),
                                    [t for _, t in slot_hits],
                                )
                                self._strip_cached = (
                                    list(detected), list(slot_hits),
                                    list(active_regions), list(_slot_debug),
                                )
                    except Exception as exc:
                        log.warning("[CARDDET] Error: %s", exc, exc_info=True)

                elif use_strip:
                    try:
                        strip_img = _capture_region(sct, strip_region)
                        if debug_img:
                            _save_debug(strip_img, "strip_raw")
                        frame_hash = _hash_frame(strip_img)
                        if frame_hash == self._strip_hash and self._strip_cached is not None:
                            detected, slot_hits, active_regions, _slot_debug = self._strip_cached
                            log.debug("[STRIP] Frame unchanged — reusing cached results")
                        else:
                            self._strip_hash = frame_hash
                            detected, slot_hits, active_regions, _slot_debug = _ocr_strip(
                                strip_img,
                                strip_region["x"],
                                strip_region,
                                regions,
                                self._talents_lower_clean,
                                self._talents,
                                threshold,
                                debug_img,
                            )
                            self._strip_cached = (list(detected), list(slot_hits), list(active_regions), list(_slot_debug))
                    except Exception as exc:
                        log.warning("Strip OCR error: %s", exc)

                else:
                    # --------------------------------------------------------
                    # Per-region mode — run Tesseract once per configured slot.
                    # Frame-hash cache: if the slot image is identical to the
                    # previous scan, reuse the previous match without calling
                    # Tesseract again.  This is the biggest win when the card
                    # selection screen is static between scan intervals.
                    # --------------------------------------------------------
                    _region_debug_entries: list[dict] = [
                        {"slot": i, "raw": "", "match": None, "near_miss": None, "score": 0.0}
                        for i in range(len(regions))
                    ]
                    for region_idx, region in enumerate(regions):
                        try:
                            img = _capture_region(sct, region)

                            # --- Hash-cache check ---
                            frame_hash = _hash_frame(img)
                            if frame_hash == self._region_hashes.get(region_idx):
                                cached = self._region_cached.get(region_idx)
                                if cached is not None:
                                    name = cached[1]
                                    if name not in detected:
                                        detected.append(name)
                                    slot_hits.append(cached)
                                    _region_debug_entries[region_idx] = {
                                        "slot": region_idx, "raw": "(cached)",
                                        "match": name, "near_miss": None, "score": 100.0,
                                    }
                                continue  # skip Tesseract for this slot
                            self._region_hashes[region_idx] = frame_hash

                            h_px = img.shape[0]
                            raw_text, candidates = "", []

                            if h_px > 80:
                                for pct in (0.20, 0.35):
                                    crop_h = max(55, int(h_px * pct))
                                    crop_img = img[:crop_h, :, :]
                                    if debug_img:
                                        _save_debug(crop_img, f"card_raw_r{region_idx}_p{int(pct*100)}")
                                    raw_text, candidates = _ocr_card_title(
                                        crop_img, debug=debug_img, region_idx=region_idx
                                    )
                                    if candidates:
                                        log.info(
                                            "[OCR] Region %d crop=%dpx raw=%r candidates=%s",
                                            region_idx, crop_h, raw_text, candidates,
                                        )
                                        break
                                if not candidates:
                                    log.info(
                                        "[OCR] Region %d (tall %dpx) | raw=%r | no candidates",
                                        region_idx, h_px, raw_text,
                                    )
                            else:
                                if debug_img:
                                    _save_debug(img, f"card_raw_r{region_idx}")
                                raw_text, candidates = _ocr_card_title(
                                    img, debug=debug_img, region_idx=region_idx
                                )
                                log.info(
                                    "[OCR] Region %d %s | raw=%r | candidates=%s",
                                    region_idx, region, raw_text, candidates,
                                )

                            slot_match: tuple[int, str] | None = None
                            for candidate in candidates:
                                best_score = 0.0
                                best_idx = -1
                                for i, talent_clean in enumerate(self._talents_lower_clean):
                                    s = _score_talent_in_line(talent_clean, candidate)
                                    if s > best_score:
                                        best_score = s
                                        best_idx = i

                                if best_idx >= 0 and best_score >= threshold:
                                    talent_name = self._talents[best_idx]
                                    if talent_name not in detected:
                                        detected.append(talent_name)
                                        log.info(
                                            "[OCR] Region %d HIT: '%s' (ocr='%s' score=%.1f)",
                                            region_idx, talent_name, candidate, best_score,
                                        )
                                    if slot_match is None:
                                        slot_match = (region_idx, talent_name)
                                elif best_idx >= 0 and best_score > 40:
                                    log.info(
                                        "[OCR] Region %d NEAR-MISS: ocr='%s' → best='%s'"
                                        " score=%.1f < threshold %d",
                                        region_idx, candidate,
                                        self._talents_lower_clean[best_idx],
                                        best_score, threshold,
                                    )

                            # Update per-region cache
                            self._region_cached[region_idx] = slot_match
                            if slot_match:
                                slot_hits.append(slot_match)

                            # Collect debug entry for this slot
                            best_cand = candidates[0] if candidates else (raw_text[:60] if raw_text else "")
                            _region_debug_entries[region_idx] = {
                                "slot": region_idx,
                                "raw": best_cand,
                                "match": slot_match[1] if slot_match else None,
                                "near_miss": None,
                                "score": 0.0,
                            }

                        except Exception as exc:
                            log.warning("OCR error in region %s: %s", region, exc)

                # Collect region-mode debug entries into _slot_debug
                if not use_strip and not use_detect:
                    _slot_debug = _region_debug_entries

                # "missing" = build talents the player does not own yet.
                cfg_owned = set(cfg.get("known_owned_talents", []))
                missing = [t for t in self._talents if t not in cfg_owned]
                self.results_ready.emit(detected, missing, slot_hits, active_regions or [])

                if cfg.get("debug_ocr_highlight", False):
                    _mode = "detect" if use_detect else ("strip" if use_strip else "region")
                    _search_r = (
                        card_detect_region if use_detect
                        else (strip_region if use_strip else None)
                    )
                    self.debug_ocr_ready.emit({
                        "mode": _mode,
                        "strip_region": _search_r,
                        "active_regions": list(active_regions) if active_regions else regions,
                        "slot_data": list(_slot_debug),
                    })

                self.msleep(interval)

        log.info("TalentScanner stopped")

    def stop(self) -> None:
        self._running = False
        self.wait()


class ScanOwnedWorker(QThread):
    """
    One-shot OCR of the in-game owned-talents panel (right side of screen).
    Emits the list of build talents found visible on that panel.

    TODO: When the in-game character export button is fixed, replace this
          one-shot scan with a proper import from the exported build data.
          Track: https://deepwoken.co — export feature restoration.
    """

    scan_done = pyqtSignal(list)  # list[str] of matched talent names

    def __init__(self, build_talents: list[str], parent=None):
        super().__init__(parent)
        self._talents = build_talents
        self._talents_lower = [t.lower() for t in build_talents]
        # Clean version used for panel OCR matching: remove bracket tags
        # so e.g. "Wyvern's Claw [HVY]" becomes "wyvern's claw" and matches
        # the OCR output which never contains square brackets.
        self._talents_lower_clean = [
            _BRACKET_SUFFIX.sub("", t.lower()).strip() for t in build_talents
        ]

    def run(self) -> None:
        cfg = load_config()
        region: dict = cfg.get(
            "talents_panel_region", {"x": 1060, "y": 100, "w": 300, "h": 650}
        )
        # Per-window threshold for the sliding-window multi-talent extractor.
        # Higher than the old whole-line threshold because windows are shorter
        # and the token_sort_ratio scorer is more precise.
        threshold: int = cfg.get("owned_fuzzy_threshold", 75)
        debug: bool = cfg.get("debug_images", False)

        log.info("One-shot owned-talents scan — region=%s threshold=%d", region, threshold)
        matched: list[str] = []

        try:
            with mss.mss() as sct:
                img = _capture_region(sct, region)

                # ── Capture diagnostics (always logged) ──────────────────────────
                _h, _w = img.shape[:2]
                _mean_brightness = float(np.mean(img[:, :, :3]))
                log.info(
                    "Owned panel capture: %dx%d px  mean_brightness=%.1f",
                    _w, _h, _mean_brightness,
                )
                if _mean_brightness < 10:
                    log.warning(
                        "Owned panel image is nearly black (mean=%.1f) — "
                        "the region may be off-screen, minimised, or pointing at a black/empty area. "
                        "Use ⚙ Settings → 'Pick Owned Talents Panel' to recalibrate.",
                        _mean_brightness,
                    )
                    _save_debug(img, "owned_panel_raw")

                if debug:
                    # Save the raw colour capture so the region boundary is easy
                    # to inspect alongside the binarized result.
                    _save_debug(img, "owned_panel_raw")
                rapid = _get_rapid_ocr()
                candidates: list[str] = []
                _use_tesseract = True

                if rapid is not None:
                    # RapidOCR handles multi-colour talent text (red/blue-green/
                    # white) better than Tesseract's grayscale pipeline.
                    try:
                        result, _ = rapid(img[:, :, :3])
                        _use_tesseract = False
                        if result:
                            for item in result:
                                if float(item[2]) >= 0.3:
                                    for cleaned in _clean_lines(item[1], skip_headers=True):
                                        if cleaned not in candidates:
                                            candidates.append(cleaned)
                    except Exception as rapid_exc:
                        log.warning(
                            "RapidOCR failed on owned panel (%s) — falling back to Tesseract",
                            rapid_exc,
                        )
                        _use_tesseract = True

                if _use_tesseract and _TESSERACT_AVAILABLE:
                    try:
                        processed = _preprocess_panel(img)
                        if debug:
                            _save_debug(processed, "owned_panel_bin")
                        raw_text = _extract_text(processed, TESS_CONFIG_PANEL)
                        candidates = _clean_lines(raw_text, skip_headers=True)

                        # Run a second pass with PSM 11 (sparse text) and merge unique
                        # candidate lines.  PSM 6 (uniform block) reads every row but
                        # can miss isolated text blocks; PSM 11 finds scattered words but
                        # is noisier.  Both modes together give near-complete coverage.
                        raw_text_11 = pytesseract.image_to_string(
                            processed, config=TESS_CONFIG_PANEL.replace("--psm 6", "--psm 11")
                        ).strip()
                        for extra in _clean_lines(raw_text_11, skip_headers=True):
                            if extra not in candidates:
                                candidates.append(extra)
                    except Exception as tess_exc:
                        log.error(
                            "Owned panel scan failed: %s\n"
                            "  → RapidOCR was %s. "
                            "Install rapidocr-onnxruntime (pip install rapidocr-onnxruntime) "
                            "or install Tesseract-OCR from https://github.com/UB-Mannheim/tesseract/wiki",
                            tess_exc,
                            "unavailable" if rapid is None else "available but failed",
                        )
                elif _use_tesseract and not _TESSERACT_AVAILABLE:
                    log.warning(
                        "Owned panel scan: RapidOCR unavailable and Tesseract not installed. "
                        "Install rapidocr-onnxruntime: pip install rapidocr-onnxruntime"
                    )

                log.info("Owned scan raw candidates (%d): %s", len(candidates), candidates)

                # ── 0-candidate diagnostic ───────────────────────────────────────
                if not candidates:
                    log.warning(
                        "Owned panel OCR returned 0 text candidates. Possible causes:\n"
                        "  • Region is off-screen or pointing at wrong area\n"
                        "  • Game not visible / talent panel not open\n"
                        "  • OCR engine failed (see errors above)\n"
                        "  → Raw screenshot saved to debug/owned_panel_raw_*.png\n"
                        "  → Enable 'debug_images' in Settings to also save the binarised result."
                    )
                    _save_debug(img, "owned_panel_raw")

                # Bottom-up: for each build talent, find the candidate line
                # that best contains that talent using a sliding n-gram window.
                #
                # _score_talent_in_line uses fuzz.ratio over fixed-width windows
                # (k-1, k, k+1 words) so:
                #   - exact matches score 100 %
                #   - merged OCR words ("speeddemon") score ~95 % via k-1 window
                #   - one noise prefix/suffix token is absorbed by k+1 window
                #   - single shared token ("dark" in wrong line) stays below ~65 %
                _score_table: list[tuple[str, float, str]] = []  # (talent, best_score, best_cand)
                for talent_name, talent_clean in zip(self._talents, self._talents_lower_clean):
                    best_score = 0.0
                    best_line_idx = -1
                    for line_idx, candidate in enumerate(candidates):
                        score = _score_talent_in_line(talent_clean, candidate)
                        if score > best_score:
                            best_score = score
                            best_line_idx = line_idx
                    best_cand = candidates[best_line_idx] if best_line_idx >= 0 else ""
                    _score_table.append((talent_name, best_score, best_cand))
                    if best_score >= threshold and talent_name not in matched:
                        matched.append(talent_name)
                        log.info(
                            "Owned talent found: '%s' (in '%s', score=%.1f)",
                            talent_name, best_cand, best_score,
                        )

                # ── 0-match-but-candidates diagnostic ───────────────────────────
                if not matched and candidates:
                    log.warning(
                        "Owned panel: %d OCR line(s) found but none matched any build "
                        "talent (threshold=%d). Closest scores (top 10):",
                        len(candidates), threshold,
                    )
                    near_misses = sorted(_score_table, key=lambda t: t[1], reverse=True)
                    for t_name, t_score, t_cand in near_misses[:10]:
                        log.warning(
                            "  %-35s → best score %.1f  (OCR line: '%s')",
                            f"'{t_name}'", t_score, t_cand,
                        )
        except Exception as exc:
            log.error("Owned panel scan failed: %s", exc)

        log.info("Owned scan complete — %d/%d build talents matched", len(matched), len(self._talents))
        self.scan_done.emit(matched)
