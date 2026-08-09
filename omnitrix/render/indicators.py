"""Price-pane overlays: exponential moving averages and the Central Pivot Range.

Both consume engine `Bar` objects. `Bar.start_ts` is the bar's market-time open
in epoch seconds, which is what makes a *real* daily pivot possible — the
previous CPR derived one pivot from whatever happened to be on screen and
labelled it "daily".
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QPainter, QFont, QPainterPath
from .crosshair import clock_label
from ..paintguard import safe_paint


class EMAItem(pg.GraphicsObject):
    """Exponential moving average over bar closes.

    The series is extended incrementally, but only when it is provably the same
    bar list growing at the tail. Switching timeframe hands over a completely
    different list of the same-or-greater length, and the old code happily
    continued a 1-minute EMA onto 5-minute bars.
    """

    def __init__(self, period: int = 9, color: QColor = QColor(255, 193, 7)):
        super().__init__()
        self.period = period
        self.color = color
        self.bars: list = []
        self.emas: list[float] = []
        self._src_id = None          # identity of the list the cache was built from
        self._bounds = QRectF()

    def set_bars(self, bars: list) -> None:
        self.prepareGeometryChange()
        self.bars = bars
        self._calculate()
        if bars:
            lo = min(b.low for b in bars)
            hi = max(b.high for b in bars)
            self._bounds = QRectF(-1, lo - 1.0, len(bars) + 2, (hi - lo) + 2.0)
        else:
            self._bounds = QRectF()
        self.update()

    def _calculate(self) -> None:
        bars = self.bars
        if not bars:
            self.emas = []
            self._src_id = None
            return

        k = 2.0 / (self.period + 1)
        same_source = (self._src_id == id(bars)
                       and 0 < len(self.emas) <= len(bars))
        if same_source:
            start = len(self.emas) - 1          # recompute the last (live) bar
            ema = self.emas[start - 1] if start > 0 else bars[0].close
        else:
            self.emas = []
            start = 0
            ema = bars[0].close

        for i in range(start, len(bars)):
            ema = (bars[i].close - ema) * k + ema
            if i < len(self.emas):
                self.emas[i] = ema
            else:
                self.emas.append(ema)
        del self.emas[len(bars):]               # bars rolled off the front
        self._src_id = id(bars)

    def boundingRect(self) -> QRectF:
        return self._bounds

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        n = len(self.emas)
        if n < 2:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        x_min, x_max = vb.viewRange()[0]
        # Clamp to the series: panning past the last bar used to index straight
        # off the end of `emas` and raise inside paint().
        x_start = max(0, min(n - 1, int(x_min) - 1))
        x_end = max(x_start + 1, min(n, int(x_max) + 2))
        if x_end - x_start < 2:
            return

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(pg.mkPen(self.color, width=2))
        path = QPainterPath()
        path.moveTo(x_start, self.emas[x_start])
        for x in range(x_start + 1, x_end):
            path.lineTo(x, self.emas[x])
        p.drawPath(path)


class CPRItem(pg.GraphicsObject):
    """Central Pivot Range — a genuine daily pivot.

        P  = (H + L + C) / 3        of the PREVIOUS session
        BC = (H + L) / 2
        TC = 2P - BC                (swapped if inverted)

    Bars are grouped into sessions by the local calendar date of `start_ts`, and
    each session's levels are drawn only across that session's own bars. A chart
    holding several days therefore shows a distinct CPR per day, and the current
    day's levels come from yesterday's range — not from the visible window.
    """

    def __init__(self):
        super().__init__()
        self.bars: list = []
        self.spans: list[tuple[int, int, float, float, float]] = []
        self.colors = {"P": QColor(255, 64, 129), "C": QColor(0, 188, 212)}
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._bounds = QRectF()

    def set_bars(self, bars: list) -> None:
        self.prepareGeometryChange()
        self.bars = bars
        self._calculate()
        if bars:
            lo = min(b.low for b in bars)
            hi = max(b.high for b in bars)
            for _, _, pv, tc, bc in self.spans:
                lo = min(lo, bc)
                hi = max(hi, tc)
            self._bounds = QRectF(-1, lo - 1.0, len(bars) + 2, (hi - lo) + 2.0)
        else:
            self._bounds = QRectF()
        self.update()

    def _calculate(self) -> None:
        self.spans = []
        bars = self.bars
        if not bars:
            return

        # (day_key, first_index, last_index, high, low, close) per session
        days: list[list] = []
        for i, b in enumerate(bars):
            key = clock_label(b.start_ts, "%Y-%m-%d")
            if days and days[-1][0] == key:
                d = days[-1]
                d[2] = i
                d[3] = max(d[3], b.high)
                d[4] = min(d[4], b.low)
                d[5] = b.close
            else:
                days.append([key, i, i, b.high, b.low, b.close])

        for n in range(1, len(days)):
            _, i0, i1, *_ = days[n]
            _, _, _, h, l, c = days[n - 1]
            pv = (h + l + c) / 3.0
            bc = (h + l) / 2.0
            tc = 2.0 * pv - bc
            if tc < bc:
                tc, bc = bc, tc
            self.spans.append((i0, i1, pv, tc, bc))

    def boundingRect(self) -> QRectF:
        return self._bounds

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        if not self.spans:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tr = p.transform()
        for i0, i1, pv, tc, bc in self.spans:
            x0, x1 = i0 - 0.5, i1 + 0.5
            band = QColor(self.colors["C"])
            band.setAlpha(38)
            p.fillRect(QRectF(x0, bc, x1 - x0, tc - bc), band)

            p.setPen(pg.mkPen(self.colors["P"], width=2))
            p.drawLine(QPointF(x0, pv), QPointF(x1, pv))
            p.setPen(pg.mkPen(self.colors["C"], width=1,
                              style=Qt.PenStyle.DashLine))
            for y in (tc, bc):
                p.drawLine(QPointF(x0, y), QPointF(x1, y))

            for label, y, col in (("P", pv, self.colors["P"]),
                                  ("TC", tc, self.colors["C"]),
                                  ("BC", bc, self.colors["C"])):
                pt = tr.map(QPointF(x0, y))
                p.save()
                p.resetTransform()
                p.setFont(self.font)
                p.setPen(pg.mkPen(col))
                p.drawText(QPointF(pt.x() + 4, pt.y() - 3), f"{label} {y:,.2f}")
                p.restore()
