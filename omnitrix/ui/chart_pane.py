"""One ticker's footprint chart, as a self-contained unit.

Extracted so the main window can show one, two or four of them side by side.
Everything that belongs to a single chart lives here: the price and CVD plots,
their axes, the footprint and heatmap items, the overlays, the crosshair and
the drawing surface.

WHY EACH PANE OWNS ITS OWN PLOT WIDGET. pyqtgraph can put several plots in one
GraphicsLayoutWidget, and that was the obvious way to build a grid - but every
plot in a layout shares ONE QGraphicsScene, so `sigMouseMoved` fires for all of
them and every handler has to hit-test which plot the pointer is over. Drawing
tools, the crosshair and the rubber-band preview would each need that routing,
and any one of them getting it wrong puts a drawing on the wrong chart. A
widget per pane gives each its own scene, so the routing is structural instead
of conditional.

The cost is one QGraphicsView per pane. That is measured and accounted for: all
panes report their paint against the MAIN WINDOW's frame-budget key, so four
charts in a grid are budgeted as one window, which is what they are.
"""

from __future__ import annotations

import math
import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QGraphicsRectItem, QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
)

from .framegov import GovernedPlotWidget
from ..render.crosshair import safe_localtime
from ..render import (
    FootprintItem, HeatmapItem, TimeAxis, PriceAxis, Crosshair,
    EMAItem, CPRItem, ExecutionMarkersItem,
)


class ChartPane:
    """The chart for one symbol. Owns its widget; the window owns the layout."""

    def __init__(self, win, gov_key: int, theme, instruments, index: int):
        self.win = win
        self.index = index
        self.theme = theme
        self.instruments = instruments
        # Empty means "follow the toolbar's symbol". Only panes 1..3 in a grid
        # carry their own, so a single-pane layout behaves exactly as before.
        self.symbol = ""
        # Tick the live-price tag is currently formatted for. -1 so the first
        # sync always runs.
        self._tag_tick = -1.0
        # Timeframe is PER PANE: the point of a grid is comparing the same or
        # different names on different horizons at once - a 10s footprint next
        # to a 5m one - so a single window-wide timeframe would defeat it.
        self.tf_s = 60
        # False until the user picks a timeframe for THIS chart. A pane that
        # has never been set follows the one you are looking at when it first
        # appears, so opening a 2x2 grid gives four charts on the timeframe you
        # were already using rather than three silently on the default.
        self.tf_explicit = False
        self._needs_center = True
        self.auto_scroll = True
        self.auto_y = True

        # The pane is a container, not just a plot: in a grid each chart needs
        # its own symbol and mode pickers, the way every multi-chart terminal
        # does it. The header hides itself in single-chart mode, where the main
        # toolbar already says the same thing and a second copy is just noise.
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
        self.sym_combo.setToolTip("Symbol for THIS chart")
        _h.addWidget(self.sym_combo)
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(120)
        self.mode_combo.setToolTip("Chart type for THIS chart")
        _h.addWidget(self.mode_combo)
        self.tf_combo = QComboBox()
        self.tf_combo.setMinimumWidth(58)
        self.tf_combo.setToolTip("Timeframe for THIS chart")
        _h.addWidget(self.tf_combo)
        self.lbl_last = QLabel("")
        self.lbl_last.setStyleSheet("color:#8A93A6;font-weight:600;")
        _h.addWidget(self.lbl_last)
        _h.addStretch(1)
        self.header.setVisible(False)
        _v.addWidget(self.header)

        self.glw = GovernedPlotWidget(gov_key=gov_key)
        _v.addWidget(self.glw, 1)

        self.price_time_axis = TimeAxis(orientation="bottom")
        self.price_axis = PriceAxis(
            orientation="right",
            tick_fn=lambda: self.instruments.tick(self.symbol or "QQQ"))
        self.price_plot = self.glw.addPlot(
            row=0, col=0, axisItems={"bottom": self.price_time_axis,
                                     "right": self.price_axis})
        self.price_plot.showAxis("right")
        self.price_plot.hideAxis("left")
        self.price_plot.hideAxis("bottom")     # CVD pane carries it by default
        self.price_plot.showGrid(x=True, y=True, alpha=0.25)

        self.time_axis = TimeAxis(orientation="bottom")
        self.cvd_plot = self.glw.addPlot(row=1, col=0,
                                         axisItems={"bottom": self.time_axis})
        self.cvd_plot.showAxis("right")
        self.cvd_plot.hideAxis("left")
        self.cvd_plot.showGrid(x=True, y=True, alpha=0.2)
        self.cvd_plot.setXLink(self.price_plot)
        self.glw.ci.layout.setRowStretchFactor(0, 4)
        self.glw.ci.layout.setRowStretchFactor(1, 1)

        tick = instruments.tick("QQQ")
        self.heatmap = HeatmapItem(tick)
        self.heatmap.setVisible(False)
        self.price_plot.addItem(self.heatmap)

        self.fp = FootprintItem(tick, theme)
        self.price_plot.addItem(self.fp)

        self.exec_item = ExecutionMarkersItem()
        self.price_plot.addItem(self.exec_item)

        self.cpr_item = CPRItem()
        self.cpr_item.setVisible(False)
        self.price_plot.addItem(self.cpr_item)

        self.ema9_item = EMAItem(period=9, color=QColor(33, 150, 243))
        self.ema21_item = EMAItem(period=21, color=QColor(255, 193, 7))
        self.ema9_item.setVisible(False)
        self.ema21_item.setVisible(False)
        self.price_plot.addItem(self.ema9_item)
        self.price_plot.addItem(self.ema21_item)

        self.vwap_curve = pg.PlotDataItem(pen=pg.mkPen(theme.vwap, width=2))
        self.price_plot.addItem(self.vwap_curve)

        # VWAP standard-deviation bands (+/-1 sigma, +/-2 sigma).
        self.vwap_bands = []
        for mult, alpha, dash in ((1, 150, Qt.PenStyle.DashLine),
                                  (2, 90, Qt.PenStyle.DotLine)):
            for _ in range(2):                    # upper + lower
                c = pg.PlotDataItem(pen=pg.mkPen(theme.vwap, width=1, style=dash))
                c.setOpacity(alpha / 255.0)
                self.price_plot.addItem(c)
                self.vwap_bands.append((mult, c))

        self.cvd_curve = pg.PlotDataItem(pen=pg.mkPen(theme.cvd, width=2))
        self.cvd_plot.addItem(self.cvd_curve)
        self.cvd_zero = pg.InfiniteLine(
            angle=0, pos=0, movable=False,
            pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.cvd_plot.addItem(self.cvd_zero, ignoreBounds=True)

        # The live price, with the value in a tag against the right axis - the
        # bookmap has had one and the footprint charts did not, so on the
        # footprint you could see WHERE price was but had to read it off the
        # axis gradations. Anchored at the right edge so it sits where the
        # axis labels are and reads as one of them, but filled, so the live
        # price is the one number on the axis that stands out.
        self.price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(theme.cvd, width=1, style=Qt.PenStyle.DashLine),
            label="{value:,.2f}",
            labelOpts={"position": 0.985, "color": theme.bg, "fill": theme.cvd,
                       "movable": False,
                       "anchors": [(1.0, 0.5), (1.0, 0.5)]})
        self.price_plot.addItem(self.price_line, ignoreBounds=True)
        self.vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.price_plot.addItem(self.vline, ignoreBounds=True)
        self.price_plot.addItem(self.hline, ignoreBounds=True)

        self.xhair = Crosshair(self.price_plot, x_label=self._time_at,
                               add_lines=False, connect=False)

        # Rubber band shown between the two creation clicks of a drawing.
        self.preview = QGraphicsRectItem()
        self.preview.setPen(pg.mkPen("#5C9DFF", width=1,
                                     style=Qt.PenStyle.DashLine))
        self.preview.setBrush(pg.mkBrush(92, 157, 255, 26))
        self.preview.setZValue(80)
        self.preview.setVisible(False)
        self.price_plot.addItem(self.preview)

        # Drawings belong to the pane they were drawn on, so switching layout
        # or active pane never moves or orphans them.
        self.drawing_items: list = []

        # CVD starts HIDDEN. It is a secondary study, and reserving a fifth of
        # every chart for it by default costs the price pane exactly that much
        # - four times over in a 2x2 grid. Turn it on per chart from Overlays.
        self.set_cvd_visible(False)

    # ---- per-chart settings ---------------------------------------------
    def sync_price_tag(self) -> None:
        """Match the live-price tag's decimals to this instrument's tick.

        Two decimals is right for most names and wrong for the ones that are
        not - a sub-penny instrument would show every price as the same
        rounded number, and a whole-dollar future would show a trailing ".00"
        that never changes. Called from the redraw, so it early-outs on the
        tick it already formatted for rather than rebuilding a format string
        several times a second.
        """
        tick = self.instruments.tick(self.symbol or "QQQ")
        if tick == self._tag_tick:
            return
        self._tag_tick = tick
        d = 2
        if tick > 0:
            d = max(0, min(8, -int(math.floor(math.log10(tick)))))
        lbl = getattr(self.price_line, "label", None)
        if lbl is not None:
            lbl.setFormat("{value:,.%df}" % d)

    def set_cvd_visible(self, on: bool) -> None:
        self.cvd_plot.setVisible(on)
        # The time axis lives on the bottom-most pane, so hiding CVD took the
        # whole time scale with it. Hand it up to the price chart instead.
        if on:
            self.price_plot.hideAxis("bottom")
        else:
            self.price_plot.showAxis("bottom")
        # Collapse the row too: hiding the plot alone leaves its band reserved,
        # so the price chart does not reclaim the space.
        self.glw.ci.layout.setRowStretchFactor(1, 1 if on else 0)
        self.glw.ci.layout.setRowMinimumHeight(1, 0)

    def set_mode(self, fp_mode: str, draw_cells: bool, hm_visible: bool) -> None:
        self.fp.set_mode(fp_mode)
        self.fp.set_draw_cells(draw_cells)
        self.heatmap.setVisible(hm_visible)

    # ---- helpers ---------------------------------------------------------
    def _time_at(self, x: float) -> str:
        """Wall-clock label for a bar position, matching this pane's axis."""
        bars = self.time_axis._bars
        i = int(round(x))
        if not (0 <= i < len(bars)):
            return ""
        lt = safe_localtime(bars[i].start_ts)
        if lt is None:
            return ""
        fmt = "%H:%M:%S" if self.tf_s < 60 else "%d %b  %H:%M"
        return time.strftime(fmt, lt)

    def clear(self) -> None:
        """Draw nothing, honestly, rather than leave another symbol's bars up."""
        self.fp.set_bars([])
        self.heatmap.set_bars([])
        self.time_axis.set_bars([])
        self.price_time_axis.set_bars([])
        self.exec_item.set_data([], [])
        for item in (self.cpr_item, self.ema9_item, self.ema21_item):
            if item.isVisible():
                item.set_bars([])
        self.vwap_curve.setData([], [])
        self.cvd_curve.setData([], [])
        for _, curve in self.vwap_bands:
            curve.setData([], [])

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
