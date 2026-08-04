"""Your own fills, drawn on the price chart as hollow circles.

Green = a buy (position increased), red = a sell (position decreased). Hollow
rather than filled on purpose: these sit on top of the footprint, and a solid
disc would hide the very cells you placed the trade against.

Radius grows with the square ROOT of size, so area is proportional to shares -
the same convention as the tape bubbles, and the one that makes a 10,000-share
fill look four times a 625-share one instead of sixteen times.

WHAT THESE MARKERS ARE, precisely. The feed carries no execution report; it
carries your position size, and a change in it is a fill (see
`engine.model.Execution`). So several fills inside one snapshot interval land
as ONE marker carrying their net size, two fills that cancel are invisible, and
the price is the snapshot's last trade rather than the actual fill price. Good
enough to see where you traded; not a blotter.
"""

from __future__ import annotations

import bisect
import math

import pyqtgraph as pg
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QFont

BUY_RING = QColor(126, 217, 87)        # position increased
SELL_RING = QColor(240, 78, 78)        # position decreased
LABEL = QColor(214, 220, 230)


class ExecutionMarkersItem(pg.GraphicsObject):
    """Fill markers positioned by BAR INDEX, like everything else on this pane.

    Executions arrive with a timestamp, but the chart's x axis is the bar's
    ordinal position, so each one is bisected into the current bar list. That
    lookup has to be redone whenever the timeframe changes - the same fill sits
    at a different index on a 1-minute chart than on a 5-minute one.
    """

    def __init__(self):
        super().__init__()
        self.bars: list = []
        self.execs: list = []          # [Execution] ascending ts
        self.min_r = 5.0
        self.max_r = 22.0
        self.size_scale = 1.0
        self.show_labels = True
        self.font = QFont("Consolas", 7, QFont.Weight.Bold)
        self._bounds = QRectF()
        self._starts: list = []        # bar start_ts, for bisect
        self.setZValue(40)             # above the footprint, below drawings

    def set_data(self, bars: list, execs: list) -> None:
        self.prepareGeometryChange()
        self.bars = bars
        self.execs = execs
        self._starts = [b.start_ts for b in bars]
        if bars:
            lo = min(b.low for b in bars)
            hi = max(b.high for b in bars)
            self._bounds = QRectF(-1, lo - 1.0, len(bars) + 2, (hi - lo) + 2.0)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def _x_for(self, ts_ms: int) -> float | None:
        """Bar index for a fill, or None if it predates the loaded history."""
        if not self._starts:
            return None
        i = bisect.bisect_right(self._starts, ts_ms // 1000) - 1
        if i < 0:
            return None
        return float(i)

    def paint(self, p: QPainter, *args) -> None:
        if not self.execs or not self.bars:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        (x_lo, x_hi), (y_lo, y_hi) = vb.viewRange()

        vis = []
        smax = 1
        for ex in self.execs:
            x = self._x_for(ex.ts_ms)
            if x is None or not (x_lo - 1 <= x <= x_hi + 1):
                continue
            if not (y_lo <= ex.price <= y_hi):
                continue
            vis.append((x, ex))
            if ex.size > smax:
                smax = ex.size
        if not vis:
            return

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tr = p.transform()
        p.resetTransform()
        rmin = self.min_r * self.size_scale
        rmax = self.max_r * self.size_scale
        for x, ex in vis:
            pt = tr.map(QPointF(x, ex.price))
            r = rmin + (rmax - rmin) * math.sqrt(ex.size / smax)
            colour = BUY_RING if ex.is_buy else SELL_RING
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(colour, 2.0))
            p.drawEllipse(pt, r, r)
        if self.show_labels:
            p.setFont(self.font)
            for x, ex in vis:
                pt = tr.map(QPointF(x, ex.price))
                r = rmin + (rmax - rmin) * math.sqrt(ex.size / smax)
                p.setPen(pg.mkPen(BUY_RING if ex.is_buy else SELL_RING))
                p.drawText(QPointF(pt.x() + r + 3, pt.y() + 3),
                           f"{'+' if ex.is_buy else '-'}{ex.size:,}")
