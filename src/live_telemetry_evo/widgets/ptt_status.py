"""Compact overlay indicator: PTT held LED + last error text."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from ..colors import Colors
from ..fonts import label_font


class PTTStatusView(QWidget):
    """Small always-on PTT lamp for the overlay.

    Lit while the bound control is held / recording. Shows the last mic
    or upload error so failures are visible without opening session.log.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._held = False
        self._error = ""
        self._idle_label = "PTT"
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_held(self, held: bool) -> None:
        held = bool(held)
        if held == self._held:
            return
        self._held = held
        if held:
            self._error = ""
        self.update()

    def set_error(self, message: str) -> None:
        text = (message or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        if text == self._error:
            return
        self._error = text
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        if w < 8 or h < 8:
            p.end()
            return

        bg = QColor(0, 0, 0)
        bg.setAlphaF(0.45)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)

        lamp_d = max(8, int(h * 0.45))
        lamp_x = int(h * 0.28)
        lamp_y = (h - lamp_d) // 2
        if self._held:
            lamp = Colors.green
        elif self._error:
            lamp = Colors.red
        else:
            lamp = QColor(90, 90, 90)
        p.setBrush(lamp)
        p.drawEllipse(lamp_x, lamp_y, lamp_d, lamp_d)

        if self._held:
            label = "PTT ON"
            color = Colors.white
        elif self._error:
            label = self._error
            color = Colors.yellow
        else:
            label = self._idle_label
            color = Colors.white
        p.setPen(color)
        p.setFont(label_font(max(10, int(h * 0.42))))
        text_rect = QRectF(lamp_x + lamp_d + 8, 0, w - lamp_x - lamp_d - 16, h)
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, label)
        p.end()
