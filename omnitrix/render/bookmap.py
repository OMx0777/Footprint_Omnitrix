"""Bookmap render items, driven by a `BookmapBuffer`:

    BookHeatmapItem  — resting-liquidity field (time × price), auto-normalised.
    BBOItem          — stepped best-bid (blue) / best-ask (red) lines.
    BubbleItem       — trade bubbles, radius ∝ √size, red=sell / pale=buy.
    DomLadderItem    — right-edge depth histogram of the latest book + numbers.
    VolumeBarsItem   — bottom per-column executed-volume bars + numbers.

x = absolute column bucket (from the buffer), y = price.
"""

from __future__ import annotations

import math
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import (QColor, QPainter, QFont, QPen, QBrush, QRadialGradient,
                         QImage)

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

# The empty book is #1A2226 — a very dark blue-grey, not pure black. Sampled
# from several genuinely empty regions of the capture; the quantised mode of the
# whole field agrees at #181824. Pure black made the uncovered area read as a
# hole punched in the chart.
BOOKMAP_BG = "#1A2226"


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
        (0.00, (26, 34, 38)),      # #1A2226 — empty book (== background)
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


_pb_memo: tuple = (None, None)


def _price_bounds(cols, tick):
    # A refresh hands the *same* column list to every item, so without a memo
    # this full-buffer scan runs once per item per frame.
    global _pb_memo
    last = cols[-1]
    key = (id(cols), len(cols), cols[0].bucket, last.bucket, id(last.book), tick)
    if _pb_memo[0] == key:
        return _pb_memo[1]

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
    _pb_memo = (key, rect)
    return rect


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
        vb = self.getViewBox()
        if vb is None or not self.cols:
            return 0, 0
        xr = vb.viewRange()[0]
        return xr[0] - 1, xr[1] + 1


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
        x_lo, x_hi = self._xrange()

        vis = [c for c in self.cols if x_lo <= c.bucket <= x_hi and c.book]
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
        x_lo, x_hi = self._xrange()
        # Width 2: the bid/ask pair has to read as a spread *channel* over a
        # bright heatmap, and a hairline disappears against white/amber walls.
        for side, color in (("bid_ti", BID_LINE), ("ask_ti", ASK_LINE)):
            p.setPen(pg.mkPen(color, width=2))
            prev = None
            for c in self.cols:
                if not (x_lo <= c.bucket <= x_hi):
                    prev = None
                    continue
                ti = getattr(c, side)
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
        # (x_display, price, buy, sell) of everything drawn last frame, for the
        # window's hover readout. Without this a bubble can be seen but not
        # interrogated, and "how much of that was buying?" is the whole question.
        self.drawn: list[tuple] = []
        self.setZValue(0)

    def _cells(self) -> dict:
        """(x_bin, price_bucket) -> [buy, sell] over the visible tape."""
        if self.buffer is None or not self.buffer.trades:
            return {}
        x_lo, x_hi = self._xrange()
        xs = self.xscale
        inv = 1.0 / max(1e-9, self.bin_cols)
        rt = max(1, int(self.row_ticks))
        cells: dict[tuple, list] = {}
        # Scan newest-first and stop once past the left edge: the tape holds up
        # to 60k prints and walking all of them every frame dominated the frame
        # time. The slack lets a slightly out-of-order print still be found.
        for x, ti, size, aggr in reversed(self.buffer.trades):
            xd = x * xs
            if xd < x_lo - _TRADE_SCAN_SLACK:
                break
            if xd > x_hi:
                continue
            key = (int(math.floor(x * inv)), ti // rt)
            e = cells.get(key)
            if e is None:
                e = cells[key] = [0, 0]
            if aggr.value == "sell":
                e[1] += size
            else:
                e[0] += size
        return cells

    def _binned(self) -> list[tuple]:
        """[(x_display, price, buy, sell, total)] largest last, capped."""
        cells = self._cells()
        if not cells:
            return []
        rt = max(1, int(self.row_ticks))
        bc, xs, tick = self.bin_cols, self.xscale, self.tick
        out = []
        for (xb, tb), (b, s) in cells.items():
            tot = b + s
            if tot < self.min_size or tot <= 0:
                continue
            x = (xb + 0.5) * bc * xs                 # centre of the time bin
            price = (tb + 0.5) * rt * tick           # centre of the price bucket
            out.append((x, price, b, s, tot))
        if not out:
            return []
        out.sort(key=lambda t: t[4])                 # big drawn last / on top
        if len(out) > self.max_cells:
            # A dense tape yields well over a thousand cells in view. Drawing
            # them all is both the frame cost and a wall of tiny circles that
            # buries the prints worth seeing - keep the largest. The size scale
            # comes from what survives, so those still read against each other.
            out = out[-self.max_cells:]
        return out


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
    def _sphere(p: QPainter, pt: QPointF, r: float, base: QColor) -> None:
        """A Bookmap volume dot: a shaded sphere with a top-left highlight.

        Measured, not assumed. A radial cut through a bubble in the reference
        capture gives luminance 30 -> 83 -> 150 -> 96 -> 92 across the diameter,
        with the peak offset from centre — that is a lit sphere. A flat disc
        would be constant. Slight translucency lets overlapping prints build up
        without hiding the liquidity field behind them.
        """
        if r < 2.5:
            # Below a few pixels the gradient is not resolvable, and building one
            # per bubble on a dense tape is pure cost.
            p.setBrush(QBrush(base))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(pt, r, r)
            return
        grad = QRadialGradient(pt.x() - r * 0.35, pt.y() - r * 0.4, r * 1.5)
        hi = base.lighter(160)
        grad.setColorAt(0.0, QColor(min(255, hi.red()), min(255, hi.green()),
                                    min(255, hi.blue()), 245))
        grad.setColorAt(0.45, QColor(base.red(), base.green(), base.blue(), 225))
        dk = base.darker(180)
        grad.setColorAt(1.0, QColor(dk.red(), dk.green(), dk.blue(), 215))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(dk.red(), dk.green(), dk.blue(), 230), 0.6))
        p.drawEllipse(pt, r, r)


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
        avg = sum(book.values()) / len(book)
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
        cells = self._cells()
        if not cells:
            return []
        rt = max(1, int(self.row_ticks))
        bins: dict[int, list] = {}          # xb -> [buy, sell, price*vol]
        for (xb, tb), (b, s) in cells.items():
            price = (tb + 0.5) * rt * self.tick
            e = bins.get(xb)
            if e is None:
                e = bins[xb] = [0, 0, 0.0]
            e[0] += b; e[1] += s; e[2] += price * (b + s)
        out = []
        for xb, (b, s, pv) in bins.items():
            tot = b + s
            if tot < self.min_size or tot <= 0:
                continue
            x = (xb + 0.5) * self.bin_cols * self.xscale
            out.append((x, pv / tot, b, s, tot))
        out.sort(key=lambda t: t[4])
        if len(out) > self.max_cells:
            out = out[-self.max_cells:]
        return out

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
        if not self.cols:
            return
        x_lo, x_hi = self._xrange()
        tr = p.transform()
        for c in self.cols:
            if not (x_lo <= c.bucket <= x_hi) or c.vol == 0:
                continue
            net = sum(c.buy.values()) - sum(c.sell.values())
            color = BID_LINE if net >= 0 else ASK_LINE
            p.fillRect(QRectF(c.bucket + 0.28, 0, 0.44, c.vol),
                       QColor(color.red(), color.green(), color.blue(), 225))
            rp = tr.map(QPointF(c.bucket + 0.5, c.vol))
            p.save(); p.resetTransform()
            p.setFont(self.font)
            p.setPen(pg.mkPen("#AEB4C0"))
            p.drawText(QRectF(rp.x() - 14, rp.y() - 14, 28, 12),
                       Qt.AlignmentFlag.AlignCenter, str(c.vol))
            p.restore()
