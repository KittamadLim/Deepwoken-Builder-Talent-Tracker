"""
Deepwoken Builder Overlay — entry point.

Launch:
    python main.py          # opens the URL input dialog

Global hotkeys (configurable in config.json):
    F6 — toggle continuous card-region tracking on/off
    F7 — one-shot scan of the owned-talents panel (saves result)
"""

import logging
import os
import signal
import sys

# Ensure src/ is on the path so all modules import each other by bare name.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from api import fetch_build
from highlight_overlay import CardHighlightOverlay
from ocr import TalentScanner
from optimizer import TALENT_DB, compute_priority
from overlay import BuildLoaderDialog, OverlayWindow
from utils import load_config, setup_logging

log = logging.getLogger(__name__)


class _HotkeyBridge(QObject):
    """
    Thread-safe proxy for the keyboard library's background thread.
    Signal emissions are automatically queued to the Qt main thread.
    """
    f6_pressed = pyqtSignal()
    f7_pressed = pyqtSignal()
    f8_pressed = pyqtSignal()  # diagnostics dump (no click needed)
    f9_pressed = pyqtSignal()  # reset owned talents


def main() -> None:
    setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("Deepwoken Builder Overlay")

    loader = BuildLoaderDialog()
    if loader.exec_() != QDialog.Accepted:
        sys.exit(0)
    url = loader.url

    log.info("Using build URL: %s", url)

    # Auto-reset owned talents when the user loads a different build.
    _cfg = load_config()
    if _cfg.get("last_url", "") != url:
        log.info("Build URL changed — resetting owned talents.")
        _cfg["known_owned_talents"] = []
        _cfg["last_url"] = url
        from utils import save_config as _save
        _save(_cfg)
    else:
        # Always persist the URL so it survives the first launch.
        if "last_url" not in _cfg:
            _cfg["last_url"] = url
            from utils import save_config as _save
            _save(_cfg)

    try:
        build_data = fetch_build(url)
    except Exception as exc:
        log.exception("Failed to fetch build data")
        QMessageBox.critical(
            None,
            "Fetch Error",
            f"Could not load build data:\n\n{exc}\n\nCheck your URL and internet connection.",
        )
        sys.exit(1)

    pre_order, post_order = compute_priority(
        build_data["pre_shrine"],
        build_data.get("post_shrine", {}),
        TALENT_DB,
    )
    talents = build_data["talents"]
    log.info(
        "Build loaded: %d pre-shrine + %d post-shrine stat targets, %d talents tracked",
        len(pre_order), len(post_order), len(talents),
    )

    # OverlayWindow must be created BEFORE CardHighlightOverlay so its HWND
    # can be passed in for permanent Z-order enforcement.
    scanner = TalentScanner(talents)
    window = OverlayWindow(pre_order, post_order, talents, scanner, None)  # card_highlight set below

    # Fullscreen transparent highlight overlay — always below OverlayWindow
    card_highlight = CardHighlightOverlay(overlay_hwnd=int(window.winId()))
    window._card_highlight = card_highlight  # wire reference now that both exist

    scanner.results_ready.connect(window.update_talents)
    scanner.results_ready.connect(card_highlight.update_highlights)
    # When the player clicks a highlighted card, mark it as owned immediately.
    card_highlight.talent_picked.connect(window.mark_talent_picked)
    scanner.start()  # starts the QThread (internally paused until resumed)

    # Belt-and-suspenders: also raise OverlayWindow to the front of the topmost layer
    window.raise_()

    # --- Global hotkeys ---
    cfg = load_config()
    hotkey_toggle = cfg.get("hotkey_toggle",     "F6")
    hotkey_scan   = cfg.get("hotkey_scan_owned",  "F7")
    hotkey_diag   = cfg.get("hotkey_diag",        "F8")
    hotkey_reset  = cfg.get("hotkey_reset_owned", "F9")

    def _dump_diag():
        """Keypress handler: runs diagnose() and logs result — no click needed."""
        if card_highlight:
            card_highlight.diagnose()
        log.info("[DIAG via %s] check overlay.log for full report", hotkey_diag)

    _bridge = _HotkeyBridge()
    _bridge.f6_pressed.connect(window._toggle_tracking)
    _bridge.f7_pressed.connect(window._scan_owned)
    _bridge.f8_pressed.connect(_dump_diag)
    _bridge.f9_pressed.connect(window._reset_owned)

    try:
        import keyboard
        keyboard.add_hotkey(hotkey_toggle, _bridge.f6_pressed.emit)
        keyboard.add_hotkey(hotkey_scan,   _bridge.f7_pressed.emit)
        keyboard.add_hotkey(hotkey_diag,   _bridge.f8_pressed.emit)
        keyboard.add_hotkey(hotkey_reset,  _bridge.f9_pressed.emit)
        log.info(
            "Global hotkeys: %s=toggle tracking, %s=scan owned, %s=dump diagnostics, %s=reset owned",
            hotkey_toggle, hotkey_scan, hotkey_diag, hotkey_reset,
        )
    except ImportError:
        log.warning("'keyboard' package not installed — global hotkeys disabled.")
    except Exception as exc:
        log.warning("Could not register global hotkeys: %s", exc)

    exit_code = 0
    try:
        # Allow Ctrl+C in the terminal to reach Python's signal handler
        # even while the Qt event loop is blocking.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        exit_code = app.exec_()
    except KeyboardInterrupt:
        log.info("Interrupted by keyboard — shutting down cleanly.")
    finally:
        scanner.stop()
        try:
            import keyboard as kb
            kb.unhook_all()
        except Exception:
            pass

    log.info("Exiting (code %d)", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
