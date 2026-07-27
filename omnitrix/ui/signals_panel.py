"""Order-Flow Signals dock — live list of detected block prints, absorption
events and broken liquidity walls for the active symbol.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget

from ..engine import signals

BG = QColor(15, 19, 28)
HEAD = QColor(143, 160, 182)
RULE = QColor(35, 42, 54)
TEXT = QColor(206, 212, 222)
DIM = QColor(140, 148, 162)

KIND_COL = {
    "block": QColor(156, 123, 255),
    "absorption": QColor(255, 179, 0),
    "wall_break": QColor(239, 96, 96),
}
KIND_TAG = {"block": "BLK", "absorption": "ABS", "wall_break": "BRK"}


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.0f}"


class SignalsPanel(QWidget):
    ROW_H = 19

    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app = app_window
        self.setMinimumWidth(250)
        self.f_head = QFont("Consolas", 9, QFont.Weight.Bold)
        self.f_row = QFont("Consolas", 9)
        self._events: list = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(900)

    def _tick(self) -> None:
        app = self.app
        sym = app.active_symbol
        buf = app.bookmaps.get(sym) if sym else None
        if buf:
            try:
                self._events = signals.detect_all(buf, agg=1)[:60]
            except Exception:
                self._events = []
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        w = self.width()

        p.setFont(self.f_head); p.setPen(HEAD)
        p.drawText(8, 16, "TYPE   PRICE      SIZE   TIME")
        p.setPen(RULE); p.drawLine(8, 21, w - 8, 21)

        if not self._events:
            p.setFont(self.f_row); p.setPen(DIM)
            p.drawText(10, 40, "no signals yet…")
            return

        sym = self.app.active_symbol or "QQQ"
        tick = self.app.instruments.tick(sym)
        dt = self.app.bookmaps[sym].col_dt if sym in self.app.bookmaps else 1.0

        p.setFont(self.f_row)
        y = 26
        for ev in self._events:
            col = KIND_COL.get(ev["kind"], TEXT)
            p.setPen(col)
            p.drawText(9, y + 13, KIND_TAG.get(ev["kind"], "?"))
            p.setPen(TEXT)
            p.drawText(52, y + 13, f"{ev['ti'] * tick:,.2f}")
            p.drawText(QRectF(0, y, w - 66, self.ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       _fmt(ev["size"]))
            p.setPen(DIM)
            p.drawText(QRectF(0, y, w - 8, self.ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       time.strftime("%H:%M:%S",
                                     time.localtime(ev["bucket"] * dt)))
            y += self.ROW_H
            if y > self.height():
                break
