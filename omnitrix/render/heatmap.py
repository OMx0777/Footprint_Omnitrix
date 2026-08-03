"""Bookmap-style resting-liquidity heatmap.

Draws one colour strip per bar column showing the L2 resting size at each price
(from `bar.book`). Normalisation is computed over the *visible* columns each
frame, so the scale auto-adapts as you scroll — fixing the old app's
"max_size only ever grows" wash-out bug.

Colour ramp (low -> high liquidity): deep navy -> blue -> white -> amber -> red.
"""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter


# One colour language across the app: this overlay and the dedicated Bookmap
# window are both "resting liquidity by price over time", so they share the same
# thermal ramp. Two different blue-to-red scales for the same quantity made the
# two views look like different products.
#
# _LUT_ARGB is _pack(_LUT), so the packed ramp cannot drift from the QColor one.
from .bookmap import _LUT_ARGB  # noqa: E402  (single source of truth for the ramp)


class HeatmapItem(pg.GraphicsObject):
    MAX_ROWS = 6000        # guard: pathological y-zoom must not allocate wildly

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
        self._buf = None           # kept alive: QImage wraps it, never copies
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
        """Composite the field into one ARGB buffer and blit it once.

        This used to be a Python loop issuing one `fillRect` per price level per
        bar - tens of thousands of Qt calls a frame, the same cost that was
        measured at ~94 ms in the dedicated Bookmap pane before it was
        vectorised. `Bar.book` is a PriceLadder now, which made it strictly
        worse: `items()` boxes both int32 arrays into Python lists on every
        call, so the loop paid 2.1x what the plain dict it replaced did.
        Reading `arrays()` instead keeps the data in numpy end to end.

        Verified pixel-identical to the per-cell renderer across zoom, pan,
        gamma and empty-book cases wherever a ladder row is at least one pixel
        tall. Below that the two differ: blitting resamples rows while the old
        loop drew overlapping sub-pixel rects and let the last one win, so both
        discard rows at extreme y zoom-out - just not the same ones. Neither is
        the more faithful picture; use the Bookmap pane's `row_ticks` if the
        aggregated view is what is wanted.
        """
        if not self.bars:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        tick = self.tick

        xr = vb.viewRange()[0]
        x_lo = max(0, int(xr[0]) - 1)
        x_hi = min(len(self.bars), int(xr[1]) + 2)
        ncols = x_hi - x_lo
        if ncols <= 0:
            return

        # Rows span only the visible price window. The old loop drew every level
        # in the ladder whatever the y-zoom, so most of its rects were clipped
        # away by Qt after being built; bounding the buffer is both cheaper and
        # invisible in the output.
        yr = vb.viewRange()[1]
        ti_lo = int(math.floor(yr[0] / tick)) - 1
        ti_hi = int(math.ceil(yr[1] / tick)) + 1
        nrows = ti_hi - ti_lo + 1
        if nrows <= 0 or nrows > self.MAX_ROWS:
            return

        # Normalise over what is visible, from the ladder's cached max - the
        # scan is per column per frame, and `max(bk.values())` re-boxed the whole
        # size array every time to answer a question the ladder already knows.
        vmax = 1
        tis, szs, xs_of = [], [], []
        for x in range(x_lo, x_hi):
            bk = self.bars[x].book
            if not bk:
                continue
            m = bk.max_size()
            if m > vmax:
                vmax = m
            ti_arr, sz_arr = bk.arrays()
            tis.append(ti_arr)
            szs.append(sz_arr)
            xs_of.append(x - x_lo)
        if not tis:
            return

        counts = np.fromiter((t.size for t in tis), dtype=np.int64,
                             count=len(tis))
        rows = np.concatenate(tis).astype(np.int64) - ti_lo
        vs = np.concatenate(szs).astype(np.float64)
        xs = np.repeat(np.fromiter(xs_of, dtype=np.int64, count=len(xs_of)),
                       counts)

        # One scatter for the whole field. Ladder keys are unique per bar and
        # one row is one tick here, so no two points collide and bincount is a
        # placement, not a sum - the same picture the per-cell loop drew.
        m = (rows >= 0) & (rows < nrows) & (vs > 0)
        if not m.any():
            return
        acc = np.bincount((rows[m] * ncols + xs[m]), weights=vs[m],
                          minlength=nrows * ncols).reshape(nrows, ncols)

        # Log scale then gamma, identical to BookHeatmapItem. A linear ratio put
        # every ordinary level within a hair of zero on any symbol carrying a
        # real wall, so the field only ever showed the wall.
        denom = math.log1p(vmax) or 1.0
        lit = acc > 0
        buf = np.zeros((nrows, ncols), dtype=np.uint32)      # 0 = transparent
        norm = np.clip(np.log1p(acc[lit]) / denom, 0.0, 1.0) ** self.gamma
        idx = (norm * 255.0).astype(np.int32)
        np.clip(idx, 0, 255, out=idx)
        # Alpha comes from `self.alpha`, not the ramp: this field sits BEHIND
        # the footprint clusters and is deliberately softer than the standalone
        # Bookmap pane, which is what the per-cell QColor(..., alpha) did.
        buf[lit] = ((_LUT_ARGB[idx] & np.uint32(0x00FFFFFF))
                    | np.uint32((self.alpha & 0xFF) << 24))

        self._buf = buf                    # QImage does not own the buffer
        img = QImage(buf.data, ncols, nrows, ncols * 4,
                     QImage.Format.Format_ARGB32)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # Row 0 is ti_lo, the lowest price, which is the order rows were filled
        # in. Column 0 is bar x_lo, and a bar's cell spans [x-0.5, x+0.5].
        p.drawImage(QRectF(x_lo - 0.5, ti_lo * tick - tick / 2,
                           ncols, nrows * tick), img)
