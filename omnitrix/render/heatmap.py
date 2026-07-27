"""Bookmap-style resting-liquidity heatmap.

Draws one colour strip per bar column showing the L2 resting size at each price
(from `bar.book`). Normalisation is computed over the *visible* columns each
frame, so the scale auto-adapts as you scroll — fixing the old app's
"max_size only ever grows" wash-out bug.

Colour ramp (low -> high liquidity): deep navy -> blue -> white -> amber -> red.
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QPainter


# One colour language across the app: this overlay and the dedicated Bookmap
# window are both "resting liquidity by price over time", so they share the same
# thermal ramp. Two different blue-to-red scales for the same quantity made the
# two views look like different products.
from .bookmap import _LUT  # noqa: E402  (single source of truth for the ramp)


class HeatmapItem(pg.GraphicsObject):
    def __init__(self, tick: float):
        super().__init__()
        self.bars: list = []
        self.tick = tick
        # Softer than the dedicated Bookmap pane (255): here the field sits
        # BEHIND the footprint clusters and must not compete with them. At full
        # opacity the shared ramp swamped the cells it is meant to contextualise.
        self.alpha = 150
        # Matches BookHeatmapItem, which is tuned against a real Bookmap capture.
        self.gamma = 1.15
        self._bounds = QRectF()
        self.setZValue(-10)        # behind the footprint

    def set_bars(self, bars: list) -> None:
        self.prepareGeometryChange()
        self.bars = bars
        if bars:
            lo = min(b.low for b in bars)
            hi = max(b.high for b in bars)
            self._bounds = QRectF(-1, lo - 1, len(bars) + 2, (hi - lo) + 2)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        if not self.bars:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick

        vb = self.getViewBox()
        if vb is None:
            return
        xr = vb.viewRange()[0]
        x_lo = max(0, int(xr[0]) - 1)
        x_hi = min(len(self.bars), int(xr[1]) + 2)

        # normalise over what's visible
        vmax = 1
        for x in range(x_lo, x_hi):
            bk = self.bars[x].book
            if bk:
                m = max(bk.values())
                if m > vmax:
                    vmax = m
        # Log scale then gamma, identical to BookHeatmapItem. A linear ratio put
        # every ordinary level within a hair of zero on any symbol carrying a
        # real wall, so the field only ever showed the wall.
        denom = math.log1p(vmax) or 1.0

        for x in range(x_lo, x_hi):
            bk = self.bars[x].book
            if not bk:
                continue
            for ti, size in bk.items():
                if size <= 0:
                    continue
                v = min(1.0, math.log1p(size) / denom) ** self.gamma
                idx = 0 if v <= 0 else (255 if v >= 1 else int(v * 255))
                c = _LUT[idx]
                c = QColor(c.red(), c.green(), c.blue(), self.alpha)
                p.fillRect(QRectF(x - 0.5, ti * tick - tick / 2, 1.0, tick), c)
