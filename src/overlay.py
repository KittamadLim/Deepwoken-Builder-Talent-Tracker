import logging
import time
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ocr import ScanOwnedWorker
from region_picker import pick_region
from utils import load_config, save_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build Loader Dialog  — shown on startup to enter / change the build URL
# ---------------------------------------------------------------------------
_LOADER_STYLE = """
QDialog {
    background-color: #0a0a14;
}
QLabel {
    color: #e0e0e0;
    font-size: 12px;
}
QLabel#title_lbl {
    color: #aad4ff;
    font-size: 18px;
    font-weight: bold;
}
QLabel#sub_lbl {
    color: #666688;
    font-size: 11px;
}
QLabel#error_lbl {
    color: #ff6666;
    font-size: 11px;
}
QLineEdit {
    background-color: #1a1a2a;
    color: #e0e0e0;
    border: 1px solid #444466;
    border-radius: 4px;
    padding: 7px 10px;
    font-size: 12px;
}
QLineEdit:focus {
    border-color: #6699cc;
}
QPushButton {
    background-color: #2a2a3a;
    color: #cccccc;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 6px 18px;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #3a3a5a;
}
QPushButton#load_btn {
    background-color: #1a3a1a;
    color: #5dfc8a;
    border-color: #3a6a3a;
    font-weight: bold;
}
QPushButton#load_btn:hover {
    background-color: #2a5a2a;
}
"""


class BuildLoaderDialog(QDialog):
    """
    Startup dialog for entering (or changing) the Deepwoken builder URL.
    Replaces the old QInputDialog / --url CLI arg.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Deepwoken Builder Overlay")
        self.setMinimumWidth(500)
        self.setStyleSheet(_LOADER_STYLE)

        cfg = load_config()
        last_url: str = cfg.get("last_url", "https://deepwoken.co/builder?id=")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 22)
        layout.setSpacing(10)

        title = QLabel("Deepwoken Builder Overlay")
        title.setObjectName("title_lbl")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        sub = QLabel("Paste your build URL from deepwoken.co/builder")
        sub.setObjectName("sub_lbl")
        sub.setAlignment(Qt.AlignCenter)
        layout.addWidget(sub)

        layout.addSpacing(6)

        layout.addWidget(QLabel("Builder URL:"))
        self._url_edit = QLineEdit(last_url)
        self._url_edit.setPlaceholderText("https://deepwoken.co/builder?id=XXXXXXXX")
        self._url_edit.selectAll()
        self._url_edit.returnPressed.connect(self._on_load)
        layout.addWidget(self._url_edit)

        self._error_lbl = QLabel("")
        self._error_lbl.setObjectName("error_lbl")
        self._error_lbl.hide()
        layout.addWidget(self._error_lbl)

        layout.addSpacing(6)

        btn_row = QHBoxLayout()
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        load_btn = QPushButton("Load Build")
        load_btn.setObjectName("load_btn")
        load_btn.setDefault(True)
        load_btn.clicked.connect(self._on_load)
        btn_row.addWidget(quit_btn)
        btn_row.addStretch()
        btn_row.addWidget(load_btn)
        layout.addLayout(btn_row)

    @property
    def url(self) -> str:
        return self._url_edit.text().strip()

    def _on_load(self) -> None:
        u = self.url
        if not u or "deepwoken.co/builder" not in u or "id=" not in u:
            self._error_lbl.setText(
                "Please enter a valid deepwoken.co/builder URL containing 'id='"
            )
            self._error_lbl.show()
            return
        self.accept()


OVERLAY_STYLE = """
QWidget#overlay {
    background-color: rgba(10, 10, 20, 200);
    border-radius: 8px;
}
QLabel {
    color: #e0e0e0;
    font-size: 12px;
}
QLabel#section_header {
    color: #aad4ff;
    font-size: 13px;
    font-weight: bold;
    padding-bottom: 2px;
}
QLabel#separator {
    color: #333;
}
QPushButton {
    background-color: #2a2a3a;
    color: #cccccc;
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 11px;
}
QPushButton:hover {
    background-color: #3a3a5a;
}
QPushButton:disabled {
    color: #555555;
    border-color: #333333;
}
"""


# ---------------------------------------------------------------------------
# Settings Dialog
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OCR Region Settings")
        self.setMinimumWidth(400)
        self.setMinimumHeight(400)
        self._cfg = load_config()

        # Outer layout: scrollable content on top, buttons pinned at the bottom.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 8)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        btn_wrapper = QWidget()
        btn_layout = QVBoxLayout(btn_wrapper)
        btn_layout.setContentsMargins(10, 4, 10, 0)

        # --- Number of cards ---
        cards_count_group = QGroupBox("Number of Cards to Track")
        cards_count_form = QFormLayout(cards_count_group)
        cards_count_form.setSpacing(4)
        self._num_cards_sb = QSpinBox()
        self._num_cards_sb.setRange(1, 6)
        self._num_cards_sb.setValue(self._cfg.get("num_cards", 5))
        self._num_cards_sb.setToolTip(
            "Set to 6 if six cards can appear (e.g. Fold). "
            "Reopen Settings after saving to see the new region picker."
        )
        cards_count_form.addRow("Num cards (1–6)", self._num_cards_sb)
        layout.addWidget(cards_count_group)

        # --- Five card-region groups (continuous card scanner) ---
        num_cards: int = self._cfg.get("num_cards", 5)
        self._region_widgets: list[dict[str, QSpinBox]] = []
        regions = self._cfg.get("ocr_regions", [])
        while len(regions) < num_cards:
            regions.append({"x": 0, "y": 0, "w": 158, "h": 42})

        for i, region in enumerate(regions[:num_cards]):
            group = QGroupBox(f"Card Region {i + 1}  (F6 scanner)")
            form = QFormLayout(group)
            form.setSpacing(4)
            widgets: dict[str, QSpinBox] = {}
            for key, label in (("x", "X"), ("y", "Y"), ("w", "Width"), ("h", "Height")):
                sb = QSpinBox()
                sb.setRange(0, 7680)
                sb.setValue(int(region.get(key, 0)))
                form.addRow(label, sb)
                widgets[key] = sb
            pick_btn = QPushButton("📌 Pick on Screen")
            pick_btn.clicked.connect(self._make_picker(widgets))
            form.addRow("", pick_btn)
            self._region_widgets.append(widgets)
            layout.addWidget(group)

        # --- Owned-talents panel region (one-shot F7 scan) ---
        panel_group = QGroupBox("Owned Talents Panel  (F7 one-shot scan)")
        panel_form = QFormLayout(panel_group)
        panel_form.setSpacing(4)
        panel_region = self._cfg.get(
            "talents_panel_region", {"x": 1060, "y": 100, "w": 300, "h": 650}
        )
        self._panel_widgets: dict[str, QSpinBox] = {}
        for key, label in (("x", "X"), ("y", "Y"), ("w", "Width"), ("h", "Height")):
            sb = QSpinBox()
            sb.setRange(0, 7680)
            sb.setValue(int(panel_region.get(key, 0)))
            panel_form.addRow(label, sb)
            self._panel_widgets[key] = sb
        pick_panel_btn = QPushButton("📌 Pick on Screen")
        pick_panel_btn.clicked.connect(self._make_picker(self._panel_widgets))
        panel_form.addRow("", pick_panel_btn)
        layout.addWidget(panel_group)

        # --- Card name strip (fast OCR) ---
        raw_strip = self._cfg.get("name_strip_region") or {"x": 0, "y": 0, "w": 0, "h": 0}
        strip_group = QGroupBox("Card Name Strip  (Fast OCR — optional)")
        strip_form = QFormLayout(strip_group)
        strip_form.setSpacing(4)
        self._strip_widgets: dict[str, QSpinBox] = {}
        for key, label in (("x", "X"), ("y", "Y"), ("w", "Width"), ("h", "Height")):
            sb = QSpinBox()
            sb.setRange(0, 7680)
            sb.setValue(int(raw_strip.get(key, 0)))
            strip_form.addRow(label, sb)
            self._strip_widgets[key] = sb
        strip_info = QLabel(
            "Pick a wide horizontal strip covering all card-name banners.\n"
            "When set, uses 1 OCR call instead of one per card — much faster\n"
            "and handles 5 or 6 cards automatically."
        )
        strip_info.setStyleSheet("color: #aaaaaa; font-size: 10px;")
        strip_form.addRow("", strip_info)
        pick_strip_btn = QPushButton("📌 Pick Name Strip")
        pick_strip_btn.clicked.connect(self._make_picker(self._strip_widgets))
        strip_form.addRow("", pick_strip_btn)
        layout.addWidget(strip_group)

        # --- Scanner settings ---
        misc_group = QGroupBox("Scanner Settings")
        misc_form = QFormLayout(misc_group)
        misc_form.setSpacing(4)

        self._interval_sb = QSpinBox()
        self._interval_sb.setRange(100, 10_000)
        self._interval_sb.setSingleStep(100)
        self._interval_sb.setSuffix(" ms")
        self._interval_sb.setValue(int(self._cfg.get("ocr_interval_ms", 1000)))
        misc_form.addRow("Scan interval", self._interval_sb)

        self._threshold_sb = QSpinBox()
        self._threshold_sb.setRange(1, 100)
        self._threshold_sb.setValue(int(self._cfg.get("fuzzy_threshold", 72)))
        misc_form.addRow("Fuzzy threshold (0–100)", self._threshold_sb)

        layout.addWidget(misc_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        btn_layout.addWidget(buttons)
        outer.addWidget(btn_wrapper)

    def _on_ok(self) -> None:
        regions = []
        for widgets in self._region_widgets:
            regions.append({k: widgets[k].value() for k in ("x", "y", "w", "h")})
        self._cfg["ocr_regions"] = regions

        # Persist num_cards and pad/trim the region list to match.
        new_nc = self._num_cards_sb.value()
        self._cfg["num_cards"] = new_nc
        while len(self._cfg["ocr_regions"]) < new_nc:
            self._cfg["ocr_regions"].append({"x": 0, "y": 0, "w": 158, "h": 42})
        self._cfg["ocr_regions"] = self._cfg["ocr_regions"][:new_nc]

        self._cfg["talents_panel_region"] = {
            k: self._panel_widgets[k].value() for k in ("x", "y", "w", "h")
        }
        self._cfg["ocr_interval_ms"] = self._interval_sb.value()
        self._cfg["fuzzy_threshold"] = self._threshold_sb.value()

        # Name strip — save None when width is zero (= disabled).
        strip_vals = {k: self._strip_widgets[k].value() for k in ("x", "y", "w", "h")}
        self._cfg["name_strip_region"] = (
            strip_vals if strip_vals["w"] > 0 and strip_vals["h"] > 0 else None
        )

        save_config(self._cfg)
        log.info("Settings saved: %s", self._cfg)
        # Re-enforce Z-order so the card highlight stays below OverlayWindow
        # after the dialog closes and focus changes.
        parent = self.parent()
        if parent:
            ch = getattr(parent, "_card_highlight", None)
            if ch:
                QApplication.processEvents()
                ch.reapply_z_order()
            parent.raise_()
        self.accept()

    def _make_picker(self, widgets: dict) -> callable:
        """Return a callback that opens the screen picker and fills the given spinboxes."""
        def _run(*_):
            parent = self.parent()
            ch = getattr(parent, "_card_highlight", None) if parent else None

            # Hide overlay + card highlight so they don't appear in the screenshot
            # and don't interfere with Z-order when the picker closes
            if parent:
                parent.hide()
            if ch:
                ch.hide()
            self.hide()
            QApplication.processEvents()
            time.sleep(0.25)  # let OS redraw before screenshot

            region = pick_region()

            # Restore in correct order: overlay first (becomes reference Z-anchor),
            # then dialog on top of overlay, then card_highlight below overlay
            if parent:
                parent.show()
                parent.raise_()
            self.show()
            self.raise_()
            self.activateWindow()  # restore KB/mouse focus to the settings dialog
            if ch:
                ch.show()
                QApplication.processEvents()  # let show() settle before correcting Z-order
                ch.reapply_z_order()
                # Raise OverlayWindow once more to ensure it wins the Z fight
                if parent:
                    parent.raise_()

            if region:
                for k in ("x", "y", "w", "h"):
                    widgets[k].setValue(region[k])
        return _run


# ---------------------------------------------------------------------------
# Main Overlay Window
# ---------------------------------------------------------------------------
class OverlayWindow(QWidget):
    """
    Always-on-top, frameless, semi-transparent overlay.

    Tracking modes:
      F6 / ▶⏸ button  — toggle continuous card-region OCR scanner
      F7 / 📷 button  — one-shot scan of the owned-talents panel, persists results
    """

    def __init__(self, stat_order: list[dict], post_order: list[dict], build_talents: list[str], scanner, card_highlight=None):
        super().__init__()
        self._drag_pos = None
        self._talent_labels: dict[str, QLabel] = {}
        self._build_talents = build_talents
        self._scanner = scanner
        self._card_highlight = card_highlight
        self._scan_worker = None  # ScanOwnedWorker held here to prevent GC

        # Load persisted owned talents from previous sessions
        cfg = load_config()
        self._known_owned: set[str] = set(cfg.get("known_owned_talents", []))

        self.setObjectName("overlay")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(OVERLAY_STYLE)
        self.setMinimumWidth(290)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(6)

        # -------- Pre-Shrine Stat Priority --------
        s1_header = QLabel("Pre-Shrine Order:")
        s1_header.setObjectName("section_header")
        root.addWidget(s1_header)

        if stat_order:
            for i, entry in enumerate(stat_order, start=1):
                lbl = QLabel(f"  {i}.  {entry['stat']}  \u2192  {entry['target']}")
                root.addWidget(lbl)
        else:
            root.addWidget(QLabel("  (no pre-shrine data available)"))

        # -------- Post-Shrine Stat Priority --------
        if post_order:
            sep_shrine = QLabel("\u2500" * 36)
            sep_shrine.setObjectName("separator")
            root.addWidget(sep_shrine)

            s1b_header = QLabel("Post-Shrine Order:")
            s1b_header.setObjectName("section_header")
            s1b_header.setStyleSheet(
                "color: #ffcc66; font-size: 13px; font-weight: bold; padding-bottom: 2px;"
            )
            root.addWidget(s1b_header)

            for i, entry in enumerate(post_order, start=1):
                lbl = QLabel(f"  {i}.  {entry['stat']}  +{entry['target']}")
                lbl.setStyleSheet("color: #ffddaa;")
                root.addWidget(lbl)

        sep1 = QLabel("─" * 36)
        sep1.setObjectName("separator")
        root.addWidget(sep1)

        # -------- Talent Tracker header + status --------
        tracker_row = QHBoxLayout()
        s2_header = QLabel("Talent Tracker:")
        s2_header.setObjectName("section_header")
        self._status_lbl = QLabel("● Idle")
        self._status_lbl.setStyleSheet("color: #666666; font-size: 11px;")
        tracker_row.addWidget(s2_header)
        tracker_row.addStretch()
        tracker_row.addWidget(self._status_lbl)
        root.addLayout(tracker_row)

        # -------- Talent labels --------
        talent_container = QWidget()
        talent_container.setObjectName("overlay")
        talent_layout = QVBoxLayout(talent_container)
        talent_layout.setContentsMargins(0, 0, 0, 0)
        talent_layout.setSpacing(2)

        if build_talents:
            for talent in build_talents:
                lbl = QLabel()
                if talent in self._known_owned:
                    lbl.setText(f"[✓]  {talent}")
                    lbl.setStyleSheet("color: #5dfc8a;")
                else:
                    lbl.setText(f"[ ]  {talent}")
                    lbl.setStyleSheet("color: #666666;")
                self._talent_labels[talent] = lbl
                talent_layout.addWidget(lbl)
        else:
            talent_layout.addWidget(QLabel("  (no talents in build)"))

        scroll = QScrollArea()
        scroll.setWidget(talent_container)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(300)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        root.addWidget(scroll)

        sep2 = QLabel("─" * 36)
        sep2.setObjectName("separator")
        root.addWidget(sep2)

        # -------- Row 1: tracking controls --------
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(6)
        self._toggle_btn = QPushButton("▶ Start Tracking  [F6]")
        self._toggle_btn.clicked.connect(self._toggle_tracking)
        self._scan_owned_btn = QPushButton("📷 Scan Owned  [F7]")
        self._scan_owned_btn.clicked.connect(self._scan_owned)
        ctrl_row.addWidget(self._toggle_btn)
        ctrl_row.addWidget(self._scan_owned_btn)
        root.addLayout(ctrl_row)

        # -------- Row 2: app controls --------
        app_row = QHBoxLayout()
        app_row.setSpacing(6)
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self._open_settings)
        quit_btn = QPushButton("✕ Quit")
        quit_btn.clicked.connect(QApplication.instance().quit)
        diag_btn = QPushButton("\U0001f50d Diag")
        diag_btn.setToolTip("Show Z-order diagnostics and last log lines")
        diag_btn.clicked.connect(self._open_diag)
        reset_btn = QPushButton("🗑 Reset Owned  [F9]")
        reset_btn.setToolTip("Clear all owned-talent checkmarks")
        reset_btn.clicked.connect(self._reset_owned)
        app_row.addWidget(settings_btn)
        app_row.addWidget(diag_btn)
        app_row.addWidget(quit_btn)
        root.addLayout(app_row)

        # -------- Row 3: reset --------
        reset_row = QHBoxLayout()
        reset_row.addWidget(reset_btn)
        root.addLayout(reset_row)

        self.adjustSize()
        self.move(50, 50)
        self.show()

    # ------------------------------------------------------------------
    # Tracking toggle (F6)
    # ------------------------------------------------------------------
    def _toggle_tracking(self) -> None:
        if self._scanner.is_paused:
            self._scanner.resume()
            self._toggle_btn.setText("⏸ Stop Tracking  [F6]")
            self._toggle_btn.setStyleSheet(
                "background-color:#1a3a1a; color:#5dfc8a; border:1px solid #3a6a3a;"
                " border-radius:4px; padding:4px 10px; font-size:11px;"
            )
            self._status_lbl.setText("● Scanning")
            self._status_lbl.setStyleSheet("color: #5dfc8a; font-size: 11px;")
            log.info("Card tracking started")
        else:
            self._scanner.pause()
            self._toggle_btn.setText("▶ Start Tracking  [F6]")
            self._toggle_btn.setStyleSheet("")  # reset to stylesheet default
            self._status_lbl.setText("● Idle")
            self._status_lbl.setStyleSheet("color: #666666; font-size: 11px;")
            self._refresh_all_labels()
            if self._card_highlight:
                self._card_highlight.clear()
            log.info("Card tracking stopped")

    # ------------------------------------------------------------------
    # One-shot owned-panel scan (F7)
    # ------------------------------------------------------------------
    def _scan_owned(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return  # already scanning

        self._scan_owned_btn.setEnabled(False)
        self._scan_owned_btn.setText("Scanning…")

        self._scan_worker = ScanOwnedWorker(self._build_talents, parent=self)
        self._scan_worker.scan_done.connect(self._on_scan_owned_done)
        self._scan_worker.start()
        log.info("Owned-panel scan started")

    @pyqtSlot(list)
    def _on_scan_owned_done(self, matched: list) -> None:
        """Merge scan results into the persistent owned set and save."""
        self._known_owned.update(matched)
        cfg = load_config()
        cfg["known_owned_talents"] = sorted(self._known_owned)
        save_config(cfg)

        self._refresh_all_labels()
        self._scan_owned_btn.setEnabled(True)
        self._scan_owned_btn.setText("📷 Scan Owned  [F7]")
        log.info(
            "Owned scan done — %d new, %d total owned", len(matched), len(self._known_owned)
        )

    def _refresh_all_labels(self) -> None:
        """Redraw all talent labels from persisted known_owned (no live scan data)."""
        for talent, lbl in self._talent_labels.items():
            if talent in self._known_owned:
                lbl.setText(f"[✓]  {talent}")
                lbl.setStyleSheet("color: #5dfc8a;")
            else:
                lbl.setText(f"[ ]  {talent}")
                lbl.setStyleSheet("color: #666666;")

    # ------------------------------------------------------------------
    # Live update from TalentScanner (only called while tracking is ON)
    # ------------------------------------------------------------------
    @pyqtSlot(list, list, list)
    def update_talents(self, card_detected: list, _missing: list, _slot_hits: list) -> None:
        """
        Update live display during F6 scanning.
        Does NOT persist — only F7 (scan owned) makes talents permanently owned.
          [✓] green  = confirmed owned (from F7 scan)
          [→] yellow = visible on screen right now, not yet owned (will be highlighted)
          [✗] red    = not visible, not yet owned
        """
        on_screen = set(card_detected)
        for talent, lbl in self._talent_labels.items():
            if talent in self._known_owned:
                lbl.setText(f"[✓]  {talent}")
                lbl.setStyleSheet("color: #5dfc8a;")
            elif talent in on_screen:
                # Visible on screen — OCR saw it. Highlight overlay will draw a box.
                lbl.setText(f"[→]  {talent}")
                lbl.setStyleSheet("color: #ffdd44;")
            else:
                lbl.setText(f"[✗]  {talent}")
                lbl.setStyleSheet("color: #fc5d5d;")

    def _reset_owned(self) -> None:
        """Clear all persisted owned-talent marks and refresh the UI."""
        self._known_owned.clear()
        cfg = load_config()
        cfg["known_owned_talents"] = []
        save_config(cfg)
        self._refresh_all_labels()
        if self._card_highlight:
            self._card_highlight.clear()
        log.info("Owned talents reset")

    @pyqtSlot(str)
    def mark_talent_picked(self, talent: str) -> None:
        """
        Called when the player left-clicks on a highlighted card.
        Immediately marks that talent as owned, persists it, and refreshes
        the label — no F7 scan required.
        """
        if talent not in self._build_talents or talent in self._known_owned:
            return
        self._known_owned.add(talent)
        cfg = load_config()
        cfg["known_owned_talents"] = sorted(self._known_owned)
        save_config(cfg)
        lbl = self._talent_labels.get(talent)
        if lbl:
            lbl.setText(f"[✓]  {talent}")
            lbl.setStyleSheet("color: #5dfc8a;")
        log.info("Talent picked and marked as owned: '%s'", talent)

    def _open_settings(self) -> None:
        # Guard: if a settings dialog is already open, just bring it forward.
        existing = getattr(self, '_settings_dlg', None)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        dlg = SettingsDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        # Track the open dialog so we can guard against duplicates.
        self._settings_dlg = dlg
        dlg.destroyed.connect(lambda *_: setattr(self, '_settings_dlg', None))
        # Use show() instead of exec_() to avoid nested-event-loop modal
        # corruption on Windows that leaves the overlay Win32-disabled after
        # the picker's inner QEventLoop runs and returns.
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _open_diag(self) -> None:
        dlg = DiagDialog(self, self._card_highlight)
        dlg.exec_()

    # --- Drag support ---
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ---------------------------------------------------------------------------
# Diagnostic Dialog
# ---------------------------------------------------------------------------
class DiagDialog(QDialog):
    """
    Shows:
    - Live Win32 Z-order + click-through state from CardHighlightOverlay.diagnose()
    - Last 60 lines of overlay.log so the user never has to alt-tab
    """

    def __init__(self, parent=None, card_highlight=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostics")
        self.setMinimumSize(540, 480)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # -- Win32 state --
        layout.addWidget(QLabel("Win32 Window State:"))
        self._win32_box = QTextEdit()
        self._win32_box.setReadOnly(True)
        self._win32_box.setMaximumHeight(220)
        self._win32_box.setStyleSheet(
            "background:#111; color:#aaffaa; font-family:Consolas,monospace; font-size:11px;"
        )
        layout.addWidget(self._win32_box)

        # -- Log tail --
        layout.addWidget(QLabel("Last 60 lines of overlay.log:"))
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            "background:#111; color:#dddddd; font-family:Consolas,monospace; font-size:10px;"
        )
        layout.addWidget(self._log_box)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("\u21bb Refresh")
        refresh_btn.clicked.connect(lambda: self._populate(card_highlight))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._populate(card_highlight)

    def _populate(self, card_highlight) -> None:
        # Win32 state
        if card_highlight and hasattr(card_highlight, "diagnose"):
            self._win32_box.setPlainText(card_highlight.diagnose())
        else:
            self._win32_box.setPlainText("(CardHighlightOverlay not available)")

        # Log tail
        from utils import BASE_DIR
        log_path = BASE_DIR / "overlay.log"
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-60:])
        except FileNotFoundError:
            tail = "(overlay.log not found)"
        self._log_box.setPlainText(tail)
        # Scroll to bottom so most recent entries are visible
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())
