"""
region_picker.py — Full-screen rubber-band region selector.

Call pick_region() from any Qt context to let the user visually drag a
rectangle over a live screenshot.  Returns {"x", "y", "w", "h"} in absolute
screen coordinates (mss-compatible), or None if cancelled.
"""

import logging
import time

import cv2
import mss
import numpy as np
from PyQt5.QtCore import Qt, QEventLoop, QPoint, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)


class _PickerWidget(QWidget):
    """Frameless full-screen overlay — user drags to select a rectangle."""

    region_selected = pyqtSignal(dict)
    cancelled = pyqtSignal()

    def __init__(self, pixmap: QPixmap, monitor_offset: tuple[int, int]):
        super().__init__()
        self._pixmap = pixmap
        self._offset_x, self._offset_y = monitor_offset
        self._start: QPoint | None = None
        self._end: QPoint | None = None
        self._selecting = False

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)
        # Dim overlay so instructions / selection stand out
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        # Instructions
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(
            self.rect().adjusted(0, 12, 0, 0),
            Qt.AlignTop | Qt.AlignHCenter,
            "Drag to select region    |    ESC to cancel",
        )
        if self._start and self._end:
            sel = QRect(self._start, self._end).normalized()
            # Punch-through: show crisp screenshot inside selection
            p.drawPixmap(sel, self._pixmap, sel)
            # Selection border
            pen = QPen(QColor(0, 200, 255), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(sel)
            # Dimensions label
            info = f"  {sel.width()} × {sel.height()}  at ({sel.x() + self._offset_x}, {sel.y() + self._offset_y})"
            p.setPen(QColor(0, 220, 255))
            label_y = sel.bottom() + 20
            if label_y > self.height() - 20:
                label_y = sel.top() - 8
            p.drawText(sel.left(), label_y, info)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.pos()
            self._end = event.pos()
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self._end = event.pos()
            sel = QRect(self._start, self._end).normalized()
            if sel.width() > 5 and sel.height() > 5:
                self.region_selected.emit(
                    {
                        "x": sel.x() + self._offset_x,
                        "y": sel.y() + self._offset_y,
                        "w": sel.width(),
                        "h": sel.height(),
                    }
                )
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()


def pick_region() -> dict | None:
    """
    Capture the primary monitor, show it fullscreen, let the user
    drag a rectangle, and return the selected region as
    {"x": int, "y": int, "w": int, "h": int} in absolute coordinates.

    Returns None if the user cancels or the selection is too small.
    The caller is responsible for hiding/showing any windows before calling.
    """
    with mss.mss() as sct:
        # Monitor index 1 = primary monitor; index 0 = virtual combined screen
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = np.array(sct.grab(monitor))

    rgb = cv2.cvtColor(shot, cv2.COLOR_BGRA2RGB)
    h, w, _ = rgb.shape
    img = QImage(rgb.tobytes(), w, h, w * 3, QImage.Format_RGB888)
    pixmap = QPixmap.fromImage(img)

    monitor_offset = (monitor.get("left", 0), monitor.get("top", 0))

    result: list[dict | None] = [None]
    loop = QEventLoop()

    def _on_selected(r: dict) -> None:
        result[0] = r
        loop.quit()

    picker = _PickerWidget(pixmap, monitor_offset)
    picker.region_selected.connect(_on_selected)
    picker.cancelled.connect(loop.quit)
    picker.destroyed.connect(lambda *_: loop.quit())
    picker.showFullScreen()
    loop.exec_()

    if result[0]:
        log.info("Region picked: %s", result[0])
    else:
        log.info("Region picker cancelled")

    return result[0]
