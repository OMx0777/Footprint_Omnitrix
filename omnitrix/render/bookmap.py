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

# Same ramp packed as opaque 0xAARRGGBB, for compositing the field as an image.
_LUT_ARGB = np.array(
    [(0xFF000000 | (c.red() << 16) | (c.green() << 8) | c.blue()) for c in _LUT],
    dtype=np.uint32)


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

        # rows span only the visible price window
        yr = vb.viewRange()[1]
        ti_lo = int(math.floor(yr[0] / tick)) - 1
        ti_hi = int(math.ceil(yr[1] / tick)) + 1
        nrows = ti_hi - ti_lo + 1
        if nrows <= 0 or nrows > self.MAX_ROWS:
            return
        b0, b1 = vis[0].bucket, vis[-1].bucket
        ncols = b1 - b0 + 1
        if ncols <= 0:
            return

        vmax = 1
        for c in vis:
            m = max(c.book.values())
            if m > vmax:
                vmax = m
        # Logarithmic scale (as real Bookmap): ordinary resting size reads as
        # quiet navy texture while walls saturate to white/amber/red.
        denom = math.log1p(vmax) or 1.0

        buf = np.zeros((nrows, ncols), dtype=np.uint32)   # 0 = transparent

        def _band(book, x0: int, x1: int) -> None:
            """Paint one ladder across image columns [x0, x1)."""
            if book is None or x1 <= x0:
                return
            n = len(book)
            if not n:
                return
            ks = np.fromiter(book.keys(), dtype=np.int64, count=n) - ti_lo
            vs = np.fromiter(book.values(), dtype=np.float64, count=n)
            m = (ks >= 0) & (ks < nrows) & (vs > 0)
            if not m.any():
                return
            norm = np.clip(np.log1p(vs[m]) / denom, 0.0, 1.0) ** self.gamma
            idx = (norm * 255.0).astype(np.int32)
            np.clip(idx, 0, 255, out=idx)
            col = np.zeros(nrows, dtype=np.uint32)
            col[ks[m]] = _LUT_ARGB[idx]
            buf[:, x0:x1] = col[:, None]        # one ladder, tiled across time

        # A resting order stays on the ladder until a later sweep replaces it,
        # so its band must be unbroken across time. The buffer only forward-fills
        # when a column is *created*, which means any second that received no
        # trade and no sweep has no column at all — and the field rendered as
        # vertical stripes with black gutters between them, nothing like the
        # continuous heat field of the real product. Carry the last known ladder
        # forward over those gaps here.
        #
        # Runs are flushed in blocks keyed on book *identity*, which the buffer's
        # share-don't-copy discipline guarantees for consecutive columns, so this
        # is also fewer numpy calls than the per-column version it replaces.
        by_bucket = {c.bucket: c for c in vis}
        run_book = None
        run_start = 0
        for x in range(ncols):
            c = by_bucket.get(b0 + x)
            if c is not None and c.book is not run_book:
                _band(run_book, run_start, x)
                run_book, run_start = c.book, x
        _band(run_book, run_start, ncols)

        self._buf = buf                    # QImage does not own the buffer
        img = QImage(buf.data, ncols, nrows, ncols * 4,
                     QImage.Format.Format_ARGB32)
        if self.alpha < 255:
            p.setOpacity(self.alpha / 255.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # Row 0 maps to the rect's top edge, i.e. the *lowest* price, which is
        # the order the rows were filled in (ti - ti_lo).
        p.drawImage(QRectF(b0, ti_lo * tick - tick / 2, ncols, nrows * tick), img)
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


class BubbleItem(_BufItem):
    def __init__(self, tick: float, buffer=None):
        super().__init__(tick)
        self.buffer = buffer
        self.xscale = 1.0            # 1/agg — aligns bubbles with aggregated columns
        self.min_r = 3.0
        self.max_r = 26.0
        self.min_size = 0           # hide clustered prints below this (noise filter)
        self.cluster_bins = 3.0     # time bins per display column for clustering
        self.max_bubbles = 320      # cap on bubbles drawn per frame (see paint)
        self.setZValue(0)

    def paint(self, p: QPainter, *args) -> None:
        if self.buffer is None:
            return
        trades = self.buffer.trades
        if not trades:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tick = self.tick
        xs = self.xscale
        x_lo, x_hi = self._xrange()

        # ---- cluster prints into (time-bin, price) cells (Bookmap volume-dots
        # clustering): consolidates the swarm of tiny circles into meaningful
        # bubbles, then a min-size threshold removes the rest of the noise. ----
        cb = self.cluster_bins
        cells: dict[tuple, list] = {}     # (xbin, ti) -> [buy, sell]
        # Scan newest-first and stop once we are past the left edge: the tape
        # holds up to 60k prints and walking all of them every frame dominated
        # the frame time. The slack lets a print that arrived slightly out of
        # order still be found before the scan gives up.
        for x, ti, size, aggr in reversed(trades):
            xd = x * xs
            if xd < x_lo - _TRADE_SCAN_SLACK:
                break
            if xd > x_hi:
                continue
            key = (round(xd * cb), ti)
            e = cells.get(key)
            if e is None:
                e = cells[key] = [0, 0]
            if aggr.value == "sell":
                e[1] += size
            else:
                e[0] += size
        if not cells:
            return

        agg = [(xb / cb, ti, b + s, b >= s) for (xb, ti), (b, s) in cells.items()
               if (b + s) >= self.min_size]
        if not agg:
            return

        # draw largest last so big prints sit on top
        agg.sort(key=lambda t: t[2])
        if len(agg) > self.max_bubbles:
            # A dense tape yields well over a thousand clustered cells in view.
            # Drawing them all is both the frame-time cost and the wall of tiny
            # circles that buries the prints worth seeing - so keep the largest
            # and drop the tail. The size scale is taken from what survives, so
            # the ones that remain still read relative to each other.
            agg = agg[-self.max_bubbles:]
        smax = agg[-1][2] or 1

        tr = p.transform()
        p.resetTransform()
        for xd, ti, total, is_buy in agg:
            pt = tr.map(QPointF(xd, ti * tick))
            r = self.min_r + (self.max_r - self.min_r) * math.sqrt(total / smax)
            base = BUY_BUBBLE if is_buy else SELL_BUBBLE
            self._sphere(p, pt, r, base)

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


class PieItem(_BufItem):
    """One pie per time column at the column's volume-weighted price, split into
    a blue (buy) and red (sell) wedge — reads the aggression ratio at a glance,
    with no overlapping circles."""

    def __init__(self, tick: float):
        super().__init__(tick)
        self.min_size = 0
        self.min_r = 7.0
        self.max_r = 30.0
        self.setZValue(0)

    def paint(self, p: QPainter, *args) -> None:
        if not self.cols:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tick = self.tick
        x_lo, x_hi = self._xrange()

        data = []
        for c in self.cols:
            if not (x_lo <= c.bucket <= x_hi):
                continue
            tb = sum(c.buy.values())
            ts = sum(c.sell.values())
            tot = tb + ts
            if tot == 0 or tot < self.min_size:
                continue
            num = den = 0
            for ti, v in c.buy.items():
                num += ti * v; den += v
            for ti, v in c.sell.items():
                num += ti * v; den += v
            if den == 0:
                continue
            data.append((c.bucket + 0.5, num / den, tb, ts, tot))
        if not data:
            return
        smax = max(d[4] for d in data) or 1
        data.sort(key=lambda d: d[4])          # big pies on top

        tr = p.transform()
        # One pie per column, so a pie wider than its column necessarily
        # collides with its neighbours - which is the one thing this mode exists
        # to avoid. Cap the radius at just under half a column's on-screen width
        # so the row stays a clean sequence at any zoom level.
        colw = abs(tr.map(QPointF(1.0, 0.0)).x() - tr.map(QPointF(0.0, 0.0)).x())
        rmax = max(1.5, min(self.max_r, colw * 0.46))
        rmin = min(self.min_r, rmax)

        p.resetTransform()
        for x, vti, tb, ts, tot in data:
            pt = tr.map(QPointF(x, vti * tick))
            r = rmin + (rmax - rmin) * math.sqrt(tot / smax)
            self._glossy_pie(p, pt, r, tb / tot)

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


class BarsItem(_BufItem):
    """One vertical split-bar per column at its VWAP price: blue segment ∝ buy
    volume, red ∝ sell volume — a compact non-overlapping alternative."""

    def __init__(self, tick: float):
        super().__init__(tick)
        self.min_size = 0
        self.setZValue(0)

    def paint(self, p: QPainter, *args) -> None:
        if not self.cols:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tick = self.tick
        x_lo, x_hi = self._xrange()
        rows = []
        for c in self.cols:
            if not (x_lo <= c.bucket <= x_hi):
                continue
            tb = sum(c.buy.values()); ts = sum(c.sell.values()); tot = tb + ts
            if tot == 0 or tot < self.min_size:
                continue
            num = den = 0
            for ti, v in c.buy.items():
                num += ti * v; den += v
            for ti, v in c.sell.items():
                num += ti * v; den += v
            if den:
                rows.append((c.bucket, num / den, tb, ts, tot))
        if not rows:
            return
        smax = max(r[4] for r in rows) or 1
        for bucket, vti, tb, ts, tot in rows:
            y = vti * tick
            h = tick * (0.6 + 6.0 * (tot / smax))          # bar half-height in price
            frac = tb / tot
            p.fillRect(QRectF(bucket + 0.2, y, 0.6, h * frac), QBrush(PIE_BUY))
            p.fillRect(QRectF(bucket + 0.2, y - h * (1 - frac), 0.6, h * (1 - frac)),
                       QBrush(PIE_SELL))


class DomLadderItem(pg.GraphicsObject):
    """Right-edge depth histogram of one Column's book, with size numbers."""

    def __init__(self, tick: float):
        super().__init__()
        self.col = None
        self.tick = tick
        self.vmax = 1                 # right edge the bars are anchored to
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._bounds = QRectF()

    def set_col(self, col, tick=None) -> None:
        self.prepareGeometryChange()
        self.col = col
        if tick is not None:
            self.tick = tick
        if col and col.book:
            lo = min(col.book) * self.tick
            hi = max(col.book) * self.tick
            self.vmax = max(col.book.values()) or 1
            self._bounds = QRectF(0, lo - self.tick, self.vmax,
                                  (hi - lo) + 2 * self.tick)
        else:
            self.vmax = 1
            self._bounds = QRectF()
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, *args) -> None:
        col = self.col
        if col is None or not col.book:
            return
        tick = self.tick
        mx = self.vmax
        ask_ti = col.ask_ti if col.ask_ti is not None else 10 ** 12
        tr = p.transform()

        # Bars are anchored at the right edge and grow *left*, so every level's
        # magnitude is read against the price axis it belongs to - the same way
        # a real DOM ladder is laid out. Solid (alpha 230) rather than washed
        # out, so the ladder holds its own next to the heatmap.
        for ti, size in col.book.items():
            color = ASK_LINE if ti >= ask_ti else BID_LINE
            c = QColor(color.red(), color.green(), color.blue(), 230)
            p.fillRect(QRectF(mx - size, ti * tick - tick / 2, size, tick), c)

        p.setFont(self.font)
        # Light text: it sits over the bar on wide levels and over the dark
        # background on thin ones, and stays readable on both.
        p.setPen(pg.mkPen("#EAEEF5"))
        for ti, size in col.book.items():
            rp = tr.map(QPointF(mx, ti * tick))
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
