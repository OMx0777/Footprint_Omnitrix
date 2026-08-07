"""The footprint / cluster GraphicsObject.

Consumes engine `Bar` objects directly and leans on their *cached* analytics
(POC, value area) so a frame's cost is drawing, not recomputation. Only the
bars inside the current viewport are drawn.

x axis  = bar index (0..n-1)
y axis  = price
Each bar draws a thin candlestick on the left, then a two-column volume block
(sell | buy) per traded price level.
"""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import QFont, QColor, QPainter, QFontMetrics

from .theme import Theme, DARK
from .pricegrid import AUTO_STEPS, TARGET_PX_LABELLED, step_ticks


class FootprintItem(pg.GraphicsObject):
    BOX_W = 0.66                      # column block width in x-units
    CANDLE_GAP = 0.06                 # gap between candle and block
    # Half-width of the candle body, in x-units. The body sits ON the split
    # between the sell and buy histograms, so it has to be narrow enough that
    # both sides remain readable and wide enough to read as a candle.
    CANDLE_HW = 0.055
    # Vertical padding inside a row, as a fraction of row height. A hairline
    # between rows is what makes a stack of bars read as a histogram rather
    # than as one solid block.
    ROW_INSET = 0.12
    # Narrower than this (screen px across the whole block) and no cell label
    # can fit, so skip the text pass entirely rather than emit clipped digits.
    MIN_LABEL_PX = 26.0
    # Shared with the Bookmap price grid so the two views cannot drift into
    # different ideas of what a step means - see render/pricegrid.py.
    AUTO_TARGET_PX = TARGET_PX_LABELLED
    AUTO_STEPS = AUTO_STEPS

    def __init__(self, tick: float, theme: Theme = DARK):
        super().__init__()
        self.bars: list = []
        self.tick = tick
        self.theme = theme
        self.mode = "Footprint"       # Footprint | Cluster | Profile | Delta
        self.show_imbalance = True
        self.show_va = True
        self.show_candles = True
        self.draw_cells = True        # False = candles only (heatmap-only view)
        self.imbalance_factor = 3.0
        self.min_imbalance_vol = 20
        self.stacked_min = 3
        self.va_pct = 0.70
        self.font = QFont("Consolas", 8, QFont.Weight.Bold)
        # User toggle for every number on the chart (cells, profile/delta
        # values, and the per-bar delta/volume footer). Some readers want the
        # shapes only.
        self.show_numbers = True
        # Price aggregation for the footprint grid, as a PRICE (dollars), not a
        # tick count - "10c" has to mean 10c on any instrument, and converting
        # through the symbol's tick keeps that true when the tick is not a cent.
        # 0.0 = Auto: pick from the zoom so rows stay readable while panning.
        self.price_step = 0.0
        self._fm = QFontMetrics(self.font)
        self._bounds = QRectF()
        self.step_ticks = 1               # last step used; for the UI readout

    # ---- external setters ------------------------------------------------
    def set_bars(self, bars: list) -> None:
        self.bars = bars
        self._recompute_bounds()
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.update()

    def set_show_imbalance(self, on: bool) -> None:
        self.show_imbalance = on
        self.update()

    def set_show_va(self, on: bool) -> None:
        self.show_va = on
        self.update()

    def set_imbalance_factor(self, f: float) -> None:
        self.imbalance_factor = f
        self.update()

    def set_draw_cells(self, on: bool) -> None:
        self.draw_cells = on
        self.update()

    def set_show_candles(self, on: bool) -> None:
        self.show_candles = on
        self.update()

    def configure(self, **kw) -> None:
        """Bulk-set customization attributes from a settings dict."""
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.update()

    # ---- geometry --------------------------------------------------------
    def _recompute_bounds(self) -> None:
        # Must precede any boundingRect() change or Qt culls against the stale
        # rect and the item flickers.
        self.prepareGeometryChange()
        if not self.bars:
            self._bounds = QRectF()
            return
        lo = min(b.low for b in self.bars)
        hi = max(b.high for b in self.bars)
        self._bounds = QRectF(-1, lo - 1.0, len(self.bars) + 2, (hi - lo) + 2.0)

    def boundingRect(self) -> QRectF:
        return self._bounds

    # ---- painting --------------------------------------------------------
    def paint(self, p: QPainter, *args) -> None:
        if not self.bars:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        t = self.theme
        tick = self.tick
        half = self.BOX_W / 2

        vb = self.getViewBox()
        if vb is None:
            return
        px_w, px_h = vb.viewPixelSize()
        # Label only when a number will actually FIT, vertically AND
        # horizontally. Testing row height alone was why numbers appeared
        # half-drawn: a row can be tall enough while its column is far too
        # narrow, and Qt then clips the string mid-digit rather than dropping
        # it. Everything downstream re-checks the exact string against the exact
        # rect, so a wide value is omitted instead of being cut in half.
        self._fm = QFontMetrics(self.font)
        box_px = self.BOX_W / max(px_w, 1e-12)

        step = self._step_ticks(px_h)
        self.step_ticks = step
        row_h = step * tick
        # Gate on the DRAWN row, not the raw tick: aggregating is precisely what
        # buys the room for the numbers, so a folded grid must be allowed to
        # label itself where a 1-tick grid could not.
        show_text = (self.show_numbers
                     and px_h < row_h * 0.72
                     and box_px >= self.MIN_LABEL_PX)

        xr = vb.viewRange()[0]
        x_lo = max(0, int(xr[0]) - 1)
        x_hi = min(len(self.bars), int(xr[1]) + 2)

        c_bull = QColor(t.bull)
        c_bear = QColor(t.bear)
        # Pens, brushes and theme colours built ONCE per frame, not per bar and
        # not per cell. Profiling a 150-bar view found 2,250 mkPen and 3,000
        # mkColor calls a frame - rebuilding identical objects cost as much as
        # drawing the footprint itself.
        pens = {}
        for cc in (c_bull, c_bear):
            pens[(cc.name(), 2)] = pg.mkPen(cc.lighter(118), width=2)
            pens[(cc.name(), 1)] = pg.mkPen(cc, width=1)
        brushes = {cc.name(): pg.mkBrush(cc) for cc in (c_bull, c_bear)}
        self._body_pen = pg.mkPen(QColor(t.bg), width=1)
        pal = {
            "poc_bg": QColor(t.poc_bg), "bid_bg": QColor(t.bid_bg),
            "ask_bg": QColor(t.ask_bg), "bull": QColor(t.bull),
            "bear": QColor(t.bear), "va_line": QColor(t.va_line),
            "sell_bar": QColor(t.sell_bar), "buy_bar": QColor(t.buy_bar),
        }

        # ONE BASELINE FOR EVERY FOOTER IN THE VIEW.
        #
        # Each footer used to hang under its OWN bar's low, so on a trending
        # chart they landed at wildly different heights and a bar's delta sat
        # in the middle of its neighbour's cells. That is the overlap: not a
        # font or a margin, an anchor that moves per bar. Anchored to the
        # lowest low on screen they form one clean row, which is also how
        # every chart worth copying draws its volume.
        base_y = None
        if show_text and x_hi > x_lo:
            lows = [self.bars[i].low for i in range(x_lo, x_hi)]
            if lows:
                base_y = min(lows) - self.tick

        for x in range(x_lo, x_hi):
            bar = self.bars[x]
            cc = c_bull if bar.is_bull else c_bear
            if self.draw_cells and bar.has_cells():
                # Fold onto the drawn grid first, so POC, value area and the
                # diagonal imbalances all describe the rows on screen.
                self._paint_block(p, x, bar.aggregated(step), half, row_h,
                                  show_text, pal, base_y)
            # AFTER the cells: the candle is the thing you read first, so it
            # goes on top of its own volume rather than under it.
            if self.show_candles:
                self._paint_candle(p, x, bar, cc, half, tick,
                                   pens[(cc.name(), 2)], pens[(cc.name(), 1)],
                                   brushes[cc.name()])

    def _step_ticks(self, px_h: float) -> int:
        """Ticks per drawn footprint row (`price_step` <= 0 selects auto)."""
        return step_ticks(self.price_step, self.tick, px_h,
                          self.AUTO_TARGET_PX)

    def _paint_candle(self, p, x, bar, color, half, tick,
                      pen2, pen1, brush) -> None:
        """The candle sits at the CENTRE of its column, not beside it.

        The footprint then reads as one object: the candle down the middle
        with its sell volume growing left and its buy volume growing right,
        the way a profile grows from its axis. Drawn AFTER the cells so the
        body stays legible over them.
        """
        cx = float(x)
        p.setPen(pen2)
        p.drawLine(QPointF(cx, bar.low), QPointF(cx, bar.high))
        top = max(bar.open, bar.close)
        bot = min(bar.open, bar.close)
        if top == bot:
            top += tick / 8
        # OUTLINED IN THE BACKGROUND COLOUR. The body now sits ON TOP of its
        # own histogram, in the same hue family, so without a separating edge
        # it disappears into the volume behind it - the candle stops being the
        # thing you read first, which is the whole point of centring it.
        p.setBrush(brush)
        p.setPen(self._body_pen)
        p.drawRect(QRectF(cx - self.CANDLE_HW, bot,
                          self.CANDLE_HW * 2, top - bot))

    def _paint_block(self, p, x, bar, half, row_h, show_text, pal,
                     base_y=None) -> None:
        """`bar` is already folded onto the drawn grid; its cell keys are BUCKET
        indices and one row spans `row_h` in price."""
        t = self.theme
        # Sealed bars have no dict - see Bar.arrays(). Boxing once here is the
        # same cost the dict iteration used to be, and every cell is drawn
        # individually anyway.
        _ti, _sell, _buy = bar.arrays()
        cells = list(zip(_ti.tolist(), _sell.tolist(), _buy.tolist()))
        poc = bar.poc
        vah, val = bar.value_area(self.va_pct)
        mode = self.mode

        buy_imb, sell_imb = (
            bar.imbalances(self.imbalance_factor, self.min_imbalance_vol)
            if self.show_imbalance and mode == "Footprint" else (set(), set())
        )

        # value-area wash + VAH/VAL guides
        if self.show_va and vah is not None:
            y_lo = val * row_h - row_h / 2
            y_hi = vah * row_h + row_h / 2
            p.fillRect(QRectF(x - half, y_lo, self.BOX_W, y_hi - y_lo), t.va_wash)
            p.setPen(pg.mkPen(t.va_line, width=1, style=Qt.PenStyle.DashLine))
            for edge in (y_hi, y_lo):
                p.drawLine(QPointF(x - half, edge), QPointF(x + half, edge))

        # scaling references for Profile / Delta modes
        max_tot = max((s + b for _t, s, b in cells), default=1) or 1
        # One scale for both wings, so a row with 900 buys and 100 sells reads
        # as lopsided rather than as two full-width blocks.
        max_side = max((max(s, b) for _t, s, b in cells), default=1) or 1
        max_abs_d = max((abs(b - s) for _t, s, b in cells), default=1) or 1

        tr = p.transform()
        # Skip rows that are off screen. Qt clipped them anyway, so the picture
        # is unchanged - but the Python loop, the QRectF and the fillRect call
        # all happened first. A bar holds every price it traded at; a zoomed-in
        # view shows a fraction of them.
        vb = self.getViewBox()
        y_min, y_max = vb.viewRange()[1] if vb is not None else (-1e18, 1e18)
        for ti, sell_v, buy_v in cells:
            tot = sell_v + buy_v
            if tot == 0:
                continue
            y = ti * row_h - row_h / 2
            if y > y_max or y + row_h < y_min:
                continue
            is_poc = ti == poc

            if mode == "Footprint":
                # A HISTOGRAM EITHER SIDE OF THE CANDLE, not two filled boxes.
                #
                # Full-width boxes made every row the same size, so the shape
                # of the auction was carried only by colour and by numbers too
                # small to read at a glance. Scaling each side by its own
                # volume turns the column into what it actually is - a profile
                # split by aggressor, growing outward from the candle - and the
                # heavy rows are then visible without reading a single digit.
                ws = half * (sell_v / max_side)
                wb = half * (buy_v / max_side)
                c_sell = t.sell_imb if ti in sell_imb else pal["sell_bar"]
                c_buy = t.buy_imb if ti in buy_imb else pal["buy_bar"]
                if is_poc:
                    c_sell = c_buy = pal["poc_bg"]
                inset = row_h * self.ROW_INSET
                yy, hh = y + inset, max(row_h - 2 * inset, row_h * 0.4)
                if ws > 0:
                    p.fillRect(QRectF(x - ws, yy, ws, hh), c_sell)
                if wb > 0:
                    p.fillRect(QRectF(x, yy, wb, hh), c_buy)
                if show_text:
                    self._cell_two(p, tr, x, y, row_h, half, sell_v, buy_v,
                                   t.poc_text if is_poc else t.cell_text,
                                   ws, wb)

            elif mode == "Cluster":
                bg = pal["poc_bg"] if is_poc else (
                    t.ask_bg if bar.is_bull else t.bid_bg)
                p.fillRect(QRectF(x - half, y, self.BOX_W, row_h), bg)
                if show_text:
                    self._cell_one(p, tr, x, y, row_h, half, _fmt(tot),
                                   t.poc_text if is_poc else t.cell_text)

            elif mode == "Profile":
                w = self.BOX_W * (tot / max_tot)
                col = pal["bull"] if buy_v >= sell_v else pal["bear"]
                if is_poc:
                    col = pal["va_line"]
                p.fillRect(QRectF(x - half, y, w, row_h), col)
                if show_text:
                    self._cell_one(p, tr, x, y, row_h, half, _fmt(tot),
                                   t.cell_text, align_left=True)

            elif mode == "Delta":
                d = buy_v - sell_v
                inten = min(1.0, abs(d) / max_abs_d)
                base = pal["bull"] if d >= 0 else pal["bear"]
                col = QColor(base.red(), base.green(), base.blue(),
                             int(60 + 195 * inten))
                p.fillRect(QRectF(x - half, y, self.BOX_W, row_h), col)
                if show_text:
                    self._cell_one(p, tr, x, y, row_h, half,
                                   f"{'+' if d > 0 else ''}{_fmt(d)}", t.cell_text)

        if self.show_imbalance and mode == "Footprint":
            self._paint_stacks(p, x, row_h, half, sorted(buy_imb),
                               sorted(sell_imb))

        if show_text:
            # Footer sits just under the bar's low, so it is offset by a real
            # tick - scaling that by the price step would push it far off at $1.
            self._paint_footer(p, tr, x, bar, half, self.tick, base_y)

    def _fits(self, rect: QRectF, text: str) -> bool:
        """Does `text` fit inside `rect` (screen px) without being clipped?

        Qt happily draws a partial glyph when the rect is too small, which is
        exactly the "numbers show up half" symptom. One space of padding each
        side keeps adjacent columns from reading as a single run of digits.
        """
        fm = self._fm
        return (rect.width() >= fm.horizontalAdvance(text) + 2
                and rect.height() >= fm.height() - 2)

    def _cell_two(self, p, tr, x, y, row_h, half, sell_v, buy_v, color,
                  ws=None, wb=None) -> None:
        # Numbers sit against the OUTER end of their own bar, never over the
        # candle in the middle. Given the bar widths they follow the histogram
        # out; without them (other modes) they fall back to the half-column.
        lw = half if ws is None else max(ws, 0.0)
        rw = half if wb is None else max(wb, 0.0)
        rb = tr.mapRect(QRectF(x - max(lw, 0.02), y, max(lw, 0.02) - 0.01, row_h))
        ra = tr.mapRect(QRectF(x + 0.01, y, max(rw, 0.02) - 0.01, row_h))
        s_txt, b_txt = _fmt(sell_v), _fmt(buy_v)
        s_ok, b_ok = self._fits(rb, s_txt), self._fits(ra, b_txt)
        if not (s_ok or b_ok):
            return
        p.save()
        p.resetTransform()
        p.setFont(self.font)
        p.setPen(pg.mkPen(color))
        if s_ok:
            p.drawText(rb, Qt.AlignmentFlag.AlignVCenter
                       | Qt.AlignmentFlag.AlignRight, s_txt)
        if b_ok:
            p.drawText(ra, Qt.AlignmentFlag.AlignVCenter
                       | Qt.AlignmentFlag.AlignLeft, b_txt)
        p.restore()

    def _cell_one(self, p, tr, x, y, row_h, half, text, color,
                  align_left=False) -> None:
        r = tr.mapRect(QRectF(x - half + 0.03, y, self.BOX_W - 0.06, row_h))
        if not self._fits(r, text):
            return
        p.save()
        p.resetTransform()
        p.setFont(self.font)
        p.setPen(pg.mkPen(color))
        align = (Qt.AlignmentFlag.AlignVCenter |
                 (Qt.AlignmentFlag.AlignLeft if align_left else Qt.AlignmentFlag.AlignHCenter))
        p.drawText(r, align, text)
        p.restore()

    def _paint_stacks(self, p, x, row_h, half, buy_sorted, sell_sorted) -> None:
        # Runs are consecutive BUCKET indices on the folded grid, so "stacked"
        # counts adjacent drawn rows - which is what the viewer is reading.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for a, b in _runs(buy_sorted, self.stacked_min):
            p.setPen(pg.mkPen(self.theme.buy_imb, width=2))
            p.drawRect(QRectF(x, a * row_h - row_h / 2, half,
                              (b - a) * row_h + row_h))
        for a, b in _runs(sell_sorted, self.stacked_min):
            p.setPen(pg.mkPen(self.theme.sell_imb, width=2))
            p.drawRect(QRectF(x - half, a * row_h - row_h / 2, half,
                              (b - a) * row_h + row_h))

    def _paint_footer(self, p, tr, x, bar, half, tick, base_y=None) -> None:
        t = self.theme
        # Anchored to the bar's low in screen space and stacked by real font
        # metrics. The old version offset by a hardcoded 10 px and 26 px, which
        # only lined up at one font size and one zoom - at others the delta
        # collided with the block above it or with the volume line below.
        fm = self._fm
        line = fm.height() + 1
        anchor = base_y if base_y is not None else (bar.low - tick)
        base = tr.map(QPointF(float(x), anchor)).y()
        w = tr.mapRect(QRectF(x - half, 0.0, self.BOX_W, tick)).width()
        cx = tr.map(QPointF(float(x), 0.0)).x()

        d = bar.delta
        rows = ((f"Δ {'+' if d > 0 else ''}{_fmt(d)}",
                 t.delta_up if d >= 0 else t.delta_dn),
                (_fmt(bar.volume), t.cell_text))
        p.save()
        p.resetTransform()
        p.setFont(self.font)
        for i, (text, colour) in enumerate(rows):
            if fm.horizontalAdvance(text) + 2 > w:
                continue                       # omit rather than overlap
            r = QRectF(cx - w / 2, base + i * line, w, line)
            p.setPen(pg.mkPen(colour))
            p.drawText(r, Qt.AlignmentFlag.AlignTop
                       | Qt.AlignmentFlag.AlignHCenter, text)
        p.restore()


def _fmt(v: int) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if a >= 1000:
        return f"{v/1000:.1f}K"
    return str(v)


def _runs(sorted_idxs, min_len):
    """Group sorted ints into consecutive runs of length >= min_len."""
    out = []
    if not sorted_idxs:
        return out
    start = prev = sorted_idxs[0]
    for v in sorted_idxs[1:]:
        if v == prev + 1:
            prev = v
        else:
            if prev - start + 1 >= min_len:
                out.append((start, prev))
            start = prev = v
    if prev - start + 1 >= min_len:
        out.append((start, prev))
    return out
