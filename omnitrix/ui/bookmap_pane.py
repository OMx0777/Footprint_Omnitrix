"""One symbol's Bookmap, as a self-contained unit.

Extracted for the same reason ChartPane was: so one window can show one, two or
four order books side by side instead of the user tiling four separate windows.

Each pane owns its own plot widget - and therefore its own QGraphicsScene - so
mouse routing, the crosshair and the hover readout are scoped structurally
rather than by hit-testing which of four charts the pointer is over.

Every pane reports its paint against the WINDOW's frame-budget key, so a 2x2
bookmap grid is budgeted as one window, which is what it is. That matters more
here than on the footprint side: the heat field is the most expensive thing
this application draws.
"""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel

from .framegov import GovernedPlotWidget
from ..engine import BookmapBuffer, SRTracker
from ..render import (
    BookHeatmapItem, BBOItem, BubbleItem, PieItem, BarsItem, ProjectionItem,
    DomLadderItem, VolumeBarsItem, SRLinesItem,
)
from ..render.bookmap import BOOKMAP_BG as BG
from ..render.pricegrid import auto_step_ticks, TARGET_PX_BAND
from ..render.crosshair import Crosshair, clock_label


class BookmapPane:
    """The order book for one symbol. Owns its widget; the window owns layout."""

    def __init__(self, win, gov_key: int, buffer: BookmapBuffer, tick: float,
                 index: int, time_axis_cls):
        self.win = win
        self.index = index
        self.buffer = buffer
        self.tick = tick
        self.agg = 1
        self.bubble_bin = 1.0
        self.row_ticks = 1
        # Must agree with the step combo's default item: the handler is
        # connected after addItems, so selecting the default never fires it and
        # nothing else would sync this. The first refresh resolves the grid.
        self.auto_step = True
        self._follow = True
        self._auto_y = True
        self.proj_width = 7
        self.style = "Bubbles"
        self._y_range = None
        self._TimeAxisSecs = time_axis_cls

        # Per-pane header: symbol picker and the resolved price grid, so a grid
        # of books says which is which without clicking through them.
        self.container = QWidget()
        self.container.setObjectName("omnipane")
        _v = QVBoxLayout(self.container)
        _v.setContentsMargins(1, 1, 1, 1)
        _v.setSpacing(0)
        self.header = QWidget()
        _h = QHBoxLayout(self.header)
        _h.setContentsMargins(6, 2, 6, 2)
        _h.setSpacing(6)
        self.sym_combo = QComboBox()
        self.sym_combo.setEditable(True)
        self.sym_combo.setMinimumWidth(86)
        self.sym_combo.setToolTip("Symbol for THIS book")
        _h.addWidget(self.sym_combo)
        self.lbl_step = QLabel("")
        self.lbl_step.setStyleSheet("color:#8A93A6;font-weight:600;")
        _h.addWidget(self.lbl_step)
        _h.addStretch(1)
        self.header.setVisible(False)
        _v.addWidget(self.header)

        self._build(gov_key)
        _v.addWidget(self.glw, 1)

    def _build(self, gov_key: int) -> None:
        self.glw = GovernedPlotWidget(gov_key=gov_key)
        self.glw.setBackground(BG)

        self.main = self.glw.addPlot(row=0, col=0)
        self.main.showAxis("right"); self.main.hideAxis("left")
        self.main.hideAxis("bottom")
        # No grid on the liquidity pane: Bookmap keeps the canvas clean so the
        # heat field is the only thing carrying colour. Gridlines over a black
        # background read as banding in the thin-liquidity tail.
        self.main.showGrid(x=False, y=False)
        vb = self.main.getViewBox()
        vb.setMouseMode(pg.ViewBox.PanMode)          # left-drag pans
        vb.setMouseEnabled(x=True, y=True)

        self.dom = self.glw.addPlot(row=0, col=1)
        self.dom.hideAxis("left"); self.dom.showAxis("right")
        self.dom.hideAxis("bottom")
        self.dom.setYLink(self.main)
        self.dom.setMouseEnabled(x=False, y=False)

        self.vol_axis = self._TimeAxisSecs(orientation="bottom", win=self)
        self.vol = self.glw.addPlot(row=1, col=0, axisItems={"bottom": self.vol_axis})
        self.vol.hideAxis("left"); self.vol.showAxis("right")
        self.vol.setXLink(self.main)
        self.vol.setMouseEnabled(y=False)

        self.glw.ci.layout.setRowStretchFactor(0, 5)
        self.glw.ci.layout.setRowStretchFactor(1, 1)
        self.glw.ci.layout.setColumnStretchFactor(0, 14)
        self.glw.ci.layout.setColumnStretchFactor(1, 1)

        for plot in (self.main, self.dom, self.vol):
            for ax in ("right", "bottom"):
                a = plot.getAxis(ax)
                a.setPen(pg.mkPen("#243040")); a.setTextPen(pg.mkPen("#8A93A6"))

        self.heat = BookHeatmapItem(self.tick)
        self.bbo = BBOItem(self.tick)
        self.bubbles = BubbleItem(self.tick, self.buffer)
        self.main.addItem(self.heat)
        self.main.addItem(self.bbo)
        self.main.addItem(self.bubbles)

        self.pie = PieItem(self.tick, self.buffer)
        self.bars = BarsItem(self.tick, self.buffer)
        self.main.addItem(self.pie)
        self.main.addItem(self.bars)

        self.bubbles.min_size = 100          # default noise filter (matches combo)
        self.pie.min_size = 100
        self.bars.min_size = 100
        self.style = "Bubbles"               # Bookmap-style volume dots
        self.bubbles.setVisible(True)
        self.bars.setVisible(False)
        self.pie.setVisible(False)

        self.dom_item = DomLadderItem(self.tick)
        self.dom.addItem(self.dom_item)
        self.vol_item = VolumeBarsItem(self.tick)
        self.vol.addItem(self.vol_item)

        self.cursor = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen("#E8C13A", width=1))
        self.main.addItem(self.cursor, ignoreBounds=True)
        self.price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#D8DCE4", width=1, style=Qt.PenStyle.DashLine),
            label="{value:.2f}",
            labelOpts={"position": 0.98, "color": "#0A0E16",
                       "fill": "#D8DCE4", "movable": False})
        self.main.addItem(self.price_line, ignoreBounds=True)

        # resting limit orders projected as fat bands just ahead of price
        # (heatmap-coloured; strongest support GREEN, resistance RED).
        self.projection = ProjectionItem(self.tick)
        self.main.addItem(self.projection)
        self.proj_width = 7

        # Absolute support / resistance: the levels that have actually held,
        # drawn full width so price can be watched approaching them.
        self.sr = SRTracker()
        self.sr_item = SRLinesItem(self.tick)
        # Hidden to match the toolbar default. A checkbox that starts unchecked
        # only emits toggled on CHANGE, so leaving the item visible here would
        # show S/R lines the menu says are off - and the two would stay out of
        # step until the user clicked it twice.
        self.sr_item.setVisible(False)
        self.main.addItem(self.sr_item)

        # ---- crosshair with live price / time / liquidity readout ----
        self.cx_v = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen("#7E8AA0", width=1,
                                                 style=Qt.PenStyle.DashLine))
        self.cx_h = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#7E8AA0", width=1, style=Qt.PenStyle.DashLine),
            label="{value:.2f}",
            labelOpts={"position": 0.02, "color": "#0A0E16",
                       "fill": "#7E8AA0", "movable": False})
        for ln in (self.cx_v, self.cx_h):
            ln.setVisible(False)
            self.main.addItem(ln, ignoreBounds=True)

        self.readout = pg.TextItem(anchor=(0, 0), color="#D8DCE4",
                                   fill=pg.mkBrush(12, 24, 40, 215))
        self.readout.setZValue(50)
        self.readout.setVisible(False)
        self.main.addItem(self.readout, ignoreBounds=True)


        # Badges only: this pane already owns its crosshair lines and a rich
        # hover readout, so a second set of lines would fight the first.
        self.xhair = Crosshair(
            self.main,
            x_label=lambda x: clock_label(x * self.buffer.col_dt * self.agg),
            add_lines=False, connect=False)

    # ---- price grid ------------------------------------------------------
    def set_row_ticks(self, rt: int) -> bool:
        """Push one price grid to every consumer. Returns True if it changed.

        Five items draw on this grid - the heat field, the DOM ladder and the
        three tape overlays - and they must agree: a ladder bucketed differently
        from the field behind it lines up with nothing, which is worse than
        either grid alone.
        """
        if rt == self.row_ticks:
            return False
        self.row_ticks = rt
        self.heat.row_ticks = rt
        self.dom_item.row_ticks = rt
        self.apply_tape()
        return True

    def apply_tape(self) -> None:
        """Push the tape binning onto all three overlays at once."""
        for it in (self.bubbles, self.pie, self.bars):
            it.row_ticks = self.row_ticks
            it.bin_cols = self.bubble_bin
            it.xscale = 1.0 / self.agg
            it.update()

    def resolve_auto_step(self) -> bool:
        """Pick the grid from the current zoom. No-op unless Auto is selected.

        Resolved here rather than inside each item's paint (as the footprint
        does) precisely because five items share this grid - letting each derive
        its own from its own viewport would let the DOM ladder and the field
        disagree. The DOM y-axis is linked to the main plot, so one reading
        serves both.
        """
        if not self.auto_step:
            return False
        vb = self.main.getViewBox()
        if vb is None:
            return False
        px_h = vb.viewPixelSize()[1]
        changed = self.set_row_ticks(
            auto_step_ticks(px_h, self.tick, TARGET_PX_BAND))
        px = self.row_ticks * self.tick
        self.lbl_step.setText(f"({px * 100:.0f}\u00a2)" if px < 1.0
                              else f"(${px:,.2f})".replace(".00", ""))
        return changed

    # ---- view ------------------------------------------------------------
    def view_width(self, cols, default: int) -> float:
        try:
            r = self.main.getViewBox().viewRange()[0]
            w = r[1] - r[0]
            return w if w > 2 else default
        except Exception:
            return default

    def fit_price(self, cols) -> None:
        """Follow the traded price path and pad it with ~16 ticks of context
        each side, so resting walls above/below (support/resistance) stay in
        frame without the deep book shrinking the pies."""
        width = int(self.view_width(cols, default=60))
        vis = cols[-width:] if len(cols) > width else cols
        lo = hi = None
        for c in vis:
            if c.bid_ti is not None and c.ask_ti is not None:
                m = (c.bid_ti + c.ask_ti) / 2
                lo = m if lo is None else min(lo, m)
                hi = m if hi is None else max(hi, m)
        if lo is None:                       # fallback: full book range
            for c in vis:
                for ti in (c.book or {}):
                    lo = ti if lo is None else min(lo, ti)
                    hi = ti if hi is None else max(hi, ti)
            if lo is None:
                return
        pad = max(16.0, (hi - lo) * 0.6) * self.tick
        y0, y1 = lo * self.tick - pad, hi * self.tick + pad
        # Hysteresis: re-fitting on every refresh made the chart micro-jitter as
        # price wobbled. Only move the view when it drifts meaningfully.
        prev = self._y_range
        if prev is not None:
            span = max(1e-9, prev[1] - prev[0])
            if (abs(y0 - prev[0]) / span < 0.04 and
                    abs(y1 - prev[1]) / span < 0.04):
                return
        self._y_range = (y0, y1)
        self.main.setYRange(y0, y1, padding=0)

    def set_active_look(self, active: bool, multi: bool) -> None:
        """Mark which pane the toolbar and drawing tools are acting on.

        The selector is scoped by objectName on purpose. A bare "border:..."
        stylesheet set on the container is inherited by every child, so the
        symbol picker, the timeframe picker and the readout in the header each
        drew their OWN border too - which is what boxed in the buttons.
        "#omnipane { ... }" matches this widget alone.

        Thin and grey, both states. A 2 px teal frame around the selected chart
        was louder than anything on the chart itself; the selected pane only has
        to be identifiable, not advertised.
        """
        self.header.setVisible(multi)
        if not multi:
            # Single chart: nothing to distinguish it from, so no border at all.
            self.container.setStyleSheet("")
            return
        self.container.setStyleSheet(
            "#omnipane { border:1px solid %s; }"
            % ("#6E747E" if active else "#242830"))
