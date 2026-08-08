"""Time-&-Sales tape: a scrolling list of recent prints, newest on top,
coloured by aggressor and highlighted for large ("block") trades.

Reads the active symbol's `BookmapBuffer.trades` (x, tick_index, size,
aggressor) on a timer — no separate storage needed.
"""

from __future__ import annotations

import time
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QFont
from .framegov import GovernedTimer, watch
from ..render.crosshair import clock_label

BUY = QColor(46, 158, 126)
SELL = QColor(212, 86, 79)
BG = QColor(10, 13, 20)
GRID = QColor(26, 32, 42)
TEXT = QColor(200, 205, 214)


class TapeWidget(QWidget):
    ROW_H = 18

    def __init__(self, get_source, tick_fn, dt_fn=None, block_size: int = 1000,
                 parent=None):
        super().__init__(parent)
        self._get_source = get_source     # () -> deque of (x, ti, size, aggr) | None
        self._tick_fn = tick_fn           # () -> current tick size
        # () -> BookmapBuffer.col_dt. Trade x is stored in *column* units
        # (ts_s / col_dt), so wall-clock time is x * col_dt. Assuming col_dt == 1
        # was correct only by coincidence and would silently skew every printed
        # timestamp the moment the buffer resolution changed.
        self._dt_fn = dt_fn or (lambda: 1.0)
        self.block_size = block_size
        self.setMinimumWidth(220)
        self.font = QFont("Consolas", 9)
        self.header_font = QFont("Consolas", 9, QFont.Weight.Bold)
        # Governed. A bare timer cannot be throttled when the chart is
        # already late, and its cost is invisible to the watchdog - a stall
        # here would be reported as "unmarked", which is the state that made
        # every previous freeze expensive to find. Priority 1: a side panel
        # gives way to the chart being traded from.
        self._timer = GovernedTimer(self, self._tick, 150, priority=1)
        self._timer.start()

    def _tick(self) -> None:
        with watch("tape_widget"):
            self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        w = self.width()
        col_time, col_price, col_size = 8, int(w * 0.42), int(w * 0.72)

        # header
        p.setFont(self.header_font)
        p.setPen(pg.mkPen(QColor(140, 147, 160)))
        p.drawText(col_time, 14, "TIME")
        p.drawText(col_price, 14, "PRICE")
        p.drawText(col_size, 14, "SIZE")
        p.setPen(pg.mkPen(GRID))
        p.drawLine(0, 20, w, 20)

        src = self._get_source()
        if not src:
            return
        tick = self._tick_fn()
        dt = self._dt_fn() or 1.0
        p.setFont(self.font)
        n = max(0, (self.height() - 24) // self.ROW_H)
        # Slice BEFORE materialising. list(src)[-n:] built a tuple for all
        # 60,000 tape entries every repaint and then threw away all but the ~40
        # that fit on screen; the view's slice builds only those.
        rows = src[-n:][::-1] if n else []      # newest first
        y = 24
        for x, ti, size, aggr in rows:
            is_buy = aggr.value == "buy"
            is_block = size >= self.block_size
            base = BUY if is_buy else (SELL if aggr.value == "sell" else QColor(120, 124, 132))
            if is_block:
                c = QColor(base); c.setAlpha(60)
                p.fillRect(QRectF(0, y, w, self.ROW_H), c)
            p.setPen(pg.mkPen(TEXT))
            p.drawText(col_time, y + 13,
                       clock_label(x * dt))
            p.setPen(pg.mkPen(base))
            p.drawText(col_price, y + 13, f"{ti * tick:.2f}")
            p.drawText(col_size, y + 13, f"{size:,}")
            y += self.ROW_H
            if y > self.height():
                break
