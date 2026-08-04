"""Tape-reader window for high-speed scalping.

Three linked panes on one time axis:

  prints   every execution as a dot at its price, sized by volume, coloured by
           aggressor, blocks ringed
  speed    prints per bucket — the rate the tape is running at
  cvd      running cumulative delta over the visible window

Deliberately unaggregated. The bookmap answers "where is the liquidity"; this
answers "what is hitting it and how fast", and for that the sequence of prints
IS the signal — a run of twelve lifts reads nothing like one block of the same
size, and any binning destroys exactly that distinction.

The window auto-follows the live edge and holds a fixed number of seconds, so it
behaves like a tape rather than a chart you have to keep panning.
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QToolBar, QLabel, QComboBox, QCheckBox, QPushButton,
)

from ..engine.model import split_size
from ..render.tape import TapePrintsItem, TapeSpeedItem, TapeCvdItem, TAPE_BG
from ..render.crosshair import Crosshair, clock_label
from .framegov import GOVERNOR, GovernedTimer, GovernedPlotWidget

# label -> seconds held in view
SPANS = {"15s": 15, "30s": 30, "1m": 60, "2m": 120, "5m": 300, "10m": 600}
BLOCKS = {"1K": 1000, "2.5K": 2500, "5K": 5000, "10K": 10000, "25K": 25000}
SIZES = {"75%": 0.75, "100%": 1.0, "150%": 1.5, "200%": 2.0, "300%": 3.0}


class TapeWindow(QMainWindow):
    def __init__(self, buffer, tick: float, parent=None):
        super().__init__(parent)
        self.buffer = buffer
        self.tick = tick
        self.span = 60.0
        self._follow = True
        self.setWindowTitle(f"Omnitrix Tape — {buffer.symbol}")
        self.resize(1180, 820)

        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_toolbar()
        self._build_plots()

        self._timer = GovernedTimer(self, self.refresh, 60,
                                    priority=1)              # a tape has to feel immediate
        self._timer.start()
        self.refresh()

    # ---- toolbar ---------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel("  Window "))
        self.span_combo = QComboBox()
        self.span_combo.addItems(list(SPANS))
        self.span_combo.setCurrentText("1m")
        self.span_combo.currentTextChanged.connect(self._on_span)
        tb.addWidget(self.span_combo)

        tb.addWidget(QLabel("   Block ≥ "))
        self.block_combo = QComboBox()
        self.block_combo.addItems(list(BLOCKS))
        self.block_combo.setCurrentText("5K")
        self.block_combo.setToolTip("Ring prints at or above this size")
        self.block_combo.currentTextChanged.connect(self._on_block)
        tb.addWidget(self.block_combo)

        tb.addWidget(QLabel("   Size "))
        self.size_combo = QComboBox()
        self.size_combo.addItems(list(SIZES))
        self.size_combo.setCurrentText("100%")
        self.size_combo.currentTextChanged.connect(self._on_size)
        tb.addWidget(self.size_combo)

        self.chk_speed = QCheckBox("Speed")
        self.chk_speed.setChecked(True)
        self.chk_speed.toggled.connect(self._on_speed_pane)
        tb.addWidget(self.chk_speed)

        self.chk_cvd = QCheckBox("CVD")
        self.chk_cvd.setChecked(True)
        self.chk_cvd.toggled.connect(self._on_cvd_pane)
        tb.addWidget(self.chk_cvd)

        self.btn_follow = QPushButton("⏵ Follow")
        self.btn_follow.clicked.connect(self._reset_view)
        tb.addWidget(self.btn_follow)

        self.lbl_stats = QLabel("  ")
        tb.addWidget(self.lbl_stats)

        self.setStyleSheet(
            "QMainWindow{background:%s;}"
            "QToolBar{background:#0A0E14;border:none;padding:4px;spacing:4px;}"
            "QLabel{color:#C7CCD6;font-size:13px;font-weight:600;}"
            "QCheckBox{color:#C7CCD6;font-size:13px;font-weight:600;padding:0 6px;}"
            "QComboBox{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:3px 8px;font-size:13px;}"
            "QPushButton{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:4px 10px;font-weight:600;}"
            "QPushButton:hover{background:#263042;}" % TAPE_BG
        )

    # ---- plots -----------------------------------------------------------
    def _build_plots(self) -> None:
        self.glw = GovernedPlotWidget(gov_key=id(self))
        self.glw.setBackground(TAPE_BG)
        self.setCentralWidget(self.glw)

        self.main = self.glw.addPlot(row=0, col=0, axisItems={"bottom": _ClockAxis("bottom")})
        self.main.showAxis("right"); self.main.hideAxis("left")
        self.main.showGrid(x=False, y=True, alpha=0.12)
        self.main.getViewBox().setMouseMode(pg.ViewBox.PanMode)

        self.speed = self.glw.addPlot(row=1, col=0)
        self.speed.showAxis("right"); self.speed.hideAxis("left")
        self.speed.hideAxis("bottom")
        self.speed.setXLink(self.main)
        self.speed.setMouseEnabled(y=False)

        self.cvd = self.glw.addPlot(row=2, col=0)
        self.cvd.showAxis("right"); self.cvd.hideAxis("left")
        self.cvd.hideAxis("bottom")
        self.cvd.setXLink(self.main)
        self.cvd.setMouseEnabled(y=False)

        self.glw.ci.layout.setRowStretchFactor(0, 6)
        self.glw.ci.layout.setRowStretchFactor(1, 1)
        self.glw.ci.layout.setRowStretchFactor(2, 1)

        for plot in (self.main, self.speed, self.cvd):
            for ax in ("right", "bottom"):
                a = plot.getAxis(ax)
                a.setPen(pg.mkPen("#243040")); a.setTextPen(pg.mkPen("#8A93A6"))

        self.prints_item = TapePrintsItem()
        self.speed_item = TapeSpeedItem()
        self.cvd_item = TapeCvdItem()
        self.main.addItem(self.prints_item)
        self.speed.addItem(self.speed_item)
        self.cvd.addItem(self.cvd_item)

        self.cvd_zero = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen("#3A4456", width=1))
        self.cvd.addItem(self.cvd_zero, ignoreBounds=True)

        self.last_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#D8DCE4", width=1, style=Qt.PenStyle.DashLine),
            label="{value:.2f}",
            labelOpts={"position": 0.98, "color": "#0A0E16",
                       "fill": "#D8DCE4", "movable": False})
        self.main.addItem(self.last_line, ignoreBounds=True)

        # x is epoch seconds here, so the time badge is a direct clock format.
        self.xhair = Crosshair(
            self.main,
            x_label=clock_label)

        self.main.getViewBox().sigRangeChangedManually.connect(self._on_manual)

    # ---- data ------------------------------------------------------------
    def _window(self):
        """The visible tape only: ([(t_s, price, size, buy)], lo, hi, stats).

        The fourth element is the print's BUY SHARE, not a boolean. It used to
        be `is_buy = aggr.value != "sell"`, which made every UNKNOWN print a
        full buy - colouring it green and, worse, adding its whole size to the
        CVD line. A cumulative-delta curve biased upward by all unclassified
        volume is not a slow chart, it is a wrong one. Carrying the split lets
        the dots take a third, neutral colour and the CVD stay exact.

        Walks the deque BACKWARDS and stops at the window edge. Converting the
        whole 60,000-print tape every frame cost 790 ms against a 60 ms timer -
        the same mistake as rebuilding the heatmap's columns per frame. The tape
        holds far more history than any span shows, so the work has to be
        proportional to what is on screen, not to what is retained.

        The buffer stores x already divided by col_dt, so it is multiplied back
        into real seconds here: the tape's axis is wall clock, not column index.
        """
        trades = self.buffer.trades
        if not trades:
            return [], 0.0, 0.0, (0, 0, 0)
        dt = self.buffer.col_dt
        tick = self.tick
        hi = trades[-1][0] * dt
        lo = hi - self.span
        out = []
        vol = buy = 0
        for x, ti, size, aggr in reversed(trades):
            t = x * dt
            if t < lo:
                break
            b, _s = split_size(size, aggr, ti)
            out.append((t, ti * tick, size, b))
            vol += size
            buy += b
        out.reverse()
        return out, lo, hi, (len(out), vol, 2 * buy - vol)

    def refresh(self) -> None:
        # A window you cannot see does not need live data. Every one of these
        # runs its own timer and repaints regardless of whether it is on
        # screen, so four open Bookmaps cost four full paints even when three
        # are minimised behind the fourth. Measured: paint is 95% of the cost
        # (98.5 ms of 104 ms at four windows), so skipping an unseen one is the
        # cheapest frame in the app.
        if not self.isVisible() or self.isMinimized():
            GOVERNOR.set_alive(id(self), False)
            return
        GOVERNOR.set_alive(id(self), True)
        vis, lo, hi, (n, vol, delta) = self._window()
        if not vis:
            return
        self.prints_item.set_prints(vis, lo, hi)
        if self.speed.isVisible():
            self.speed_item.set_prints(vis, lo, hi)
        if self.cvd.isVisible():
            self.cvd_item.set_prints(vis, lo, hi)

        rate = n / self.span if self.span else 0.0
        self.lbl_stats.setText(
            f"   {n:,} prints   {rate:,.1f}/s   vol {vol:,}   Δ {delta:+,}   ")

        self.last_line.setPos(vis[-1][1])
        if self._follow:
            self.main.setXRange(lo, hi, padding=0)
            y0 = min(q[1] for q in vis)
            y1 = max(q[1] for q in vis)
            pad = max((y1 - y0) * 0.12, self.tick * 4)
            self.main.setYRange(y0 - pad, y1 + pad, padding=0)

    # ---- handlers --------------------------------------------------------
    def _on_manual(self):
        self._follow = False
        self.btn_follow.setText("⏸ Live")

    def _reset_view(self):
        self._follow = True
        self.btn_follow.setText("⏵ Follow")
        self.refresh()

    def _on_span(self, txt: str):
        self.span = float(SPANS.get(txt, 60))
        # Keep roughly 40 speed bars across the window whatever its length, so
        # the pane stays readable at 15 s and at 10 m.
        self.speed_item.bucket_s = max(0.25, self.span / 40.0)
        self._reset_view()

    def _on_block(self, txt: str):
        self.prints_item.block_size = BLOCKS.get(txt, 5000)
        self.prints_item.update()

    def _on_size(self, txt: str):
        self.prints_item.size_scale = SIZES.get(txt, 1.0)
        self.prints_item.update()

    def _on_speed_pane(self, on: bool):
        self.speed.setVisible(on)
        self.glw.ci.layout.setRowStretchFactor(1, 1 if on else 0)
        self.glw.ci.layout.setRowMinimumHeight(1, 0)

    def _on_cvd_pane(self, on: bool):
        self.cvd.setVisible(on)
        self.glw.ci.layout.setRowStretchFactor(2, 1 if on else 0)
        self.glw.ci.layout.setRowMinimumHeight(2, 0)


class _ClockAxis(pg.AxisItem):
    """Seconds-since-epoch -> HH:MM:SS."""

    def tickStrings(self, values, scale, spacing):
        return [time.strftime("%H:%M:%S", time.localtime(v)) for v in values]
