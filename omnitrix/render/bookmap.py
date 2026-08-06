"""Bookmap render items, driven by a `BookmapBuffer`:

    BookHeatmapItem  — resting-liquidity field (time × price), auto-normalised.
    BBOItem          — stepped best-bid (blue) / best-ask (red) lines.
    BubbleItem       — trade bubbles, radius ∝ √size, red=sell / pale=buy.
    DomLadderItem    — right-edge depth histogram of the latest book + numbers.
    VolumeBarsItem   — bottom per-column executed-volume bars + numbers.

x = absolute column bucket (from the buffer), y = price.
"""

from __future__ import annotations

import bisect
from itertools import chain

import math
import numpy as np
import pyqtgraph as pg

from ..engine.model import split_size, split_sizes
from ..engine.bookmap import _AG_FROM
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import (QColor, QPainter, QFont, QPen, QBrush, QRadialGradient,
                         QImage, QPixmap)

# Sampled from a real Bookmap ESU6:CME capture, not chosen by eye.
#
# Executions are shaded spheres with a highlight, NOT flat discs — measured by
# taking a radial cut through a bubble: luminance runs 30 -> 150 -> 92 across
# the diameter, which a flat fill cannot produce. Colours are the measured
# green/red pair.
BUY_BUBBLE = QColor(54, 179, 109)      # #36B36D — lifted the ask
SELL_BUBBLE = QColor(242, 82, 66)      # #F25242 — hit the bid
# Best bid / best ask trace the same aggression palette, a little deeper so the
# stepped channel reads under the dots rather than competing with them.
BID_LINE = QColor(47, 168, 95)
ASK_LINE = QColor(224, 74, 60)

# The empty book, DARKENED from the sampled value on request.
#
# The capture's own empty book is #1A2226 — a very dark blue-grey, sampled from
# several genuinely empty regions, with the quantised mode of the whole field
# agreeing at #181824. That is recorded here because it is measured and the
# measurement should not be lost; what is drawn is one step darker so the
# bookmap sits at the same black as the rest of the terminal instead of
# floating a paler panel inside it.
#
# It is still not pure black: that made the uncovered area read as a hole
# punched in the chart, which was the original finding and has not changed.
#
# THIS AND THE LUT'S 0.00 STOP MUST STAY EQUAL. The heat field is drawn as an
# image over the background, so if its zero-liquidity colour differs from the
# background by even a little, the field's extent shows up as a rectangle.
BOOKMAP_BG = "#0A0D14"


def _build_bookmap_lut() -> list[QColor]:
    """Bookmap's measured thermal ramp.

    Two things the eyeballed version got wrong, both corrected from the capture:

      * **There is no green in the field.** A hue census over the heat area
        returns blue 54%, red 4.6%, yellow/orange 4.0%, white 1.6% and green
        0.8% — and that 0.8% is the trade dots, not liquidity.
      * **The top of the ramp is RED (252,12,0), not white.** White sits in the
        upper-middle, *below* yellow: the measured wall bands run
        #E7EBE5 -> #FFFB00 -> #FFA500 -> #FF2900 as size rises.

    The blues are cyan-toned, not royal: the red channel is ~0 throughout while
    green climbs 36->180 and blue 48->216, so the field is an azure ramp. Over
    half the ramp's length is blue because that is where the ordinary book
    actually lives (measured mode: rgb(0,96,132)..rgb(0,120,180)).
    """
    stops = [
        (0.00, (10, 13, 20)),      # #0A0D14 — empty book (== BOOKMAP_BG)
        (0.08, (12, 36, 48)),      # faintest resting size
        (0.18, (12, 48, 60)),
        (0.28, (12, 60, 84)),
        (0.38, (0, 84, 120)),      # ordinary book — the field's mode
        (0.48, (0, 96, 144)),
        (0.58, (0, 120, 180)),
        (0.66, (0, 132, 192)),
        (0.72, (48, 156, 204)),    # building
        (0.78, (96, 180, 216)),
        (0.83, (180, 216, 228)),   # pale
        (0.87, (228, 228, 228)),   # white
        (0.91, (240, 240, 84)),    # yellow
        (0.95, (252, 108, 0)),     # orange
        (1.00, (252, 12, 0)),      # red — the heaviest wall
    ]
    lut: list[QColor] = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(stops) - 1):
            t0, c0 = stops[j]
            t1, c1 = stops[j + 1]
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                lut.append(QColor(
                    int(c0[0] + (c1[0] - c0[0]) * f),
                    int(c0[1] + (c1[1] - c0[1]) * f),
                    int(c0[2] + (c1[2] - c0[2]) * f)))
                break
    return lut


_LUT = _build_bookmap_lut()


def _pack(lut: list) -> np.ndarray:
    """Pack a 256-colour ramp as opaque 0xAARRGGBB for image compositing."""
    return np.array(
        [(0xFF000000 | (c.red() << 16) | (c.green() << 8) | c.blue())
         for c in lut], dtype=np.uint32)


def _ramp(stops) -> list:
    """Linear-interpolate (position, rgb) stops into a 256-entry ramp."""
    out: list[QColor] = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(stops) - 1):
            t0, c0 = stops[j]
            t1, c1 = stops[j + 1]
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                out.append(QColor(int(c0[0] + (c1[0] - c0[0]) * f),
                                  int(c0[1] + (c1[1] - c0[1]) * f),
                                  int(c0[2] + (c1[2] - c0[2]) * f)))
                break
    return out


# Same ramp packed as opaque 0xAARRGGBB, for compositing the field as an image.
_LUT_ARGB = _pack(_LUT)

# Alternative looks. "Bookmap" is the measured reference and stays the default;
# the rest are conveniences, not claims about any other product. Each entry is
# (packed LUT, background). Light backgrounds invert the ramp's dark end so an
# empty book matches the canvas rather than punching black holes in it.
LOOK_LUTS: dict[str, tuple] = {
    "Bookmap": (_LUT_ARGB, BOOKMAP_BG),
    "Ice": (_pack(_ramp([
        (0.00, (16, 20, 28)), (0.20, (18, 44, 74)), (0.45, (24, 88, 140)),
        (0.65, (54, 148, 196)), (0.80, (140, 206, 230)), (0.90, (232, 240, 246)),
        (1.00, (255, 255, 255))])), "#10141C"),
    "Fire": (_pack(_ramp([
        (0.00, (18, 14, 12)), (0.22, (60, 18, 8)), (0.45, (128, 40, 6)),
        (0.66, (206, 88, 8)), (0.82, (242, 158, 22)), (0.92, (250, 216, 96)),
        (1.00, (255, 255, 236))])), "#12100E"),
    "Mono": (_pack(_ramp([
        (0.00, (18, 20, 22)), (0.35, (70, 74, 80)), (0.65, (132, 138, 146)),
        (0.85, (196, 200, 206)), (1.00, (255, 255, 255))])), "#121416"),
    "Light": (_pack(_ramp([
        (0.00, (245, 246, 248)), (0.15, (206, 222, 238)), (0.35, (150, 190, 224)),
        (0.55, (86, 152, 206)), (0.72, (40, 110, 178)), (0.85, (232, 150, 30)),
        (0.94, (226, 92, 20)), (1.00, (198, 24, 24))])), "#F5F6F8"),
}


# Bounded, NOT a single slot.
#
# One entry per (column list, tick), so every render item in a window shares a
# hit. It used to hold exactly one result, which was fine for one Bookmap and
# catastrophic for two: each window's items evicted the other's key on every
# refresh, so the memo never hit again and all eight items fell back to a full
# scan of up to 14,400 columns. Measured 3.2 ms for one window and 61.8 ms for
# two - a 19x jump for a second window, which is what "it lags when I open 3-4
# bookmaps" actually was.
#
# 32 is far more than any plausible number of open windows x items; the cap
# only exists so a long-lived process cannot accumulate stale keys.
_PB_MAX = 32
_pb_memo: dict = {}

# (radius_px, colour) -> pre-rendered sphere. See BubbleItem._sphere_sprite.
_SPHERE_CACHE: dict = {}


def _price_bounds(cols, tick):
    # A refresh hands the *same* column list to every item, so without a memo
    # this full-buffer scan runs once per item per frame.
    last = cols[-1]
    key = (id(cols), len(cols), cols[0].bucket, last.bucket, id(last.book), tick)
    hit = _pb_memo.get(key)
    if hit is not None:
        return hit

    # Hot path: runs for every item on every refresh over the whole buffer.
    # min()/max() on the dict is a C-level scan instead of a Python loop, and
    # consecutive columns share one forward-filled book object, so an identity
    # check skips the repeats without changing the result.
    lo = hi = None
    seen = None
    for c in cols:
        bk = c.book
        if not bk or bk is seen:
            continue
        seen = bk
        a = min(bk)
        b = max(bk)
        if lo is None:
            lo, hi = a, b
        else:
            if a < lo:
                lo = a
            if b > hi:
                hi = b
    if lo is None:
        rect = QRectF()
    else:
        x0 = cols[0].bucket
        x1 = cols[-1].bucket
        rect = QRectF(x0 - 1, lo * tick, (x1 - x0) + 3, (hi - lo) * tick + tick)
    if len(_pb_memo) >= _PB_MAX:
        # Cheap eviction: these keys die as soon as their column list is
        # rebuilt, so anything still here is either live or already garbage.
        _pb_memo.clear()
    _pb_memo[key] = rect
    return rect


class _BucketView:
    """Read-only `.bucket` projection of a column list, so bisect can search it
    without materialising a parallel list of keys every frame."""

    __slots__ = ("_c",)

    def __init__(self, cols):
        self._c = cols

    def __len__(self):
        return len(self._c)

    def __getitem__(self, i):
        return self._c[i].bucket


class _BufItem(pg.GraphicsObject):
    """Base: holds a column list + tick and a cached bounding rect."""

    def __init__(self, tick: float):
        super().__init__()
        self.cols: list = []
        self.tick = tick
        self._bounds = QRectF()

    def set_cols(self, cols: list) -> None:
        # Qt caches boundingRect() in the scene index. Changing it without
        # prepareGeometryChange() leaves the stale rect in place, so the item
        # gets culled at random - which is exactly the "bubbles/pies flicker in
        # and out while the heatmap stays" symptom.
        self.prepareGeometryChange()
        self.cols = cols
        self._bounds = _price_bounds(cols, self.tick) if cols else QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def _xrange(self):
        """The visible x span, GUARANTEED FINITE.

        A panned or zoomed viewport hands back inf and nan - this is the same
        class of value that has bitten the time axes four times over (see
        tests/clock_guard.py). Here it reached math.floor(), which raises
        OverflowError on inf and ValueError on nan, from inside paint().

        That is not a cosmetic failure. PyQt6 routes an exception out of
        paint() to qFatal unless the excepthook in app.py catches it, and even
        caught, it raises on every subsequent frame - so the chart stops
        updating and the terminal looks hung while the process is alive.

        Clamped rather than dropped: a broken viewport should still draw the
        data that exists, and the caller's own `x_hi <= x_lo` test handles the
        genuinely empty case.
        """
        vb = self.getViewBox()
        if vb is None or not self.cols:
            return 0, 0
        xr = vb.viewRange()[0]
        lo, hi = xr[0] - 1, xr[1] + 1
        if lo != lo or hi != hi:              # nan: no meaningful clamp
            return 0, 0
        if not (-_X_LIMIT <= lo <= _X_LIMIT):
            lo = -_X_LIMIT if lo < 0 else _X_LIMIT
        if not (-_X_LIMIT <= hi <= _X_LIMIT):
            hi = -_X_LIMIT if hi < 0 else _X_LIMIT
        return lo, hi

    def _visible(self):
        """The columns actually on screen, found by BISECTION.

        `cols` is the whole ring - up to 14,400 columns for one symbol at a
        4-hour depth - while a normal view shows about 60 of them. Scanning the
        list and testing each bucket cost the full 14,400 every frame, per item,
        per book: with four books open that was ~115,000 wasted iterations a
        frame before a single pixel was drawn, and it got worse the longer the
        session ran, which is exactly the shape of a slow leak.

        Buckets increase monotonically (they are time), so the visible span is a
        contiguous slice and bisect finds it in ~14 comparisons.
        """
        x_lo, x_hi = self._xrange()
        cols = self.cols
        if not cols or x_hi <= x_lo:
            return (), 0.0, 0.0
        lo = bisect.bisect_left(_BucketView(cols), x_lo)
        hi = bisect.bisect_right(_BucketView(cols), x_hi)
        return cols[lo:hi], x_lo, x_hi


class BookHeatmapItem(_BufItem):
    """The resting-liquidity field, composited as a single image.

    Every column carries a full forward-filled ladder, so a per-cell fillRect
    means tens of thousands of Qt calls per frame (measured: ~100 ms at normal
    zoom, ~800 ms zoomed out to full history). Building one ARGB buffer with
    numpy and blitting it once is the same picture for a fraction of the cost,
    and it is how a real depth heatmap is drawn.
    """

    MAX_ROWS = 6000        # guard: pathological y-zoom must not allocate wildly

    def __init__(self, tick: float):
        super().__init__(tick)
        self.alpha = 255
        # Contrast exponent applied AFTER the log normalise. Tuned against the
        # reference capture rather than by eye: there the ordinary book sits at
        # luminance 68-99, i.e. ~0.45-0.55 of this ramp, and is plainly blue —
        # NOT pushed down into the floor. log1p alone puts a typical level near
        # 0.64, so only a mild correction is wanted. (1.8 was far too strong and
        # crushed the routine book to near-black.)
        self.gamma = 1.15
        # Fade columns that carry a forward-filled ladder but received no sweep
        # of their own. Without this a gap in sampling and a period of genuinely
        # stable liquidity render identically, so the field asserts things it
        # never measured. Alpha is deliberately high enough that a faded band
        # still reads as continuous liquidity - the point is to distinguish
        # observed from inferred, not to punch holes in the chart.
        self.dim_unobserved = True
        self.unobserved_alpha = 96
        # Price aggregation: how many ticks collapse into one drawn row.
        # At 1 tick on a penny-quoted name every cent gets its own 1-pixel line,
        # and zoomed out those lines overlap into mush. Bucketing to 10c or $1
        # makes each band thick enough to read, and sizes are SUMMED within a
        # bucket so a wall spread across several cents shows its true weight.
        self.row_ticks = 1
        # Live gradient: fade older columns so the field is dominated by current
        # liquidity. 0 = off (every column equal, the classic view); 1 = only the
        # newest columns carry full intensity. For scalpers who want the magnets
        # that matter NOW rather than an even history.
        self.recency = 0.0
        self.min_alpha = 40
        self.lut = _LUT_ARGB          # swappable colour ramp (see LOOK_LUTS)
        self._buf = None   # kept alive: QImage wraps this memory, never copies
        self.setZValue(-20)

    def paint(self, p: QPainter, *args) -> None:
        if not self.cols:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        tick = self.tick
        # Bisect to the on-screen span first: this list comprehension used to
        # test every column in the ring - up to 14,400 - to keep the ~60 that
        # are visible, on every frame, for every book.
        span, x_lo, x_hi = self._visible()
        vis = [c for c in span if c.book]
        if not vis:
            return

        # Rows span only the visible price window, in units of `row_ticks` so a
        # coarser price grid draws fewer, thicker bands.
        rt = max(1, int(self.row_ticks))
        yr = vb.viewRange()[1]
        ti_lo = int(math.floor(yr[0] / tick)) - 1
        ti_hi = int(math.ceil(yr[1] / tick)) + 1
        # Floor-divide so bucket edges are absolute and do not slide as the view
        # scrolls - otherwise a wall would shimmer between adjacent bands.
        r_lo = ti_lo // rt
        nrows = (ti_hi // rt) - r_lo + 1
        if nrows <= 0 or nrows > self.MAX_ROWS:
            return
        b0, b1 = vis[0].bucket, vis[-1].bucket
        ncols = b1 - b0 + 1
        if ncols <= 0:
            return

        # A resting order stays on the ladder until a later sweep replaces it,
        # so its band must be unbroken across time. The buffer only forward-fills
        # when a column is *created*, which means any second that received no
        # trade and no sweep has no column at all — and the field rendered as
        # vertical stripes with black gutters between them, nothing like the
        # continuous heat field of the real product. Carry the last known ladder
        # forward over those gaps by collecting runs of identical book identity.
        by_bucket = {c.bucket: c for c in vis}
        runs: list[tuple[object, int, int]] = []
        run_book = None
        run_start = 0
        for x in range(ncols):
            c = by_bucket.get(b0 + x)
            if c is not None and c.book is not run_book:
                if run_book is not None and x > run_start:
                    runs.append((run_book, run_start, x))
                run_book, run_start = c.book, x
        if run_book is not None and ncols > run_start:
            runs.append((run_book, run_start, ncols))
        runs = [r for r in runs if len(r[0])]
        if not runs:
            return

        # ONE scatter for the whole field, not one numpy round-trip per column.
        #
        # At the live sweep rate no two consecutive columns share a ladder, so
        # the run loop degenerated to a call per column: 1,400 `np.fromiter`
        # pairs plus 1,400 small log/pow/LUT passes per frame, measured at 94 ms
        # against an 80 ms timer. Concatenating first turns that into a handful
        # of vectorised passes over the same points.
        #
        # Each run is painted into its FIRST column here; widening to the rest
        # of the run is a memory copy below, which is far cheaper than putting
        # the tiling into the scatter.
        tis = [r[0].ti for r in runs]
        szs = [r[0].sz for r in runs]
        counts = np.fromiter((t.size for t in tis), dtype=np.int64,
                             count=len(tis))
        rows = (np.concatenate(tis).astype(np.int64) // rt) - r_lo
        vs = np.concatenate(szs).astype(np.float64)
        xs = np.repeat(np.fromiter((r[1] for r in runs), dtype=np.int64,
                                   count=len(runs)), counts)

        # Accumulate SIZE first, colourise after.
        #
        # With row_ticks > 1 several ladder levels land in one drawn row, and a
        # bucket has to report their SUM - a $1 band holding 100 cents of 500
        # lots is a 50,000-lot wall and must read as one. That also means vmax
        # is a property of the aggregated field, not of any single level, so it
        # cannot be taken from the per-ladder cached max any more.
        # bincount, not np.add.at: same result, roughly two orders faster.
        m = (rows >= 0) & (rows < nrows) & (vs > 0)
        acc = np.bincount((rows[m] * ncols + xs[m]), weights=vs[m],
                          minlength=nrows * ncols).reshape(nrows, ncols)

        # Widen each run across the columns it covers (the forward-fill), while
        # it is still size rather than colour - one copy either way.
        for _, x0, x1 in runs:
            if x1 - x0 > 1:
                acc[:, x0 + 1:x1] = acc[:, x0:x0 + 1]

        vmax = float(acc.max()) or 1.0
        # Logarithmic scale (as real Bookmap): ordinary resting size reads as
        # quiet navy texture while walls saturate to white/amber/red.
        denom = math.log1p(vmax) or 1.0
        lit = acc > 0
        buf = np.zeros((nrows, ncols), dtype=np.uint32)   # 0 = transparent
        if lit.any():
            norm = np.clip(np.log1p(acc[lit]) / denom, 0.0, 1.0) ** self.gamma
            idx = (norm * 255.0).astype(np.int32)
            np.clip(idx, 0, 255, out=idx)
            buf[lit] = self.lut[idx]

        # Live gradient: weight the field toward NOW.
        #
        # A scalper hunting magnets cares about the book in front of him, not an
        # even-weighted history. Scaling alpha along x makes current liquidity
        # dominate while older structure stays visible as context, so the same
        # chart serves both readings without changing the colour language.
        if self.recency > 0.0 and ncols > 1:
            ramp = np.linspace(0.0, 1.0, ncols) ** (1.0 + 3.0 * self.recency)
            a_lo = self.min_alpha / 255.0
            fac = a_lo + (1.0 - a_lo) * ramp
            alpha = (buf >> 24).astype(np.float64) * fac[None, :]
            buf = ((buf & np.uint32(0x00FFFFFF))
                   | (alpha.astype(np.uint32) << 24))
            buf[~lit] = 0

        # Mark what was actually measured. A column absent from `vis` entirely
        # (no trade and no sweep in that second, so no Column was ever created)
        # is unobserved too, and stays False here by construction.
        if self.dim_unobserved:
            seen = np.zeros(ncols, dtype=bool)
            for c in vis:
                if c.sweeps:
                    i = c.bucket - b0
                    if 0 <= i < ncols:
                        seen[i] = True
            if not seen.all():
                gap = ~seen
                sub = buf[:, gap]
                lit = sub != 0                 # leave empty cells transparent
                if lit.any():
                    faded = ((sub & np.uint32(0x00FFFFFF))
                             | np.uint32(self.unobserved_alpha << 24))
                    buf[:, gap] = np.where(lit, faded, sub)

        self._buf = buf                    # QImage does not own the buffer
        img = QImage(buf.data, ncols, nrows, ncols * 4,
                     QImage.Format.Format_ARGB32)
        if self.alpha < 255:
            p.setOpacity(self.alpha / 255.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # Row 0 maps to the rect's top edge, i.e. the *lowest* price, which is
        # the order the rows were filled in. One row spans `row_ticks` ticks, so
        # the rect has to be sized in bucket space or the field would be drawn
        # at 1/row_ticks of its true height.
        row_h = rt * tick
        p.drawImage(QRectF(b0, r_lo * row_h - row_h / 2, ncols, nrows * row_h),
                    img)
        if self.alpha < 255:
            p.setOpacity(1.0)


class BBOItem(_BufItem):
    def __init__(self, tick: float):
        super().__init__(tick)
        self.setZValue(-5)

    def paint(self, p: QPainter, *args) -> None:
        if not self.cols:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick
        vis, x_lo, x_hi = self._visible()
        if not vis:
            return
        # Width 2: the bid/ask pair has to read as a spread *channel* over a
        # bright heatmap, and a hairline disappears against white/amber walls.
        for side, color in (("bid_ti", BID_LINE), ("ask_ti", ASK_LINE)):
            p.setPen(pg.mkPen(color, width=2))
            prev = None
            is_bid = side == "bid_ti"
            for c in vis:
                ti = c.bid_ti if is_bid else c.ask_ti
                if ti is None:
                    prev = None
                    continue
                y = ti * tick
                x = c.bucket
                if prev is not None:
                    py, px = prev
                    p.drawLine(QPointF(px, py), QPointF(x, py))   # horizontal hold
                    p.drawLine(QPointF(x, py), QPointF(x, y))     # vertical step
                p.drawLine(QPointF(x, y), QPointF(x + 1, y))
                prev = (y, x + 1)


# Columns of tolerance for out-of-order prints when scanning the tape backwards.
_TRADE_SCAN_SLACK = 8.0

# Fraction of a FULL tape that the fold deliberately skips at the back, so that
# eviction does not invalidate the cache on every single print. Proportional,
# not absolute: 2% of a 60,000 print tape is 1,200 prints and buys 1,200
# frames between rebuilds, while 2% of a small tape is a handful. An absolute
# margin would have skipped 30% of a 4,000-print tape. See the rebuild path in
# _TapeItem._cells for the measurement behind it.
REBUILD_MARGIN_FRAC = 0.02

# Clamp for viewport x values. Generous next to real data - x is epoch seconds
# over the column width, so a live chart sits near 1.7e9 - while leaving the
# packed key (time_bin << 32) far inside int64 once the bin is also clamped to
# the data's own span in _grouped.
_X_LIMIT = 1e12
# Clamp for the derived time bin, which divides x by the scale and so is not
# bounded by _X_LIMIT alone.
#
# THE VALUE IS FORCED, not chosen for comfort. The key packs the time bin into
# the high 32 bits, and _grouped shifts `hi_bin + 1`, so the largest bin that
# survives the shift inside a signed 64-bit int is 2**31 - 2. A first attempt
# at 2**30 looked generously large and was in fact SMALLER THAN A REAL BIN: x
# is epoch seconds over the column width, so a live chart today sits at
# 1.79e9 and every bubble vanished.
#
# Headroom against real data is therefore only 1.20x, and it is a date, not a
# size: bins reach 2**31 at epoch second 2147483648, in January 2038. The
# packing also assumes col_dt >= 1 - a sub-second column would multiply x and
# overflow immediately - which is why tests/binned_exact.py pins both.
_BIN_LIMIT = (1 << 31) - 2

_EMPTY_I64 = np.zeros(0, dtype=np.int64)
_NO_CELLS = (_EMPTY_I64, _EMPTY_I64, _EMPTY_I64)


def _unpack(keys):
    """Packed key array -> (time bin, price bucket), both int64.

    The bucket occupies the low 32 bits as two's complement, so the cast to
    int32 is what restores a negative bucket - masking alone would read one as
    a large positive number and draw the bubble four billion ticks away.
    """
    return (keys >> 32).astype(np.int64), (keys & 0xFFFFFFFF).astype(np.int32)


def _left_edge(tx, base, cap, n, target):
    """First logical index k with tx[k] >= target, over the wrapped ring.

    Replaces a newest-first Python scan that walked up to the whole tape to
    find the same index - measured at the 60,000 cap, 13.31 ms of a 24.19 ms
    rebuild, spent entirely on locating a boundary.

    It rests on the same assumption the scan did: the tape is in arrival
    order, so x is non-decreasing, which is why stopping at the first entry
    below the edge was correct in the first place. Two searches because the
    ring wraps - the logical sequence is tx[base:cap] then tx[0:base] - and
    numpy will not search across that seam for us.
    """
    if n <= 0:
        return 0
    tail = cap - base                    # entries before the wrap
    if n <= tail:
        return int(np.searchsorted(tx[base:base + n], target, "left"))
    i = int(np.searchsorted(tx[base:cap], target, "left"))
    if i < tail:
        return i
    return tail + int(np.searchsorted(tx[0:n - tail], target, "left"))


def _merge_pending(keys, buys, sells, pending, lo_bin, hi_bin):
    """Fold the small live-trade dict into the sorted rebuild arrays.

    NEVER MUTATES ITS INPUTS. They are the cache; a frame that added its
    pending volume into them in place would add it again on the next frame,
    which is the doubling bug this file has already been bitten by once.
    """
    n = len(pending)
    pk = np.fromiter(pending.keys(), dtype=np.int64, count=n)
    flat = np.fromiter(chain.from_iterable(pending.values()),
                       dtype=np.int64, count=2 * n)
    pb, ps = flat[0::2], flat[1::2]
    if lo_bin is not None:
        xb = pk >> 32
        m = (xb >= lo_bin) & (xb <= hi_bin)
        if not m.all():
            pk, pb, ps = pk[m], pb[m], ps[m]
    if pk.size == 0:
        return keys, buys, sells

    miss = np.ones(pk.size, dtype=bool)
    if keys.size:
        # The rebuild arrays are sorted, so locating every pending bin is one
        # binary search rather than a scan. A pending key is unique (it came
        # out of a dict), so the scatter-add below cannot collide with itself.
        pos = np.minimum(np.searchsorted(keys, pk), keys.size - 1)
        hit = keys[pos] == pk
        if hit.any():
            buys, sells = buys.copy(), sells.copy()
            at = pos[hit]
            buys[at] += pb[hit]
            sells[at] += ps[hit]
            miss = ~hit
    if miss.any():
        keys = np.concatenate((keys, pk[miss]))
        buys = np.concatenate((buys, pb[miss]))
        sells = np.concatenate((sells, ps[miss]))
        o = np.argsort(keys, kind="stable")
        keys, buys, sells = keys[o], buys[o], sells[o]
    return keys, buys, sells


class _TapeItem(_BufItem):
    """Shared base for the three trade overlays (bubbles / pies / split bars).

    They differ only in how a binned cell is drawn, so binning, filtering,
    sizing and the hover index live here once.

    The time bin is deliberately INDEPENDENT of the heatmap's column
    aggregation. Tying them together meant that selecting a 1-minute bookmap
    collapsed every print in that minute onto a single x, so the tape rendered
    as a vertical stack of circles at one instant instead of a readable
    left-to-right sequence. Now the heatmap can be coarse (to see structure)
    while the tape stays fine (to see order flow), which is the combination a
    scalper actually wants.
    """

    def __init__(self, tick: float, buffer=None):
        super().__init__(tick)
        self.buffer = buffer
        self.xscale = 1.0        # 1/agg — maps base column units to display x
        self.bin_cols = 1.0      # time bin width, in BASE column units
        self.row_ticks = 1       # price bucket, in ticks
        self.min_size = 0        # noise filter on the binned total
        self.size_scale = 1.0    # user size multiplier
        self.max_cells = 320
        self._eff_bin = 1.0
        # (x_display, price, buy, sell) of everything drawn last frame, for the
        # window's hover readout. Without this a bubble can be seen but not
        # interrogated, and "how much of that was buying?" is the whole question.
        self.drawn: list[tuple] = []
        self.setZValue(0)

    # Pending folds are absorbed into the sorted arrays once there are this
    # many. It bounds the per-frame merge, which is O(P log N) in the pending
    # count - and P only grows between rebuilds, so left alone it would grow
    # until the next one.
    MERGE_PENDING = 2048

    def _grouped(self):
        """(keys, buys, sells) as sorted int64 arrays over the VISIBLE tape.

        `keys` packs (time bin << 32 | price bucket), so sorting by key sorts
        by time bin first and a time range is therefore one contiguous slice.

        WHY ARRAYS AND NOT A DICT. This used to return
        {(x_bin, bucket): [buy, sell]}, and measured on a full 60,000-print
        tape zoomed out - 45,315 bins:

            building the dict after the vectorised grouping   29.1 ms
            _binned walking it, on EVERY frame                54.2 ms

        The second number is the one that mattered. The rebuild is periodic,
        but _binned runs on every paint, and 54 ms against an 80 ms timer is
        two thirds of the budget spent iterating 45,000 Python tuples to throw
        away 99% of them - `max_cells` is 320.

        Arrays make both stages vectorised: the filter is a mask, the top-320
        is an argpartition, and a Python-level tuple exists only for the 320
        bubbles that are actually drawn.

        INCREMENTAL. Re-binning every visible print on every frame was ~49% of
        the bubble overlay's cost and the single largest item on a busy desk:
        a dense tape puts 15,000+ prints inside the view, and all but the
        handful that arrived since the last frame were re-folded into exactly
        the bins they were already in.

        The bins are keyed by ABSOLUTE time bin and price bucket, so they do
        not depend on the viewport - which is what makes reuse possible at all.
        The cache is kept honest by rebuilding whenever anything could make it
        disagree with the tape:

          * the binning parameters or the buffer changed;
          * the view scrolled LEFT of the span we folded, into bins we never
            built;
          * eviction has eaten into the folded span. The tape is a bounded
            deque, so old prints are dropped silently; a bin built from prints
            the tape no longer holds would draw volume that cannot be shown
            anywhere else on the chart, which is exactly the kind of
            can't-check-it discrepancy this codebase treats as false data.
            This is tested on ABSOLUTE PRINT INDEX, not on position: comparing
            x coordinates instead made a fresh session - where the oldest print
            legitimately sits inside the view - look identical to one that had
            evicted, and rebuilt on every single frame;
          * the folded span has grown past a few screens, which bounds the
            cache so a long session cannot inflate it.

        Eviction is detectable exactly because the buffer counts every print it
        has ever appended, so `trade_count - len(trades)` is the absolute index
        of the oldest print still retained.
        """
        buf = self.buffer
        if buf is None or buf.trade_count == 0:
            self._cache = None
            return _NO_CELLS
        x_lo, x_hi = self._xrange()
        if x_hi <= x_lo:
            return _NO_CELLS
        xs = self.xscale
        # BIN AT THE RESOLUTION THE SCREEN CAN SHOW, not finer.
        #
        # Zoomed out over a full tape the view spans ~7,200 columns across
        # ~1,400 pixels - five columns per pixel - so a one-column bin is
        # sub-pixel. Measured there: 60,000 prints folded into 58,160 distinct
        # bins, a compression of 1.03x, of which _binned then draws the 320
        # largest and discards 99%. All that work produced detail no monitor
        # can resolve, and the prints in the discarded 99% were not drawn at
        # all - their volume simply vanished.
        #
        # Widening the bin to one pixel bounds the fold by the WINDOW rather
        # than by the tape, so it costs the same whether the tape holds a
        # minute or a full day. It is also more honest: neighbouring prints now
        # merge into one bubble carrying their combined volume instead of
        # 99 of every 100 being dropped for not being in the top 320.
        #
        # It never makes the bin FINER than asked for - max() - so the zoomed-in
        # case, which is the one people trade from, is bit-for-bit unchanged.
        bin_cols = self.bin_cols
        vb_ = self.getViewBox()
        if vb_ is not None:
            try:
                px = vb_.viewPixelSize()[0] / max(xs, 1e-9)
                if px > bin_cols:
                    # QUANTISED to a power-of-two multiple. The effective bin
                    # is part of the cache signature, so if it tracked the
                    # viewport continuously it would change on every frame that
                    # follows live price - and the cache would rebuild every
                    # frame, which is the failure this is here to fix. Snapping
                    # means it only moves on a real zoom.
                    steps = math.ceil(math.log2(px / bin_cols))
                    bin_cols = bin_cols * (2.0 ** max(0, steps))
            except Exception:
                pass
        inv = 1.0 / max(1e-9, bin_cols)
        self._eff_bin = bin_cols
        rt = max(1, int(self.row_ticks))
        # Read the RING ARRAYS, not the TapeView. The view builds a tuple
        # per access, which is exactly the allocation the ring was introduced
        # to remove - going through it here would move the cost from memory to
        # CPU on the hottest loop in the application.
        tx, tti, tsz, tag = buf.trade_x, buf.trade_ti, buf.trade_sz, buf.trade_ag
        cap = buf.max_trades
        total = buf.trade_count
        n = total if total < cap else cap
        first_abs = total - n                # absolute index of logical 0
        base = buf._tape_first               # physical slot of logical 0
        fold_lo = x_lo - _TRADE_SCAN_SLACK

        c = getattr(self, "_cache", None)
        sig = (id(buf), xs, round(inv, 9), rt)
        reusable = (
            c is not None
            and c["sig"] == sig
            and first_abs <= c["fold_start"]    # no FOLDED print has been evicted
            and x_lo >= c["lo_x"]               # view still inside the fold
            and (x_lo - c["lo_x"]) <= 3.0 * (x_hi - x_lo)   # cache stays bounded
        )

        if reusable:
            start = c["consumed"] - first_abs
        else:
            # Rebuild starts at the left edge, so a cold cache folds the view
            # and not the whole 60k tape.
            start = _left_edge(tx, base, cap, n, fold_lo / xs) if xs > 0 else 0
            # NEVER FOLD THE OLDEST SLIVER OF THE TAPE. This is what actually
            # fixes the two-hour freeze, and it is a scheduling fix rather than
            # a speed one.
            #
            # The cache is dropped when a print it folded has been evicted.
            # Once the ring is at its cap EVERY new print evicts one, so if the
            # fold reaches the oldest print - which it does the moment you zoom
            # out to the whole tape - the cache is invalid again one print
            # later. Measured: rebuilt on 8 frames out of 8, ~120 ms each,
            # against an 80 ms timer. 60,000 prints at ~8/sec is 2.08 hours to
            # fill the ring, and a restart cleared it because the ring began
            # empty.
            #
            # Holding the fold back by a margin means eviction only invalidates
            # once that margin is consumed - one rebuild per REBUILD_MARGIN
            # prints instead of one per print.
            #
            # What it costs: the oldest ~2% of the tape is not drawn when you
            # are zoomed out far enough to see the whole thing. That is an
            # omission at the extreme left edge, not an invention - and it sits
            # next to _binned already keeping only the largest `max_cells` of
            # ~46,000 bins, which discards 99% of them.
            if n >= cap:
                margin = int(cap * REBUILD_MARGIN_FRAC)
                if start < margin:
                    start = margin
            # VECTORISED, because this path is not as rare as it looks. The
            # reuse test rejects the cache when a print it folded has been
            # evicted - and once the tape is at its cap, EVERY new print evicts
            # one. Zoomed out far enough that the fold reaches the oldest
            # print, that is a full rebuild on every single frame.
            #
            # Measured at the 60,000 cap: 108 ms per frame, rebuilt 8 frames
            # out of 8, against an 80 ms timer. That is the two-hour freeze -
            # 60,000 prints at ~8/sec is 2.08 hours to fill the ring, and it
            # cleared on restart because the ring started empty again.
            #
            # The invalidation rule is NOT relaxed: a bin holding prints the
            # tape no longer has would draw volume that exists nowhere else.
            # The rebuild is simply made cheap enough that doing it every frame
            # does not matter.
            if start < n:
                idx = np.arange(start, n, dtype=np.int64)
                ps = (base + idx) % cap
                xb = np.floor(tx[ps] * inv).astype(np.int64)
                tb = tti[ps].astype(np.int64) // rt
                b_arr, s_arr = split_sizes(tsz[ps], tag[ps], tti[ps])
                # One 64-bit key per (time bin, price bucket) so the grouping
                # is a single sort rather than a dict insert per print.
                keys = (xb << 32) | (tb & 0xFFFFFFFF)
                uniq, inv_idx = np.unique(keys, return_inverse=True)
                bs = np.bincount(inv_idx, weights=b_arr).astype(np.int64)
                ss = np.bincount(inv_idx, weights=s_arr).astype(np.int64)
                # np.unique returns `uniq` sorted, which _grouped's callers
                # rely on for both the visible-range slice and the pending
                # merge. Nothing below may reorder it.
            else:
                uniq, bs, ss = _EMPTY_I64, _EMPTY_I64, _EMPTY_I64
            # `pending` carries the prints folded one at a time since this
            # rebuild. Keeping them separate is what lets the rebuild output
            # stay a sorted array: a scalar insert into a sorted array is O(N),
            # while a dict of a few hundred live trades merges back in one
            # searchsorted.
            c = self._cache = {"sig": sig, "keys": uniq, "buys": bs,
                               "sells": ss, "pending": {}, "lo_x": fold_lo,
                               "fold_start": first_abs + start,
                               "consumed": first_abs + n}
            # The vectorised pass has already folded start..n. The incremental
            # loop below folds `start` onwards, so leaving `start` where it was
            # would fold every one of them a SECOND time - the cache read
            # exactly double, which the exactness oracle caught immediately.
            start = n

        pending = c["pending"]
        for k in range(start, n):
            p = (base + k) % cap
            x = tx[p]
            ti = int(tti[p])
            size = int(tsz[p])
            aggr = _AG_FROM[tag[p]]
            key = (int(math.floor(x * inv)) << 32) | ((ti // rt) & 0xFFFFFFFF)
            e = pending.get(key)
            if e is None:
                e = pending[key] = [0, 0]
            # THE one split (model.split_size). This used to be
            # `if sell: ... else: buy`, which counted every UNKNOWN print as
            # 100% buying - so a bubble was green whenever the print could not
            # be classified, and the overlay systematically overstated buying
            # by the whole unclassified volume. That is the "everything is
            # green" report, and it was false data, not a colour choice.
            b, sl = split_size(size, aggr, ti)
            e[0] += b
            e[1] += sl
        c["consumed"] = total

        if len(pending) >= self.MERGE_PENDING:
            c["keys"], c["buys"], c["sells"] = _merge_pending(
                c["keys"], c["buys"], c["sells"], pending, None, None)
            pending = c["pending"] = {}

        keys, buys, sells = c["keys"], c["buys"], c["sells"]
        # The fold covers [lo_x, live edge]; the caller may be looking at less
        # than that, so the visible subset is selected here. Bin -> x is exact
        # (the key IS floor(x / bin_cols)), so this filter is the same one the
        # old per-trade scan applied - but on sorted keys it is a slice rather
        # than a test per bin, because the time bin is in the HIGH bits.
        lo_bin = hi_bin = None
        if self.bin_cols * xs > 0:
            # CLAMPED BEFORE ANY USE. `lo_bin << 32` is a Python int of
            # unbounded width and numpy raises converting one past int64, so a
            # viewport far outside the data would take out the frame rather
            # than simply showing nothing. _X_LIMIT bounds x, but the bin also
            # divides by the scale, which can be small - so the bin is bounded
            # here in its own right. 2^30 is far beyond any real time bin
            # (a live chart sits near 1.7e9 seconds / bin width) while leaving
            # the shifted key inside int64 with room to spare.
            lo_bin = max(-_BIN_LIMIT, min(_BIN_LIMIT,
                                          math.floor((x_lo / xs) * inv) - 1))
            hi_bin = max(-_BIN_LIMIT, min(_BIN_LIMIT,
                                          math.floor((x_hi / xs) * inv) + 1))
            i0 = int(np.searchsorted(keys, lo_bin << 32, "left"))
            i1 = int(np.searchsorted(keys, (hi_bin + 1) << 32, "left"))
            keys, buys, sells = keys[i0:i1], buys[i0:i1], sells[i0:i1]
        if not pending:
            return keys, buys, sells
        return _merge_pending(keys, buys, sells, pending, lo_bin, hi_bin)

    def _cells(self) -> dict:
        """_grouped as {(x_bin, price_bucket): [buy, sell]}.

        The old shape of the hot path, kept because the exactness tests compare
        against it bin by bin (tests/tape_ring.py, tests/tape_cache.py,
        tests/freeze_2h.py) and a dict is what an oracle built from a plain
        Python loop can be compared to directly. Nothing that paints calls it -
        materialising 45,000 entries is the cost the refactor removed.
        """
        keys, buys, sells = self._grouped()
        if not keys.size:
            return {}
        xb, tb = _unpack(keys)
        return dict(zip(zip(xb.tolist(), tb.tolist()),
                        map(list, zip(buys.tolist(), sells.tolist()))))

    def _binned(self) -> list[tuple]:
        """[(x_display, price, buy, sell, total)] largest last, capped."""
        keys, buys, sells = self._grouped()
        if not keys.size:
            return []
        tot = buys + sells
        keep = (tot >= self.min_size) & (tot > 0)
        if not keep.all():
            keys, buys, sells, tot = keys[keep], buys[keep], sells[keep], tot[keep]
        n = tot.size
        if n == 0:
            return []
        if n > self.max_cells:
            # A dense tape yields well over a thousand cells in view. Drawing
            # them all is both the frame cost and a wall of tiny circles that
            # buries the prints worth seeing - keep the largest. The size scale
            # comes from what survives, so those still read against each other.
            #
            # argpartition, not a sort: selecting the top 320 of 45,000 is
            # O(n), and only those 320 are then ordered. Where several bins tie
            # exactly on the cut, which one survives is arbitrary but
            # deterministic - it was arbitrary before too, decided by dict
            # insertion order.
            sel = np.argpartition(tot, n - self.max_cells)[n - self.max_cells:]
            keys, buys, sells, tot = keys[sel], buys[sel], sells[sel], tot[sel]
        o = np.argsort(tot, kind="stable")           # big drawn last / on top
        keys, buys, sells, tot = keys[o], buys[o], sells[o], tot[o]
        rt = max(1, int(self.row_ticks))
        # The SAME effective bin _grouped used, or every bubble is drawn at the
        # wrong x - the key is floor(x / bin), so the inverse needs the same
        # divisor.
        bc, xs, tick = self._eff_bin, self.xscale, self.tick
        xb, tb = _unpack(keys)
        xv = (xb + 0.5) * (bc * xs)                  # centre of the time bin
        pv = (tb + 0.5) * (rt * tick)                # centre of the price bucket
        return list(zip(xv.tolist(), pv.tolist(),
                        buys.tolist(), sells.tolist(), tot.tolist()))


class BubbleItem(_TapeItem):
    def __init__(self, tick: float, buffer=None):
        super().__init__(tick, buffer)
        self.min_r = 3.0
        self.max_r = 26.0

    def paint(self, p: QPainter, *args) -> None:
        data = self._binned()
        self.drawn = [(x, y, b, s) for x, y, b, s, _ in data]
        if not data:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        smax = data[-1][4] or 1
        sc = self.size_scale
        rmin, rmax = self.min_r * sc, self.max_r * sc
        tr = p.transform()
        p.resetTransform()
        for x, price, b, s, total in data:
            pt = tr.map(QPointF(x, price))
            r = rmin + (rmax - rmin) * math.sqrt(total / smax)
            self._sphere(p, pt, r, BUY_BUBBLE if b >= s else SELL_BUBBLE)

    @staticmethod
    def _sphere_sprite(r: int, base: QColor) -> QPixmap:
        """A pre-rendered sphere, cached by (radius, colour).

        Building a QRadialGradient per bubble was ~half the Bookmap's entire
        paint - 14 ms of 30 ms, at 190 bubbles a frame - and every one of those
        gradients is identical for a given radius and colour. Rendering each
        distinct one ONCE into a pixmap turns the per-bubble cost into a blit.

        The cache is bounded by construction: radius is an integer pixel count
        over a small range and there are three bubble colours, so it settles at
        a few dozen small pixmaps and never grows with the tape.
        """
        key = (r, base.rgba())
        hit = _SPHERE_CACHE.get(key)
        if hit is not None:
            return hit
        d = r * 2 + 2                       # +2 so the 0.6px rim is not clipped
        pm = QPixmap(d, d)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPointF(d / 2.0, d / 2.0)
        grad = QRadialGradient(c.x() - r * 0.35, c.y() - r * 0.4, r * 1.5)
        hi = base.lighter(160)
        grad.setColorAt(0.0, QColor(min(255, hi.red()), min(255, hi.green()),
                                    min(255, hi.blue()), 245))
        grad.setColorAt(0.45, QColor(base.red(), base.green(), base.blue(), 225))
        dk = base.darker(180)
        grad.setColorAt(1.0, QColor(dk.red(), dk.green(), dk.blue(), 215))
        q.setBrush(QBrush(grad))
        q.setPen(QPen(QColor(dk.red(), dk.green(), dk.blue(), 230), 0.6))
        q.drawEllipse(c, float(r), float(r))
        q.end()
        if len(_SPHERE_CACHE) > 256:        # guard, not a policy
            _SPHERE_CACHE.clear()
        _SPHERE_CACHE[key] = pm
        return pm

    @staticmethod
    def _sphere(p: QPainter, pt: QPointF, r: float, base: QColor) -> None:
        """A Bookmap volume dot: a shaded sphere with a top-left highlight.

        Measured, not assumed. A radial cut through a bubble in the reference
        capture gives luminance 30 -> 83 -> 150 -> 96 -> 92 across the diameter,
        with the peak offset from centre — that is a lit sphere. A flat disc
        would be constant. Slight translucency lets overlapping prints build up
        without hiding the liquidity field behind them.

        Radius is quantised to whole pixels so the sprite cache can hit. That
        is a sub-pixel size change on a soft-edged dot and is not visible; it
        is the one thing here that is NOT bit-identical to the old renderer.
        """
        if r < 2.5:
            # Below a few pixels the gradient is not resolvable, and building one
            # per bubble on a dense tape is pure cost.
            p.setBrush(QBrush(base))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(pt, r, r)
            return
        ri = int(r + 0.5)
        pm = BubbleItem._sphere_sprite(ri, base)
        p.drawPixmap(QPointF(pt.x() - pm.width() / 2.0,
                             pt.y() - pm.height() / 2.0), pm)


# Same aggression palette as the volume dots — one chart, one colour language.
PIE_BUY = BUY_BUBBLE                # green (aggressive buys)
PIE_SELL = SELL_BUBBLE              # red (aggressive sells)
SUPPORT_GREEN = QColor(40, 210, 122)
RESIST_RED = QColor(244, 74, 86)
# Darker, heavier tones for the absolute S/R levels: these are structural marks
# that must stay legible across the whole pane without competing with the trade
# bubbles for attention the way the bright projection bands do.
SR_SUPPORT = QColor(18, 102, 56)
SR_RESIST = QColor(138, 28, 36)


def _bmfmt(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{v / 1000:.0f}K"
    return str(v)


class SRLinesItem(pg.GraphicsObject):
    """Absolute support (dark green) / resistance (dark red) levels.

    Drawn full width and carried past the last trade into the projection zone,
    so the level price is heading toward is visible before it gets there. Each
    is a translucent zone band (the level has depth, it is not a hairline) with
    a solid rule at the exact price and a label carrying the resting size and
    how long it has held.
    """

    def __init__(self, tick: float):
        super().__init__()
        self.tick = tick
        self.support = None
        self.resistance = None
        self.x0 = 0.0
        self.x1 = 1.0
        self.zone_ticks = 1.6          # half-height of the shaded zone
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._bounds = QRectF()
        self.setZValue(-12)            # above the heatmap, below trades

    def set_levels(self, support, resistance, x0: float, x1: float) -> None:
        self.prepareGeometryChange()
        self.support, self.resistance = support, resistance
        self.x0, self.x1 = x0, x1
        tis = [lv.ti for lv in (support, resistance) if lv is not None]
        if tis and x1 > x0:
            lo = (min(tis) - self.zone_ticks - 1) * self.tick
            hi = (max(tis) + self.zone_ticks + 1) * self.tick
            self._bounds = QRectF(x0, lo, x1 - x0, hi - lo)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        if self.support is None and self.resistance is None:
            return
        tick = self.tick
        x0, x1 = self.x0, self.x1
        if x1 <= x0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tr = p.transform()

        for lv, color, tag in ((self.support, SR_SUPPORT, "SUPPORT"),
                               (self.resistance, SR_RESIST, "RESIST")):
            if lv is None:
                continue
            y = lv.ti * tick
            zone = self.zone_ticks * tick
            # The level almost always sits on the heaviest wall, which the
            # heatmap paints white/amber - so the zone needs enough opacity to
            # tint it green/red rather than letting it read as just a bright
            # band with a coloured edge.
            p.fillRect(QRectF(x0, y - zone, x1 - x0, 2 * zone),
                       QColor(color.red(), color.green(), color.blue(), 135))
            p.setPen(pg.mkPen(color, width=4))
            p.drawLine(QPointF(x0, y), QPointF(x1, y))

            pt = tr.map(QPointF(x1, y))
            label = f"{tag} {_bmfmt(lv.size)} · {lv.held}"
            p.save(); p.resetTransform()
            p.setFont(self.font)
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(label) + 10
            box = QRectF(pt.x() - w - 6, pt.y() - 9, w, 18)
            p.fillRect(box, QColor(color.red(), color.green(), color.blue(), 235))
            p.setPen(pg.mkPen("#F2F6FA"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
            p.restore()


class ProjectionItem(pg.GraphicsObject):
    """Projects the CURRENT resting book as fat horizontal bands in the empty
    space just ahead of the latest pie — the limit orders waiting for price.
    Bands are heatmap-coloured (blue->amber->red by size); the single strongest
    bid wall below price is drawn GREEN (support) and the strongest ask wall
    above price RED (resistance), each labelled with its size."""

    def __init__(self, tick: float):
        super().__init__()
        self.col = None
        self.tick = tick
        self.x0 = 0.0
        self.width = 7.0
        self.mid_ti = None
        self.vmax = 1
        self.wall_mult = 4.0         # wall = this × average book size
        self.wall_floor = 4000       # …and at least this many shares
        self.support = None          # set by set_sr(); shared with SRLinesItem
        self.resistance = None
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._bounds = QRectF()
        self.setZValue(-14)          # above heatmap, below pies/bubbles

    def set_sr(self, support, resistance) -> None:
        """Use the tracker's persistence-weighted levels rather than picking the
        biggest level in this one snapshot - otherwise the band highlighted here
        and the line drawn by SRLinesItem can disagree on the same frame."""
        self.support, self.resistance = support, resistance
        self.update()

    def set_projection(self, col, x0, mid_ti, vmax) -> None:
        self.prepareGeometryChange()
        self.col = col
        self.x0 = x0
        self.mid_ti = mid_ti
        self.vmax = max(1, vmax)
        if col and col.book:
            lo = min(col.book) * self.tick
            hi = max(col.book) * self.tick
            self._bounds = QRectF(x0, lo - self.tick, self.width, (hi - lo) + 2 * self.tick)
        else:
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        col = self.col
        if col is None or not col.book:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick
        x0, w, mid = self.x0, self.width, self.mid_ti
        book = col.book

        # Wall threshold still gates whether the band is worth highlighting, but
        # *which* level counts comes from the persistence tracker.
        avg = float(book.values().mean())      # numpy, not a boxed Python sum
        thr = max(self.wall_floor, avg * self.wall_mult)
        sup_ti = self.support.ti if self.support is not None else None
        res_ti = self.resistance.ti if self.resistance is not None else None
        sup_sz = book.get(sup_ti, 0) if sup_ti is not None else 0
        res_sz = book.get(res_ti, 0) if res_ti is not None else 0

        denom = math.log1p(self.vmax)
        for ti, size in book.items():
            v = math.log1p(size) / denom
            idx = 255 if v >= 1 else int(v * 255)
            c = _LUT[idx]
            p.fillRect(QRectF(x0, ti * tick - tick / 2, w, tick),
                       QColor(c.red(), c.green(), c.blue(), 235))

        # absolute support (green) / resistance (red) — fat bright bands + label
        tr = p.transform()
        for ti, size, color, tag in ((sup_ti, sup_sz, SUPPORT_GREEN, "S"),
                                     (res_ti, res_sz, RESIST_RED, "R")):
            if ti is None or size < thr:
                continue
            p.fillRect(QRectF(x0, ti * tick - tick * 0.85, w, tick * 1.7), color)
            pt = tr.map(QPointF(x0, ti * tick))
            p.save(); p.resetTransform()
            p.setFont(self.font); p.setPen(pg.mkPen("#06131E"))
            p.drawText(QPointF(pt.x() + 4, pt.y() + 4), f"{tag} {_bmfmt(size)}")
            p.restore()


class PieItem(_TapeItem):
    """One pie per time bin at that bin's volume-weighted price, split into a
    green (buy) and red (sell) wedge — reads the aggression ratio at a glance.

    Binned on the tape timeframe, not the heatmap column: at a 1-minute bookmap
    a per-column pie gave one circle per minute, which is not a tape. Now the
    pies march horizontally at whatever tape resolution is selected, each at its
    own traded price.
    """

    def __init__(self, tick: float, buffer=None):
        super().__init__(tick, buffer)
        self.min_r = 7.0
        self.max_r = 30.0

    def _by_bin(self) -> list[tuple]:
        """Collapse the price dimension: one entry per time bin, at its VWAP."""
        keys, buys, sells = self._grouped()
        if not keys.size:
            return []
        rt = max(1, int(self.row_ticks))
        xb, tb = _unpack(keys)
        price = (tb + 0.5) * (rt * self.tick)
        u, iv = np.unique(xb, return_inverse=True)
        m = u.size
        b = np.bincount(iv, weights=buys, minlength=m).astype(np.int64)
        s = np.bincount(iv, weights=sells, minlength=m).astype(np.int64)
        pv = np.bincount(iv, weights=price * (buys + sells), minlength=m)
        tot = b + s
        keep = (tot >= self.min_size) & (tot > 0)
        if not keep.all():
            u, b, s, pv, tot = u[keep], b[keep], s[keep], pv[keep], tot[keep]
        n = tot.size
        if n == 0:
            return []
        if n > self.max_cells:
            sel = np.argpartition(tot, n - self.max_cells)[n - self.max_cells:]
            u, b, s, pv, tot = u[sel], b[sel], s[sel], pv[sel], tot[sel]
        o = np.argsort(tot, kind="stable")
        u, b, s, pv, tot = u[o], b[o], s[o], pv[o], tot[o]
        x = (u + 0.5) * (self.bin_cols * self.xscale)
        return list(zip(x.tolist(), (pv / tot).tolist(),
                        b.tolist(), s.tolist(), tot.tolist()))

    def paint(self, p: QPainter, *args) -> None:
        data = self._by_bin()
        self.drawn = [(x, y, b, s) for x, y, b, s, _ in data]
        if not data:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        smax = data[-1][4] or 1
        tr = p.transform()
        # A pie wider than its own time bin necessarily collides with its
        # neighbours - the one thing this mode exists to avoid. Cap the radius
        # at just under half a bin's on-screen width so the row stays a clean
        # sequence at any zoom. The user's size multiplier is allowed to push
        # past that (they asked for bigger), but the floor keeps it visible when
        # zoomed out, which is where they reported losing the circles entirely.
        binw = abs(tr.map(QPointF(self.bin_cols * self.xscale, 0.0)).x()
                   - tr.map(QPointF(0.0, 0.0)).x())
        sc = self.size_scale
        rmax = max(2.5 * sc, min(self.max_r * sc, binw * 0.46 * sc))
        rmin = min(self.min_r * sc, rmax)
        p.resetTransform()
        for x, price, b, s, tot in data:
            pt = tr.map(QPointF(x, price))
            r = rmin + (rmax - rmin) * math.sqrt(tot / smax)
            self._glossy_pie(p, pt, r, b / tot)

    @staticmethod
    def _glossy_pie(p: QPainter, pt: QPointF, r: float, buy_frac: float) -> None:
        rect = QRectF(pt.x() - r, pt.y() - r, 2 * r, 2 * r)
        buy_span = int(round(360 * 16 * buy_frac))
        # flat wedges (clear red/blue split)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(PIE_BUY))
        p.drawPie(rect, 90 * 16, buy_span)                  # buy from 12 o'clock, CCW
        p.setBrush(QBrush(PIE_SELL))
        p.drawPie(rect, 90 * 16 + buy_span, 360 * 16 - buy_span)
        # glossy sphere sheen (white highlight top-left -> transparent)
        g = QRadialGradient(pt.x() - r * 0.34, pt.y() - r * 0.4, r * 1.35)
        g.setColorAt(0.0, QColor(255, 255, 255, 140))
        g.setColorAt(0.45, QColor(255, 255, 255, 24))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(g))
        p.drawEllipse(pt, r, r)
        # dark rim for depth
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(6, 18, 34, 230), 1.2))
        p.drawEllipse(pt, r, r)


class BarsItem(PieItem):
    """One vertical split-bar per time bin at its VWAP price: green segment ∝
    buy volume, red ∝ sell volume — a compact non-overlapping alternative.

    Shares PieItem's per-bin aggregation; only the glyph differs.
    """

    def paint(self, p: QPainter, *args) -> None:
        data = self._by_bin()
        self.drawn = [(x, y, b, s) for x, y, b, s, _ in data]
        if not data:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        smax = data[-1][4] or 1
        tick = self.tick
        w = self.bin_cols * self.xscale * 0.6      # bar width in display x
        sc = self.size_scale
        for x, price, b, s, tot in data:
            h = tick * (0.6 + 6.0 * sc * (tot / smax))     # half-height in price
            frac = b / tot
            p.fillRect(QRectF(x - w / 2, price, w, h * frac), QBrush(PIE_BUY))
            p.fillRect(QRectF(x - w / 2, price - h * (1 - frac), w,
                              h * (1 - frac)), QBrush(PIE_SELL))


class DomLadderItem(pg.GraphicsObject):
    """Right-edge depth histogram of one Column's book, with size numbers."""

    def __init__(self, tick: float):
        super().__init__()
        self.col = None
        self.tick = tick
        self.vmax = 1                 # right edge the bars are anchored to
        self.row_ticks = 1            # matches the heatmap's price aggregation
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._bounds = QRectF()
        self._rows: list[tuple[int, int]] = []   # (bucket, summed size)

    def set_col(self, col, tick=None) -> None:
        self.prepareGeometryChange()
        self.col = col
        if tick is not None:
            self.tick = tick
        rt = max(1, int(self.row_ticks))
        rows: dict[int, int] = {}
        if col and col.book:
            # Aggregated to the same price grid as the heatmap, so a level in
            # the ladder lines up with the band it belongs to. Mismatched grids
            # were worse than either grid alone.
            for ti, size in col.book.items():
                b = ti // rt
                rows[b] = rows.get(b, 0) + size
        if rows:
            self._rows = sorted(rows.items())
            row_h = rt * self.tick
            lo = self._rows[0][0] * row_h
            hi = self._rows[-1][0] * row_h
            self.vmax = max(rows.values()) or 1
            self._bounds = QRectF(0, lo - row_h, self.vmax,
                                  (hi - lo) + 2 * row_h)
        else:
            self._rows = []
            self.vmax = 1
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        col = self.col
        if col is None or not self._rows:
            return
        rt = max(1, int(self.row_ticks))
        row_h = rt * self.tick
        mx = self.vmax
        ask_b = (col.ask_ti // rt) if col.ask_ti is not None else 10 ** 12
        tr = p.transform()

        # Bars are anchored at the right edge and grow *left*, so every level's
        # magnitude is read against the price axis it belongs to - the same way
        # a real DOM ladder is laid out. Solid (alpha 230) rather than washed
        # out, so the ladder holds its own next to the heatmap.
        for b, size in self._rows:
            color = ASK_LINE if b >= ask_b else BID_LINE
            c = QColor(color.red(), color.green(), color.blue(), 230)
            p.fillRect(QRectF(mx - size, b * row_h - row_h / 2, size, row_h), c)

        p.setFont(self.font)
        # Light text: it sits over the bar on wide levels and over the dark
        # background on thin ones, and stays readable on both.
        p.setPen(pg.mkPen("#EAEEF5"))
        # Skip labels that cannot fit: at 1-tick rows on a zoomed-out ladder the
        # numbers overlapped into an unreadable smear. One label per ~13 px.
        rh_px = abs(tr.map(QPointF(0.0, row_h)).y() - tr.map(QPointF(0.0, 0.0)).y())
        step = 1 if rh_px >= 13 else max(1, int(math.ceil(13.0 / max(1e-6, rh_px))))
        for i, (b, size) in enumerate(self._rows):
            if i % step:
                continue
            rp = tr.map(QPointF(mx, b * row_h))
            p.save(); p.resetTransform()
            p.drawText(QRectF(rp.x() - 62, rp.y() - 7, 58, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       str(size))
            p.restore()


class VolumeBarsItem(_BufItem):
    """Bottom per-column executed-volume bars, coloured by net delta, numbered."""

    def __init__(self, tick: float):
        super().__init__(tick)
        self.font = QFont("Consolas", 7, QFont.Weight.Bold)

    def set_cols(self, cols):
        self.prepareGeometryChange()
        self.cols = cols
        if cols:
            mx = max((c.vol for c in cols), default=1) or 1
            x0, x1 = cols[0].bucket, cols[-1].bucket
            self._bounds = QRectF(x0 - 1, 0, (x1 - x0) + 3, mx * 1.15)
        else:
            self._bounds = QRectF()
        self.update()

    def paint(self, p: QPainter, *args) -> None:
        vis, _x_lo, _x_hi = self._visible()
        if not vis:
            return
        tr = p.transform()

        # 1. Colour comes from the column's own running delta (engine-side,
        #    one addition per print) instead of two sum() passes over the
        #    column's whole price dict on every frame, for every visible
        #    column, of every book.
        # 2. The label is skipped when a column is narrower than the text it
        #    would hold: at that width the digits overlap into a grey smear, so
        #    dropping them is both cheaper AND more readable.
        px_per_col = abs(tr.m11())
        show_text = px_per_col >= 26.0

        bars, colours = [], []
        for c in vis:
            if c.vol == 0:
                continue
            base = BID_LINE if c.net >= 0 else ASK_LINE
            bars.append(QRectF(c.bucket + 0.28, 0, 0.44, c.vol))
            colours.append(QColor(base.red(), base.green(), base.blue(), 225))
        for rect, col in zip(bars, colours):
            p.fillRect(rect, col)

        if not show_text:
            return
        # 3. save()/resetTransform()/setFont()/setPen() ran per column; the font
        #    and pen are identical every time, so they are hoisted out and the
        #    painter state is pushed once for the whole label pass.
        p.save()
        p.resetTransform()
        p.setFont(self.font)
        p.setPen(pg.mkPen("#AEB4C0"))
        for c in vis:
            if c.vol == 0:
                continue
            rp = tr.map(QPointF(c.bucket + 0.5, c.vol))
            p.drawText(QRectF(rp.x() - 14, rp.y() - 14, 28, 12),
                       Qt.AlignmentFlag.AlignCenter, str(c.vol))
        p.restore()
