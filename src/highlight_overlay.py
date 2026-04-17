"""
highlight_overlay.py — Transparent fullscreen window that draws white pick-me
boxes over talent card slots currently showing build talents the player still
needs.  Mouse and keyboard input passes straight through to the game.

When the player left-clicks on a highlighted card, a global Win32
WH_MOUSE_LL hook detects the click and emits the `talent_picked` signal so
the talent can be marked as owned without any extra button press.
"""

import ctypes
import ctypes.wintypes
import logging

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QPainter, QPen, QFont
from PyQt5.QtWidgets import QApplication, QWidget

from utils import load_config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 extended-style constants for true click-through
# ---------------------------------------------------------------------------
_GWL_EXSTYLE       = -20
_WS_EX_LAYERED     = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE  = 0x08000000

_SWP_FLAGS = 0x0013   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE

# SetWindowDisplayAffinity flag — excludes the window from screen capture
# (mss, PrintWindow, BitBlt).  Available since Windows 10 v2004 (build 19041).
_WDA_EXCLUDEFROMCAPTURE = 0x00000011

# ---------------------------------------------------------------------------
# Win32 low-level mouse hook infrastructure
# ---------------------------------------------------------------------------
_WH_MOUSE_LL  = 14
_WM_LBUTTONUP = 0x0202


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          ctypes.wintypes.POINT),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("flags",       ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


# On 64-bit Windows WPARAM/LPARAM/LRESULT are pointer-wide (8 bytes).
# ctypes.wintypes.WPARAM/LPARAM are c_ulong/c_long which are *32-bit* on
# Windows regardless of process bitness — using them here causes the
# "int too long to convert" OverflowError on every mouse event.
# c_ssize_t and c_size_t are always pointer-wide and are the correct types.
_HOOKPROC_TYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,                          # LRESULT return
    ctypes.c_int,                              # nCode
    ctypes.c_size_t,                           # WPARAM
    ctypes.c_ssize_t,                          # LPARAM
)

# Pre-declare argtypes/restype on CallNextHookEx for the same reason.
_CallNextHookEx = ctypes.windll.user32.CallNextHookEx
_CallNextHookEx.restype  = ctypes.c_ssize_t
_CallNextHookEx.argtypes = [
    ctypes.c_void_p,   # hhk
    ctypes.c_int,      # nCode
    ctypes.c_size_t,   # wParam
    ctypes.c_ssize_t,  # lParam
]

# Module-level hook state — only one CardHighlightOverlay is active at a time.
_g_mouse_hook_handle = None
_g_active_highlight = None   # set to the live CardHighlightOverlay instance


def _mouse_hook_callback(nCode: int, wParam: int, lParam: int) -> int:
    """Low-level mouse proc — called on the Qt main thread via the message loop."""
    if nCode >= 0 and wParam == _WM_LBUTTONUP and _g_active_highlight is not None:
        data = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
        _g_active_highlight._on_mouse_click(data.pt.x, data.pt.y)
    return _CallNextHookEx(_g_mouse_hook_handle, nCode, wParam, lParam)


# Keep the HOOKPROC object alive for the lifetime of the module — must not be
# GC'd while the hook is installed, otherwise Windows calls into freed memory.
_MOUSE_HOOK_PROC = _HOOKPROC_TYPE(_mouse_hook_callback)


def _get_exstyle(hwnd: int) -> int:
    try:
        return ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    except Exception:
        return 0


def _apply_click_through(hwnd: int) -> None:
    """Force WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE at the Win32 level."""
    try:
        before = _get_exstyle(hwnd)
        desired = before | _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
        ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, desired)
        after = _get_exstyle(hwnd)
        has_transparent = bool(after & _WS_EX_TRANSPARENT)
        has_noactivate  = bool(after & _WS_EX_NOACTIVATE)
        if has_transparent and has_noactivate:
            log.debug("[Z] click-through OK hwnd=%s exstyle=0x%08x", hwnd, after)
        else:
            log.warning(
                "[Z] click-through INCOMPLETE hwnd=%s before=0x%08x after=0x%08x "
                "transparent=%s noactivate=%s",
                hwnd, before, after, has_transparent, has_noactivate,
            )
    except Exception as exc:
        log.warning("[Z] click-through EXCEPTION hwnd=%s: %s", hwnd, exc)


def _enforce_z_below(card_hwnd: int, overlay_hwnd: int) -> None:
    """
    Place card_hwnd IMMEDIATELY below overlay_hwnd in Win32 Z-order.
    Both windows remain TOPMOST; this just ensures card_highlight never
    occludes OverlayWindow, surviving Qt repaints.
    """
    try:
        ret = ctypes.windll.user32.SetWindowPos(
            card_hwnd, overlay_hwnd, 0, 0, 0, 0, _SWP_FLAGS
        )
        if not ret:
            err = ctypes.windll.kernel32.GetLastError()
            log.warning("[Z] SetWindowPos FAILED card=%s below=%s err=%d", card_hwnd, overlay_hwnd, err)
        else:
            log.debug("[Z] SetWindowPos OK card=%s below=%s", card_hwnd, overlay_hwnd)
    except Exception as exc:
        log.warning("[Z] SetWindowPos EXCEPTION: %s", exc)


def _get_window_z_rank(hwnd: int) -> int:
    """
    Walk the topmost Z-order chain and return how many windows are ABOVE hwnd.
    0 = topmost window.  Returns -1 if hwnd not found in the TOPMOST chain.
    """
    GW_HWNDNEXT = 2
    HWND_TOPMOST = -1  # not actually used here, but for reference
    found_at = -1
    try:
        # Get the first TOPMOST window
        cur = ctypes.windll.user32.GetTopWindow(None)
        rank = 0
        while cur:
            if cur == hwnd:
                found_at = rank
                break
            # Check if this window is TOPMOST
            style = ctypes.windll.user32.GetWindowLongW(cur, _GWL_EXSTYLE)
            if not (style & 0x00000008):  # WS_EX_TOPMOST
                break   # left the TOPMOST zone
            cur = ctypes.windll.user32.GetWindow(cur, GW_HWNDNEXT)
            rank += 1
    except Exception:
        pass
    return found_at


class CardHighlightOverlay(QWidget):
    """
    Fullscreen transparent overlay.

    Connected to TalentScanner.results_ready(detected, missing, slot_hits).
    Draws a white box around every card slot where:
      - a talent was detected by OCR  AND
      - that talent is still MISSING from the player's owned set.

    Additionally installs a Win32 WH_MOUSE_LL global mouse hook.  When the
    player left-clicks on a highlighted card, talent_picked(str) is emitted
    so the overlay window can immediately mark the talent as owned.

    The box expands above/below the narrow OCR title-strip so it covers the
    full card.  Expansion is driven by config keys:
      card_highlight_above  (default 30 px)
      card_highlight_below  (default 185 px)
    """

    # Emitted when the player clicks on a highlighted (missing) card.
    talent_picked = pyqtSignal(str)

    def __init__(self, overlay_hwnd: int = 0):
        super().__init__()
        self._slot_hits: list[tuple[int, str]] = []   # (card_slot_idx, talent_name)
        self._missing: set[str] = set()
        self._regions: list[dict] = []
        self._overlay_hwnd: int = overlay_hwnd
        self._highlight_above: int = 30
        self._highlight_below: int = 185
        self._debug_data: dict | None = None   # set by update_debug_ocr when debug mode is on

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._fit_to_screen()
        self.show()
        _apply_click_through(int(self.winId()))
        self._apply_exclude_from_capture()
        if self._overlay_hwnd:
            _enforce_z_below(int(self.winId()), self._overlay_hwnd)

        # Continuously re-enforce Z-order every 300 ms.
        # This is the robust fix: even if Qt, Windows, or the game disrupts the
        # Z-order after a hide/show cycle or a WM_ACTIVATEAPP event, it is
        # corrected within one timer tick without relying on the scanner running.
        self._z_timer = QTimer(self)
        self._z_timer.timeout.connect(self.reapply_z_order)
        self._z_timer.start(300)

        # Diagnostic timer — runs every 2 s when debug_zorder=true.
        # Warns in the log if card_highlight is detected ABOVE OverlayWindow.
        self._diag_timer = QTimer(self)
        self._diag_timer.timeout.connect(self._check_z_order)
        self._diag_timer.start(2000)

        # Install the global low-level mouse hook.
        # Must be done after the window is shown so we are already on the
        # Qt main thread (which drives the Windows message loop).
        self._install_mouse_hook()

    def _fit_to_screen(self) -> None:
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

    def _apply_exclude_from_capture(self) -> None:
        """Make this overlay invisible to screen-capture APIs (mss, BitBlt).

        This prevents the highlight boxes, PICK badges, and debug text from
        being captured in the screenshots that the OCR scanner grabs.
        Without this, the overlay's own painted elements corrupt the next
        OCR cycle and break parchment banner detection.
        """
        hwnd = int(self.winId())
        try:
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(
                hwnd, _WDA_EXCLUDEFROMCAPTURE,
            )
            if ok:
                log.info("[Z] WDA_EXCLUDEFROMCAPTURE applied — overlay hidden from capture")
            else:
                err = ctypes.windll.kernel32.GetLastError()
                log.warning(
                    "[Z] SetWindowDisplayAffinity failed (err=%d) — "
                    "overlay will be visible in captures", err,
                )
        except Exception as exc:
            log.warning("[Z] SetWindowDisplayAffinity unavailable: %s", exc)

    @pyqtSlot(list, list, list, list)
    def update_highlights(
        self,
        detected: list,
        missing: list,
        slot_hits: list,
        active_regions: list | None = None,
    ) -> None:
        self._slot_hits = slot_hits
        self._missing = set(missing)
        cfg = load_config()
        if active_regions is not None:
            # Strip mode: use dynamically inferred regions so highlight boxes
            # follow the actual card positions regardless of card count.
            self._regions = active_regions
        else:
            self._regions = cfg.get("ocr_regions", [])
        self._highlight_above = cfg.get("card_highlight_above", 30)
        self._highlight_below = cfg.get("card_highlight_below", 185)
        # Re-enforce Z-order after every repaint cycle to survive Qt's paint bookkeeping
        if self._overlay_hwnd:
            _enforce_z_below(int(self.winId()), self._overlay_hwnd)
        self.update()

    def clear(self) -> None:
        """Remove all highlight boxes (called when scanner is paused)."""
        self._slot_hits = []
        self._missing = set()
        self.update()

    def clear_debug(self) -> None:
        """Remove the debug OCR overlay."""
        self._debug_data = None
        self.update()

    @pyqtSlot(dict)
    def update_debug_ocr(self, data: dict) -> None:
        """Receive per-slot OCR debug information from TalentScanner."""
        self._debug_data = data
        self.update()

    def _draw_debug_overlay(self, painter: QPainter) -> None:
        """
        Visualise OCR scan areas.
          Cyan dashed rect  — the wide name-strip grab region
          Green  solid rect  — slot with a confirmed talent match
          Orange solid rect  — slot with a near-miss (score 40–threshold)
          Grey   dashed rect — slot with no match / empty OCR
        Each slot shows its index badge, raw OCR text, and match result below.
        """
        data = self._debug_data
        if data is None:
            return

        strip_r       = data.get("strip_region")
        active_regions = data.get("active_regions", [])
        slot_data      = data.get("slot_data", [])

        # ── Owned-panel scan region ───────────────────────────────────────
        owned_r = load_config().get("talents_panel_region")
        if owned_r and owned_r.get("w", 0) > 0:
            pen = QPen(QColor(255, 140, 0, 220), 2)   # orange
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 100, 0, 14))
            painter.drawRect(owned_r["x"], owned_r["y"], owned_r["w"], owned_r["h"])
            font_lbl = QFont()
            font_lbl.setPointSize(8)
            font_lbl.setBold(True)
            painter.setFont(font_lbl)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRect(owned_r["x"], owned_r["y"], 180, 16)
            painter.setPen(QColor(255, 165, 0, 240))
            painter.drawText(owned_r["x"] + 4, owned_r["y"] + 11, "📷 OWNED PANEL REGION")

        font_bold = QFont()
        font_bold.setPointSize(8)
        font_bold.setBold(True)
        font_small = QFont()
        font_small.setPointSize(7)

        # ── Strip boundary ────────────────────────────────────────────────
        if strip_r and strip_r.get("w", 0) > 0:
            pen = QPen(QColor(0, 220, 255, 200), 2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(strip_r["x"], strip_r["y"], strip_r["w"], strip_r["h"])
            # Label above the strip
            painter.setFont(font_bold)
            painter.setPen(QColor(0, 220, 255, 230))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRect(strip_r["x"], strip_r["y"] - 18, 120, 16)
            painter.setPen(QColor(0, 220, 255, 230))
            painter.drawText(strip_r["x"] + 4, strip_r["y"] - 5, "╠ STRIP REGION ═══")

        # ── Per-slot boxes ────────────────────────────────────────────────
        slot_map = {entry["slot"]: entry for entry in slot_data}
        for i, r in enumerate(active_regions):
            entry     = slot_map.get(i, {})
            raw       = entry.get("raw",      "")
            match     = entry.get("match",     None)
            near_miss = entry.get("near_miss", None)
            score     = entry.get("score",     0.0)

            if match:
                outline = QColor(60,  220,  80, 230)
                lbl_bg  = QColor(0,   50,    0, 185)
                lbl_fg  = QColor(120, 255, 130, 255)
                status  = f"✓ {match}  ({score:.0f})"
                line_sty = Qt.SolidLine
            elif near_miss:
                outline = QColor(255, 160,   0, 215)
                lbl_bg  = QColor(60,   30,   0, 185)
                lbl_fg  = QColor(255, 195,  60, 255)
                status  = f"~ {near_miss}  ({score:.0f})"
                line_sty = Qt.SolidLine
            else:
                outline = QColor(130, 130, 130, 160)
                lbl_bg  = QColor(0,     0,   0, 150)
                lbl_fg  = QColor(160, 160, 160, 200)
                status  = "— no match"
                line_sty = Qt.DashLine

            # Slot outline
            pen = QPen(outline, 2)
            pen.setStyle(line_sty)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r["x"], r["y"], r["w"], r["h"])

            # Slot index badge (top-left corner of region)
            painter.setPen(Qt.NoPen)
            painter.setBrush(outline)
            painter.drawRect(r["x"], r["y"], 18, 16)
            painter.setFont(font_bold)
            painter.setPen(QColor(0, 0, 0, 230))
            painter.drawText(r["x"] + 2, r["y"] + 11, str(i + 1))

            # Labels drawn just below the slot region
            ly = r["y"] + r["h"] + 2

            # Line 1 — raw OCR text
            raw_show = (raw[:38] + "…") if len(raw) > 38 else (raw or "  (empty)")
            painter.setFont(font_small)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRect(r["x"], ly, r["w"], 14)
            painter.setPen(QColor(200, 200, 200, 215))
            painter.drawText(r["x"] + 3, ly + 10, raw_show)

            # Line 2 — match/near-miss/no-match status
            painter.setFont(font_bold)
            painter.setPen(Qt.NoPen)
            painter.setBrush(lbl_bg)
            painter.drawRect(r["x"], ly + 16, r["w"], 16)
            painter.setPen(lbl_fg)
            painter.drawText(r["x"] + 4, ly + 27, status)

    # ------------------------------------------------------------------
    # Win32 Z-order / click-through helpers
    # ------------------------------------------------------------------
    def _install_mouse_hook(self) -> None:
        global _g_mouse_hook_handle, _g_active_highlight
        _g_active_highlight = self
        try:
            _g_mouse_hook_handle = ctypes.windll.user32.SetWindowsHookExW(
                _WH_MOUSE_LL, _MOUSE_HOOK_PROC, None, 0
            )
            if _g_mouse_hook_handle:
                log.debug("[MOUSE] Low-level mouse hook installed")
            else:
                err = ctypes.windll.kernel32.GetLastError()
                log.warning("[MOUSE] SetWindowsHookExW failed (err=%d) — pick-to-own disabled", err)
        except Exception as exc:
            log.warning("[MOUSE] Failed to install mouse hook: %s", exc)

    def _uninstall_mouse_hook(self) -> None:
        global _g_mouse_hook_handle, _g_active_highlight
        _g_active_highlight = None
        if _g_mouse_hook_handle:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(_g_mouse_hook_handle)
                log.debug("[MOUSE] Mouse hook uninstalled")
            except Exception as exc:
                log.warning("[MOUSE] Failed to uninstall mouse hook: %s", exc)
            finally:
                _g_mouse_hook_handle = None

    def _on_mouse_click(self, x: int, y: int) -> None:
        """
        Called by the Win32 mouse hook on every left-button release.

        Checks whether (x, y) falls inside any currently highlighted card-slot
        bounding box.  If it does and the talent is still missing, schedules
        talent_picked emission via QTimer.singleShot so the signal is delivered
        on the next event-loop iteration (safe re-entrancy from within a hook).
        """
        for region_idx, talent_name in self._slot_hits:
            if talent_name not in self._missing:
                continue
            if region_idx >= len(self._regions):
                continue
            r = self._regions[region_idx]
            rh = r["h"]
            if rh < 120:
                eff_above = max(self._highlight_above, int(rh * 1.15))
                eff_below = max(self._highlight_below, int(rh * 3.45))
            else:
                eff_above = self._highlight_above
                eff_below = self._highlight_below
            bx = r["x"] - 4
            by = r["y"] - eff_above
            bw = r["w"] + 8
            bh = rh + eff_above + eff_below
            if bx <= x < bx + bw and by <= y < by + bh:
                log.info(
                    "[PICK] Click (%d, %d) hit slot %d  talent='%s'",
                    x, y, region_idx, talent_name,
                )
                QTimer.singleShot(0, lambda t=talent_name: self.talent_picked.emit(t))
                break  # register only the first (topmost) match

    def closeEvent(self, event) -> None:
        self._uninstall_mouse_hook()
        super().closeEvent(event)

    def _check_z_order(self) -> None:
        """Periodic diagnostic: log a warning if card_highlight is above OverlayWindow."""
        from utils import load_config as _lc
        if not _lc().get("debug_zorder", False):
            return
        if not self._overlay_hwnd:
            return
        card_rank    = _get_window_z_rank(int(self.winId()))
        overlay_rank = _get_window_z_rank(self._overlay_hwnd)
        if card_rank == -1 or overlay_rank == -1:
            log.debug("[Z-diag] one window not in TOPMOST chain: card=%d overlay=%d",
                      card_rank, overlay_rank)
            return
        if card_rank < overlay_rank:
            log.warning(
                "[Z-diag] PROBLEM: card_highlight (rank %d) is ABOVE OverlayWindow (rank %d) "
                "— input may be broken. Correcting now.",
                card_rank, overlay_rank,
            )
            self.reapply_z_order()
        else:
            log.debug("[Z-diag] OK: card_highlight rank=%d overlay rank=%d",
                      card_rank, overlay_rank)

    def reapply_z_order(self) -> None:
        """
        Re-apply Win32 click-through styles and Z-order enforcement.
        Called by QTimer every 300 ms and after any hide/show cycle.
        """
        _apply_click_through(int(self.winId()))
        if self._overlay_hwnd:
            _enforce_z_below(int(self.winId()), self._overlay_hwnd)

    def nativeEvent(self, event_type, message):
        """
        Return HTTRANSPARENT for every WM_NCHITTEST message.

        This is the definitive click-through mechanism.  It fires synchronously
        for every single mouse hit-test, before Z-order is considered, and tells
        Windows "treat this window as if it doesn\'t exist for mouse input". 
        Z-order tricks and WS_EX_TRANSPARENT fix the *painting* layer; this fixes
        the *input routing* layer.  Both are needed for 100% reliability.
        """
        if event_type == b"windows_generic_MSG":
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            WM_NCHITTEST   = 0x0084
            HTTRANSPARENT  = -1
            if msg.message == WM_NCHITTEST:
                return True, HTTRANSPARENT
        return super().nativeEvent(event_type, message)

    def diagnose(self) -> str:
        """
        Return a multi-line diagnostic string describing the current Win32
        window state for both this window and OverlayWindow.
        Useful for the in-overlay Diag dialog.
        """
        card_hwnd    = int(self.winId())
        overlay_hwnd = self._overlay_hwnd
        card_ex    = _get_exstyle(card_hwnd)
        overlay_ex = _get_exstyle(overlay_hwnd) if overlay_hwnd else 0
        card_rank    = _get_window_z_rank(card_hwnd)
        overlay_rank = _get_window_z_rank(overlay_hwnd) if overlay_hwnd else -1

        has_transparent = bool(card_ex & _WS_EX_TRANSPARENT)
        has_noactivate  = bool(card_ex & _WS_EX_NOACTIVATE)
        z_ok = (overlay_rank != -1 and card_rank > overlay_rank) or overlay_rank == -1

        lines = [
            "=== Z-Order / Click-Through Diagnostics ===",
            f"CardHighlightOverlay  hwnd=0x{card_hwnd:08x}",
            f"  exstyle=0x{card_ex:08x}",
            f"  WS_EX_TRANSPARENT : {'YES ✓' if has_transparent else 'NO  ✗  <-- PROBLEM'}",
            f"  WS_EX_NOACTIVATE  : {'YES ✓' if has_noactivate  else 'NO  ✗  <-- PROBLEM'}",
            f"  Z-rank in TOPMOST : {card_rank}  (0=topmost)",
            "",
            f"OverlayWindow  hwnd=0x{overlay_hwnd:08x}",
            f"  exstyle=0x{overlay_ex:08x}",
            f"  Z-rank in TOPMOST : {overlay_rank}  (0=topmost)",
            "",
            f"Z-order correct (overlay above card): {'YES ✓' if z_ok else 'NO  ✗  <-- PROBLEM'}",
            "",
            "If WS_EX_TRANSPARENT=NO or Z-order wrong, the highlight window",
            "is blocking mouse input to the overlay. Check overlay.log for",
            "[Z] warning lines that show when/why the state was disrupted.",
        ]
        diag_str = "\n".join(lines)
        log.info("[DIAG]\n%s", diag_str)
        return diag_str

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        has_highlights = bool(self._slot_hits and self._missing)
        has_debug = bool(self._debug_data)
        if not has_highlights and not has_debug:
            return

        above: int = self._highlight_above
        below: int = self._highlight_below

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if has_highlights:
            for region_idx, talent_name in self._slot_hits:
                if talent_name not in self._missing:
                    continue            # already owned — no highlight
                if region_idx >= len(self._regions):
                    continue

                r = self._regions[region_idx]
                rh = r["h"]

                # Auto-calculate expansion for parchment-sized banners.
                # Parchment banners (h < 120) sit at the card top; a full
                # Deepwoken card is roughly 5.5× the banner height.
                if rh < 120:
                    eff_above = max(above, int(rh * 1.15))    # ~82 px
                    eff_below = max(below, int(rh * 3.45))    # ~245 px
                else:
                    eff_above = above
                    eff_below = below

                # Expand the narrow OCR strip into a full-card bounding box
                bx = r["x"] - 4
                by = r["y"] - eff_above
                bw = r["w"] + 8
                bh = rh + eff_above + eff_below

                # --- Faint white fill ---
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, 18))
                painter.drawRoundedRect(bx, by, bw, bh, 6, 6)

                # --- Bright white border ---
                pen = QPen(QColor(255, 255, 220, 240), 3)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(bx, by, bw, bh, 6, 6)

                # --- "PICK" badge at top-left corner ---
                badge_x = bx + 5
                badge_y = by + 4
                badge_w = 38
                badge_h = 18
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 220, 50, 210))
                painter.drawRoundedRect(badge_x, badge_y, badge_w, badge_h, 3, 3)

                font = QFont()
                font.setPointSize(8)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor(20, 20, 20, 255))
                painter.drawText(badge_x + 4, badge_y + 12, "PICK")

        if has_debug:
            self._draw_debug_overlay(painter)

        painter.end()
