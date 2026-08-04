"""Microstructure Analytics: four stacked, time-aligned panes derived from the
L2 book and tick-by-tick prints.

  1. Book Imbalance   resting bid vs ask depth (-1..+1)  — L2 pressure
  2. Resting Depth    absolute bid / ask depth curves    — liquidity supply
  3. Spread           bid/ask width in ticks             — liquidity stress
  4. Tape Speed + CVD volume per column & cumulative delta — aggression
"""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QMainWindow, QToolBar, QLabel, QComboBox

import time

from ..engine import metrics
from ..render.crosshair import Crosshair, clock_label
from .bookmap_window import TF

BG = "#0B0E14"


class AnalyticsWindow(QMainWindow):
    def __init__(self, buffer, tick: float, parent=None):
        super().__init__(parent)
        self.buffer = buffer
        self.tick = tick
        self.agg = 1
        self.setWindowTitle(f"Omnitrix Analytics — {buffer.symbol}")
        self.resize(1420, 900)

        pg.setConfigOptions(useOpenGL=False, antialias=True)
        self._build_toolbar()
        self._build_plots()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(250)
        self.refresh()

    def _build_toolbar(self) -> None:
        tb = QToolBar(); tb.setMovable(False); self.addToolBar(tb)
        tb.addWidget(QLabel("  Timeframe "))
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(list(TF))
        self.tf_combo.currentTextChanged.connect(self._on_tf)
        tb.addWidget(self.tf_combo)
        self.lbl = QLabel("   ")
        tb.addWidget(self.lbl)
        self.setStyleSheet(
            f"QMainWindow{{background:{BG};}}"
            "QToolBar{background:#12161F;border:none;padding:4px;spacing:6px;}"
            "QLabel{color:#C7CCD6;font-size:13px;font-weight:600;}"
            "QComboBox{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:3px 8px;}")

    def _build_plots(self) -> None:
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(BG)
        self.setCentralWidget(self.glw)

        titles = ["Book Imbalance  (bid vs ask depth)", "Resting Depth",
                  "Spread (ticks)", "Tape Speed / CVD"]
        self.plots = []
        for i, t in enumerate(titles):
            pl = self.glw.addPlot(row=i, col=0)
            pl.showAxis("right"); pl.hideAxis("left")
            pl.setTitle(t, color="#8A93A6", size="10pt")
            pl.showGrid(x=True, y=True, alpha=0.12)
            if i:
                pl.setXLink(self.plots[0])
            if i < len(titles) - 1:
                pl.hideAxis("bottom")
            for ax in ("right", "bottom"):
                a = pl.getAxis(ax)
                a.setPen(pg.mkPen("#2A3140")); a.setTextPen(pg.mkPen("#8A93A6"))
            self.plots.append(pl)

        # Every pane shares the x axis, so each gets its own readout: the y
        # value differs per pane (imbalance, depth, ticks, volume) and reading
        # it off the axis by eye is exactly what a crosshair is for.
        self.xhairs = [
            Crosshair(pl,
                      x_label=lambda x: clock_label(
                          x * self.buffer.col_dt * self.agg),
                      price_fmt="{:,.2f}")
            for pl in self.plots]

        p_imb, p_depth, p_spread, p_speed = self.plots

        self.imb_curve = pg.PlotDataItem(pen=pg.mkPen("#42A5F5", width=1.5),
                                         fillLevel=0,
                                         brush=pg.mkBrush(66, 165, 245, 70))
        p_imb.addItem(self.imb_curve)
        p_imb.addItem(pg.InfiniteLine(angle=0, pos=0, movable=False,
                                      pen=pg.mkPen("#555", style=Qt.PenStyle.DashLine)),
                      ignoreBounds=True)
        p_imb.setYRange(-1, 1, padding=0)

        self.bid_curve = pg.PlotDataItem(pen=pg.mkPen("#26A69A", width=1.4))
        self.ask_curve = pg.PlotDataItem(pen=pg.mkPen("#EF5350", width=1.4))
        p_depth.addItem(self.bid_curve); p_depth.addItem(self.ask_curve)

        # plain line, no fill — spread sits in a narrow band, so a fill to zero
        # would flood the pane and hide the variation
        self.spread_curve = pg.PlotDataItem(pen=pg.mkPen("#FFB300", width=1.4))
        p_spread.addItem(self.spread_curve)

        self.speed_curve = pg.PlotDataItem(pen=pg.mkPen("#9C7BFF", width=1.2),
                                           fillLevel=0,
                                           brush=pg.mkBrush(156, 123, 255, 60))
        p_speed.addItem(self.speed_curve)
        self.cvd_curve = pg.PlotDataItem(pen=pg.mkPen("#FFFFFF", width=1.6))
        self.cvd_vb = pg.ViewBox()
        p_speed.scene().addItem(self.cvd_vb)
        p_speed.getAxis("right").linkToView(self.cvd_vb)
        self.cvd_vb.setXLink(p_speed)
        self.cvd_vb.addItem(self.cvd_curve)
        p_speed.vb.sigResized.connect(self._sync_cvd)

    def _sync_cvd(self) -> None:
        self.cvd_vb.setGeometry(self.plots[3].vb.sceneBoundingRect())
        self.cvd_vb.linkedViewChanged(self.plots[3].vb, self.cvd_vb.XAxis)

    def closeEvent(self, event) -> None:
        # The overlay ViewBox was added straight to the scene, so it is not a
        # child of any plot and would outlive the window.
        self._timer.stop()
        try:
            self.glw.scene().removeItem(self.cvd_vb)
        except Exception:
            pass
        super().closeEvent(event)

    def _on_tf(self, txt: str) -> None:
        self.agg = TF.get(txt, 1)
        self.refresh()

    def refresh(self) -> None:
        # A window you cannot see does not need live data. Every one of these
        # runs its own timer and repaints regardless of whether it is on
        # screen, so four open Bookmaps cost four full paints even when three
        # are minimised behind the fourth. Measured: paint is 95% of the cost
        # (98.5 ms of 104 ms at four windows), so skipping an unseen one is the
        # cheapest frame in the app.
        if not self.isVisible() or self.isMinimized():
            return
        cols = self.buffer.view(self.agg)
        if not cols:
            return
        cols = cols[-400:]                       # keep the panes responsive

        # One split serves both panes; computing it twice was half this
        # window's cost.
        xs2, bids, asks = metrics.depth_sides(cols)
        xs, ys = metrics.imbalance_from(xs2, bids, asks)
        self.imb_curve.setData(xs, ys)
        self.bid_curve.setData(xs2, bids)
        self.ask_curve.setData(xs2, asks)

        xs3, sp = metrics.spread_ticks(cols)
        self.spread_curve.setData(xs3, sp)
        if sp:
            self.plots[2].setYRange(0, max(sp) + 1, padding=0)

        xs4, vol = metrics.intensity(cols)
        self.speed_curve.setData(xs4, vol)
        xs5, cvd = metrics.cvd_series(cols)
        self.cvd_curve.setData(xs5, cvd)
        self._sync_cvd()

        if ys:
            imb = ys[-1]
            bias = "BID" if imb > 0.05 else ("ASK" if imb < -0.05 else "FLAT")
            self.lbl.setText(
                f"   Imbalance {imb:+.2f} ({bias})   "
                f"Spread {sp[-1] if sp else 0}t   "
                f"CVD {cvd[-1] if cvd else 0:+,}")
