"""Market Profile (TPO) and session Volume Profile render items."""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QFont

TPO_FILL = QColor(96, 152, 226)
TPO_VA = QColor(120, 190, 255)
POC_COL = QColor(255, 196, 60)
BUY_COL = QColor(38, 166, 154)
SELL_COL = QColor(239, 83, 80)
HVN_COL = QColor(255, 196, 60, 60)


class TPOItem(pg.GraphicsObject):
    """Market Profile: one block per (price, 30-min bracket) that traded."""

    def __init__(self, tick: float):
        super().__init__()
        self.rows: list = []          # [(tick_index, [bracket ids])]
        self.b0 = 0                   # first bracket id
        self.tick = tick
        self.poc = None
        self.vah = None
        self.val = None
        self.collapsed = True         # True = classic left-packed bell profile
        self._bounds = QRectF()

    def set_collapsed(self, on: bool) -> None:
        self.collapsed = on
        self.set_data(self.rows, self.b0, self.poc, self.vah, self.val)

    def set_data(self, rows, b0, poc, vah, val) -> None:
        self.prepareGeometryChange()
        self.rows, self.b0 = rows, b0
        self.poc, self.vah, self.val = poc, vah, val
        if rows:
            lo = rows[0][0] * self.tick
            hi = rows[-1][0] * self.tick
            if self.collapsed:
                wmax = max(len(bs) for _, bs in rows)
            else:
                wmax = max((max(bs) - self.b0 + 1) for _, bs in rows)
            self._bounds = QRectF(0, lo - self.tick, wmax + 1,
                                  (hi - lo) + 2 * self.tick)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        if not self.rows:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick
        for ti, brackets in self.rows:
            y = ti * tick - tick / 2
            in_va = (self.val is not None and self.val <= ti <= self.vah)
            c = POC_COL if ti == self.poc else (TPO_VA if in_va else TPO_FILL)
            if self.collapsed:
                # classic Market Profile: pack TPOs left so the row length is
                # the time spent at that price -> the familiar bell shape
                for i in range(len(brackets)):
                    p.fillRect(QRectF(i + 0.06, y, 0.88, tick), c)
            else:
                for b in brackets:
                    p.fillRect(QRectF((b - self.b0) + 0.06, y, 0.88, tick), c)


class VolumeProfileItem(pg.GraphicsObject):
    """Horizontal session volume-at-price, split buy (right) / sell (left)."""

    def __init__(self, tick: float):
        super().__init__()
        self.buy: dict[int, int] = {}
        self.sell: dict[int, int] = {}
        self.tick = tick
        self.poc = None
        self.vah = None
        self.val = None
        self.hvn: list[int] = []
        self._bounds = QRectF()
        self._max = 1

    def set_data(self, buy, sell, poc, vah, val, hvn) -> None:
        self.prepareGeometryChange()
        self.buy, self.sell = buy, sell
        self.poc, self.vah, self.val, self.hvn = poc, vah, val, hvn
        tis = set(buy) | set(sell)
        if tis:
            lo, hi = min(tis) * self.tick, max(tis) * self.tick
            self._max = max((buy.get(t, 0) + sell.get(t, 0)) for t in tis) or 1
            self._bounds = QRectF(0, lo - self.tick, 1.0, (hi - lo) + 2 * self.tick)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        tis = set(self.buy) | set(self.sell)
        if not tis:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick
        mx = self._max
        for ti in tis:
            b = self.buy.get(ti, 0)
            s = self.sell.get(ti, 0)
            tot = b + s
            if tot == 0:
                continue
            y = ti * tick - tick / 2
            if ti in self.hvn:
                p.fillRect(QRectF(0, y, 1.0, tick), HVN_COL)
            ws = (s / mx)
            wb = (b / mx)
            p.fillRect(QRectF(0, y, ws, tick), SELL_COL)
            p.fillRect(QRectF(ws, y, wb, tick), BUY_COL)
            if ti == self.poc:
                p.setPen(pg.mkPen(POC_COL, width=2))
                p.drawLine(QPointF(0, ti * tick), QPointF(1.0, ti * tick))
