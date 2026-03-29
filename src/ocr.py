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
import pytesseract
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

for _candidate in _TESSERACT_CANDIDATES:
    if _candidate and os.path.isfile(_candidate):
        pytesseract.pytesseract.tesseract_cmd = _candidate
        log.info("Tesseract found at: %s", _candidate)
        break

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
    # --- RapidOCR path (no subprocess, ~3-5× faster) ---
    rapid = _get_rapid_ocr()
    if rapid is not None:
        result, _ = rapid(img[:, :, :3])
        if result:
            raw = " ".join(item[1] for item in result if float(item[2]) >= 0.3)
            cands = _clean_lines(raw)
            if debug:
                log.debug("[RapidOCR] card r%d → %r", region_idx, cands)
            return raw, cands
        return "", []

    # --- Tesseract fallback ---
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
            # For win >= k (exact and noise-absorbing windows) every talent
            # token must approximately match some window token.  This rejects
            # windows where only a shared prefix drives the score.
            # k-1 windows skip this check — they intentionally have fewer
            # tokens (merged-word OCR artefact case).
            if win >= k and not _tokens_present(t_tokens, w_tokens):
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
    mon = {"top": region["y"], "left": region["x"], "width": region["w"], "height": region["h"]}
    return np.array(sct.grab(mon))


def _ocr_strip(
    strip_img: np.ndarray,
    strip_x: int,
    ocr_regions: list[dict],
    talents_lower_clean: list[str],
    talents: list[str],
    threshold: int,
    debug: bool = False,
) -> tuple[list[str], list[tuple[int, str]]]:
    """
    Single-image OCR across the full card-name banner strip.

    Instead of N separate Tesseract calls (one per card slot), this function:
      1. Upscales and binarises ONE wide image spanning all card banners.
      2. Calls pytesseract.image_to_data(PSM 11 — sparse text) ONCE.
      3. Maps each returned word back to its card slot via the per-card
         region x boundaries stored in ocr_regions.
      4. Fuzzy-matches each slot's word cluster against the build talent list.

    Speed benefit: 1 Tesseract subprocess instead of N (5–6×  faster).
    Accuracy benefit: PSM 11 is designed for scattered text on non-uniform
    backgrounds, so it handles the stylised card banners well.
    6-card support: naturally handles any number of configured slots.
    """
    # Group words by card slot using their horizontal centre position.
    slot_words: dict[int, list[str]] = {}
    rapid = _get_rapid_ocr()
    if rapid is not None:
        # --- RapidOCR path: one in-process ONNX call, no subprocess overhead ---
        result, _ = rapid(strip_img[:, :, :3])
        if result:
            for item in result:
                box, text, conf = item[0], item[1], float(item[2])
                if conf < 0.3 or not text.strip():
                    continue
                # box: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] in original image coords
                word_cx = (box[0][0] + box[2][0]) / 2.0
                screen_x = strip_x + word_cx
                slot = -1
                for j, region in enumerate(ocr_regions):
                    if region["x"] <= screen_x < region["x"] + region["w"]:
                        slot = j
                        break
                if slot >= 0:
                    slot_words.setdefault(slot, []).append(text)
        if debug:
            log.debug("[STRIP-RAPID] slot_words: %s", slot_words)
    else:
        # --- Tesseract fallback ---
        gray = cv2.cvtColor(strip_img, cv2.COLOR_BGRA2GRAY)
        scale = 3
        gray3x = _upscale(gray, scale)
        adaptive = cv2.adaptiveThreshold(
            gray3x, 255,
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
        n = len(data["text"])
        for i in range(n):
            conf_val = int(data["conf"][i])
            text = data["text"][i].strip()
            if conf_val < 30 or not text:
                continue
            # Convert from 3x-upscaled image coords → screen x
            word_cx_3x = data["left"][i] + data["width"][i] / 2.0
            screen_x = strip_x + word_cx_3x / scale
            slot = -1
            for j, region in enumerate(ocr_regions):
                if region["x"] <= screen_x < region["x"] + region["w"]:
                    slot = j
                    break
            if slot >= 0:
                slot_words.setdefault(slot, []).append(text)
        if debug:
            log.debug("[STRIP] slot_words: %s", slot_words)

    detected: list[str] = []
    slot_hits: list[tuple[int, str]] = []

    for slot, words in slot_words.items():
        candidate = " ".join(words).lower()
        candidate = _JUNK_PREFIX.sub("", candidate)
        candidate = re.sub(r"[^a-z '\-]", "", candidate).strip()
        if len(candidate) < 3:
            continue

        best_score = 0.0
        best_idx = -1
        for i, talent_clean in enumerate(talents_lower_clean):
            s = _score_talent_in_line(talent_clean, candidate)
            if s > best_score:
                best_score = s
                best_idx = i

        if best_idx >= 0 and best_score >= threshold:
            talent_name = talents[best_idx]
            if talent_name not in detected:
                detected.append(talent_name)
            hit = (slot, talent_name)
            if hit not in slot_hits:
                slot_hits.append(hit)
            log.info(
                "[STRIP] Slot %d HIT: '%s'  (ocr=%r  score=%.1f)",
                slot, talent_name, candidate, best_score,
            )
        elif best_idx >= 0 and best_score > 40:
            log.debug(
                "[STRIP] Slot %d NEAR-MISS: ocr=%r → '%s'  score=%.1f < thresh %d",
                slot, candidate, talents_lower_clean[best_idx], best_score, threshold,
            )

    return detected, slot_hits


class TalentScanner(QThread):
    """
    Continuously scans card regions to detect talent cards.
    Starts PAUSED — call resume() (button or F6) to begin scanning.
    """

    # (detected_names, missing_names, slot_hits: list[tuple[int, str]])
    results_ready = pyqtSignal(list, list, list)

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
        self._region_hashes: dict[int, bytes] = {}
        self._region_cached: dict[int, tuple[int, str] | None] = {}  # idx → hit

    def pause(self) -> None:
        self._paused = True
        # Invalidate caches so the next resume starts with a fresh scan.
        self._strip_hash = None
        self._strip_cached = None
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
                # Strip mode: one grab + one Tesseract call for ALL slots at once.
                # Activated when name_strip_region is configured (w > 0, h > 0).
                # ----------------------------------------------------------------
                strip_region = cfg.get("name_strip_region")
                use_strip = bool(
                    strip_region
                    and strip_region.get("w", 0) > 0
                    and strip_region.get("h", 0) > 0
                    and regions
                )

                if use_strip:
                    try:
                        strip_img = _capture_region(sct, strip_region)
                        if debug_img:
                            _save_debug(strip_img, "strip_raw")
                        frame_hash = _hash_frame(strip_img)
                        if frame_hash == self._strip_hash and self._strip_cached is not None:
                            detected, slot_hits = self._strip_cached
                            log.debug("[STRIP] Frame unchanged — reusing cached results")
                        else:
                            self._strip_hash = frame_hash
                            detected, slot_hits = _ocr_strip(
                                strip_img,
                                strip_region["x"],
                                regions,
                                self._talents_lower_clean,
                                self._talents,
                                threshold,
                                debug_img,
                            )
                            self._strip_cached = (list(detected), list(slot_hits))
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

                        except Exception as exc:
                            log.warning("OCR error in region %s: %s", region, exc)

                # "missing" = build talents the player does not own yet.
                # Using known_owned_talents from config (persisted by the overlay)
                # instead of "not in detected" — a detected card is one that
                # is visible on screen and should be highlighted, not skipped.
                cfg_owned = set(cfg.get("known_owned_talents", []))
                missing = [t for t in self._talents if t not in cfg_owned]
                self.results_ready.emit(detected, missing, slot_hits)
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
                if debug:
                    # Save the raw colour capture so the region boundary is easy
                    # to inspect alongside the binarized result.
                    _save_debug(img, "owned_panel_raw")
                rapid = _get_rapid_ocr()
                if rapid is not None:
                    # RapidOCR handles multi-colour talent text (red/blue-green/
                    # white) better than Tesseract's grayscale pipeline.
                    result, _ = rapid(img[:, :, :3])
                    candidates: list[str] = []
                    if result:
                        for item in result:
                            if float(item[2]) >= 0.3:
                                for cleaned in _clean_lines(item[1], skip_headers=True):
                                    if cleaned not in candidates:
                                        candidates.append(cleaned)
                else:
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

                log.info("Owned scan raw candidates (%d): %s", len(candidates), candidates)

                # Bottom-up: for each build talent, find the candidate line
                # that best contains that talent using a sliding n-gram window.
                #
                # _score_talent_in_line uses fuzz.ratio over fixed-width windows
                # (k-1, k, k+1 words) so:
                #   - exact matches score 100 %
                #   - merged OCR words ("speeddemon") score ~95 % via k-1 window
                #   - one noise prefix/suffix token is absorbed by k+1 window
                #   - single shared token ("dark" in wrong line) stays below ~65 %
                for talent_name, talent_clean in zip(self._talents, self._talents_lower_clean):
                    best_score = 0.0
                    best_line_idx = -1
                    for line_idx, candidate in enumerate(candidates):
                        score = _score_talent_in_line(talent_clean, candidate)
                        if score > best_score:
                            best_score = score
                            best_line_idx = line_idx
                    if best_score >= threshold and talent_name not in matched:
                        matched.append(talent_name)
                        log.info(
                            "Owned talent found: '%s' (in '%s', score=%.1f)",
                            talent_name, candidates[best_line_idx], best_score,
                        )
        except Exception as exc:
            log.error("Owned panel scan failed: %s", exc)

        log.info("Owned scan complete — %d/%d build talents matched", len(matched), len(self._talents))
        self.scan_done.emit(matched)
