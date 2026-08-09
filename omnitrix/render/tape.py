"""Rendering items for the tape-reader window.

Aimed at high-speed scalping, which asks a different question from the bookmap.
The bookmap answers "where is the liquidity"; the tape answers "what is hitting
it, right now, and how fast". So nothing here aggregates by default: every print
is its own mark, at its own price and its own instant, because the *sequence* is
the signal — a run of 12 lifts at the ask reads completely differently from one
block of the same total size.

  TapePrintsItem  — one dot per print, sized by volume, coloured by aggressor,
                    with large prints ringed so blocks pop out of the stream.
  TapeSpeedItem   — prints per interval: the speed of tape.
  TapeCvdItem     — running cumulative delta over the visible window.

x axis = seconds (float, epoch-relative), shared by all three.
"""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont

from .bookmap import BUY_BUBBLE, SELL_BUBBLE
from PyQt6.QtGui import QColor as _QColor
from ..paintguard import safe_paint

# A print that could not be classified is neither a lift nor a hit. Drawing
# it in either colour asserts a direction the data does not contain, so it
# gets its own neutral grey - the same choice the time-and-sales dock makes.
UNKNOWN_BUBBLE = _QColor(150, 156, 168)

BLOCK_RING = QColor(255, 214, 92)          # ring around institutional-size prints
TAPE_BG = "#0E1319"


class _TapeBase(pg.GraphicsObject):
    """Shared window handling: a flat list of prints and an x window."""

    def __init__(self):
        super().__init__()
        self.prints: list = []        # [(t_s, price, size, is_buy)] ascending t
        self._bounds = QRectF()
        self._key = None              # identity of the data the cache belongs to
        self._cache: dict = {}

    def set_prints(self, prints: list, lo: float, hi: float) -> None:
        self.prepareGeometryChange()
        self.prints = prints
        # One cache generation per data update. Bounds and paint both want the
        # same derived series over the same window, and each item was walking
        # the window twice per frame - with three items that is six passes over
        # tens of thousands of prints for one screen.
        self._key = (id(prints), len(prints))
        self._cache = {}
        self._bounds = self._compute_bounds(lo, hi)
        self.update()

    def _memo(self, name: str, build):
        """Cache a derived series for the current `prints` generation.

        Deliberately NOT keyed on the view range. `prints` has already been
        trimmed to the visible window by the caller, so the series is a pure
        function of the data - and keying on lo/hi meant `set_prints` and
        `paint` computed the same thing under two keys that differed in the
        last float digit, so every series was built three times per frame
        instead of once.
        """
        hit = self._cache.get(name)
        if hit is None:
            hit = self._cache[name] = build()
        return hit

    def _compute_bounds(self, lo: float, hi: float) -> QRectF:
        if not self.prints:
            return QRectF()
        y0 = y1 = self.prints[0][1]
        for q in self.prints:
            v = q[1]
            if v < y0: y0 = v
            elif v > y1: y1 = v
        pad = max((y1 - y0) * 0.08, 1e-6)
        return QRectF(lo, y0 - pad, max(hi - lo, 1e-6), (y1 - y0) + 2 * pad)

    def boundingRect(self) -> QRectF:
        return self._bounds


class TapePrintsItem(_TapeBase):
    """Every print as a dot: x = time, y = price, area ∝ size."""

    def __init__(self):
        super().__init__()
        self.min_r = 2.0
        self.max_r = 18.0
        self.size_scale = 1.0
        self.block_size = 5000       # ring prints at or above this
        # Hard cap per frame. Each dot is a separate drawEllipse, so this is the
        # single biggest lever on frame time; beyond roughly this many the
        # circles overlap into a solid band and carry no extra information.
        self.max_dots = 1000
        self.setZValue(0)

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        if not self.prints:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        (x_lo, x_hi), _ = vb.viewRange()
        vis = [q for q in self.prints if x_lo <= q[0] <= x_hi]
        if not vis:
            return
        # Newest wins when the tape is faster than the pixels available.
        if len(vis) > self.max_dots:
            vis = vis[-self.max_dots:]

        smax = max(q[2] for q in vis) or 1
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tr = p.transform()
        p.resetTransform()
        rmin = self.min_r * self.size_scale
        rmax = self.max_r * self.size_scale
        block = self.block_size
        for t, price, size, buy in vis:
            pt = tr.map(QPointF(t, price))
            r = rmin + (rmax - rmin) * math.sqrt(size / smax)
            # Three states, not two. `buy` is the print's buy SHARE, so an
            # unclassifiable print splits evenly and gets a neutral dot instead
            # of being drawn as a green lift it never was.
            sell = size - buy
            base = (BUY_BUBBLE if buy > sell
                    else SELL_BUBBLE if sell > buy else UNKNOWN_BUBBLE)
            p.setBrush(QBrush(QColor(base.red(), base.green(), base.blue(), 225)))
            if size >= block:
                # A block trade is the one print a scalper must not miss in a
                # fast tape, so it gets an outline rather than just more area -
                # size alone is not separable at a glance mid-flow.
                p.setPen(QPen(BLOCK_RING, 1.6))
            else:
                p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(pt, r, r)


class TapeSpeedItem(_TapeBase):
    """Prints per bucket — how fast the tape is actually running."""

    def __init__(self, bucket_s: float = 1.0):
        super().__init__()
        self.bucket_s = bucket_s
        self.font = QFont("Consolas", 7, QFont.Weight.Bold)
        self.setZValue(0)

    def _compute_bounds(self, lo: float, hi: float) -> QRectF:
        counts = self._counts(lo, hi)
        mx = max(counts.values(), default=1) or 1
        return QRectF(lo, 0, max(hi - lo, 1e-6), mx * 1.15)

    def _counts(self, lo: float = 0.0, hi: float = 0.0) -> dict:
        return self._memo("counts", self._build_counts)

    def _build_counts(self) -> dict:
        b = max(self.bucket_s, 1e-6)
        out: dict[int, int] = {}
        get = out.get
        for t, _price, _size, _buy in self.prints:
            k = int(t // b)
            out[k] = get(k, 0) + 1
            get = out.get
        return out

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        if not self.prints:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        (x_lo, x_hi), _ = vb.viewRange()
        counts = self._counts(x_lo, x_hi)
        if not counts:
            return
        b = max(self.bucket_s, 1e-6)
        mx = max(counts.values()) or 1
        # Hot bars where the tape is running well above its own recent norm -
        # acceleration is the tradable event, not the absolute rate.
        for k, n in counts.items():
            frac = n / mx
            c = QColor(70, 130, 180) if frac < 0.66 else QColor(255, 170, 40)
            p.fillRect(QRectF(k * b + b * 0.12, 0, b * 0.76, n), c)


class TapeCvdItem(_TapeBase):
    """Running cumulative delta across the visible window."""

    def __init__(self):
        super().__init__()
        self.setZValue(0)

    def _compute_bounds(self, lo: float, hi: float) -> QRectF:
        xs, ys = self._curve(lo, hi)
        if not ys:
            return QRectF(lo, -1, max(hi - lo, 1e-6), 2)
        y0, y1 = min(ys), max(ys)
        pad = max(abs(y1 - y0) * 0.15, 1.0)
        return QRectF(lo, y0 - pad, max(hi - lo, 1e-6), (y1 - y0) + 2 * pad)

    def _curve(self, lo: float = 0.0, hi: float = 0.0):
        return self._memo("curve", self._build_curve)

    def _build_curve(self):
        xs, ys, acc = [], [], 0
        xa, ya = xs.append, ys.append
        # buy - sell, where sell = size - buy. Was `+size if is_buy else -size`,
        # which added the FULL size of every unclassified print to the buy side
        # and made the curve drift upward all session.
        for t, _price, size, buy in self.prints:
            acc += 2 * buy - size
            xa(t)
            ya(acc)
        return xs, ys

    MAX_POINTS = 1200

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        if not self.prints:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        (x_lo, x_hi), _ = vb.viewRange()
        xs, ys = self._curve(x_lo, x_hi)
        if len(xs) < 2:
            return

        # Decimate to screen resolution and draw POLYLINES, not segments.
        #
        # One drawLine per print meant 36,000 painter calls at a 10-minute span,
        # which alone took the window from 60 ms to ~1 s. The curve cannot show
        # more than one point per pixel column anyway, so keeping every print is
        # detail that is thrown away by the rasteriser after being paid for.
        # The running total is preserved exactly at each kept sample - only the
        # intermediate steps between two adjacent pixels are dropped.
        n = len(xs)
        step = 1 if n <= self.MAX_POINTS else n // self.MAX_POINTS
        if step > 1:
            # Always keep the last point so the curve ends on the true total.
            idx = list(range(0, n, step))
            if idx[-1] != n - 1:
                idx.append(n - 1)
            xs = [xs[i] for i in idx]
            ys = [ys[i] for i in idx]

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Colour by sign so a flip is visible without reading the axis: emit one
        # polyline per contiguous run of the same sign (a handful, typically).
        pen_up = pg.mkPen(BUY_BUBBLE, width=2)
        pen_dn = pg.mkPen(SELL_BUBBLE, width=2)
        run = [QPointF(xs[0], ys[0])]
        sign = ys[0] >= 0
        for x, y in zip(xs[1:], ys[1:]):
            s = y >= 0
            run.append(QPointF(x, y))
            if s != sign:
                p.setPen(pen_up if sign else pen_dn)
                p.drawPolyline(*run)
                run = [QPointF(x, y)]
                sign = s
        if len(run) > 1:
            p.setPen(pen_up if sign else pen_dn)
            p.drawPolyline(*run)
