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

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from .framegov import GovernedPlotWidget
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
        self._needs_center = True
        self.auto_scroll = True
        self.auto_y = True

        self.glw = GovernedPlotWidget(gov_key=gov_key)

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

        self.price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(theme.cvd, width=1, style=Qt.PenStyle.DashLine))
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

    # ---- helpers ---------------------------------------------------------
    def _time_at(self, x: float) -> str:
        """Wall-clock label for a bar position, matching this pane's axis."""
        bars = self.time_axis._bars
        i = int(round(x))
        if not (0 <= i < len(bars)):
            return ""
        lt = time.localtime(bars[i].start_ts)
        fmt = "%H:%M:%S" if self.win.tf_s < 60 else "%d %b  %H:%M"
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

        Only meaningful in a grid: with a single pane there is nothing to
        distinguish it from, and a border would just be noise.
        """
        if not multi:
            self.glw.setStyleSheet("")
            return
        self.glw.setStyleSheet(
            "border:2px solid #26A69A;" if active else "border:2px solid #232833;")
