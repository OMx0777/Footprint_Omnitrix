"""Market Profile window: TPO (time-price opportunity) beside the session
Volume Profile, sharing one price axis — the classic institutional read of
*where the market spent time* vs *where size actually traded*.
"""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QToolBar, QLabel, QComboBox, QPushButton,
)

from ..render import TPOItem, VolumeProfileItem

BG = "#0B0E14"


class ProfileWindow(QMainWindow):
    def __init__(self, profile, tick: float, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.tick = tick
        self.va_pct = 0.70
        self.setWindowTitle(f"Omnitrix Market Profile — {profile.symbol}")
        self.resize(1180, 900)

        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_toolbar()
        self._build_plots()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(400)
        self.refresh()

    def _build_toolbar(self) -> None:
        tb = QToolBar(); tb.setMovable(False); self.addToolBar(tb)
        tb.addWidget(QLabel("  Value area "))
        self.va_combo = QComboBox()
        self.va_combo.addItems(["60%", "68%", "70%", "80%"])
        self.va_combo.setCurrentText("70%")
        self.va_combo.currentTextChanged.connect(self._on_va)
        tb.addWidget(self.va_combo)
        tb.addWidget(QLabel("   TPO "))
        self.tpo_combo = QComboBox()
        self.tpo_combo.addItems(["Profile", "Split by bracket"])
        self.tpo_combo.currentTextChanged.connect(self._on_tpo_mode)
        tb.addWidget(self.tpo_combo)

        self.btn_fit = QPushButton("Fit")
        self.btn_fit.clicked.connect(self._fit)
        tb.addWidget(self.btn_fit)
        self.lbl = QLabel("   ")
        tb.addWidget(self.lbl)
        self.setStyleSheet(
            f"QMainWindow{{background:{BG};}}"
            "QToolBar{background:#12161F;border:none;padding:4px;spacing:6px;}"
            "QLabel{color:#C7CCD6;font-size:13px;font-weight:600;}"
            "QComboBox{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:3px 8px;}"
            "QPushButton{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:4px 10px;font-weight:600;}")

    def _build_plots(self) -> None:
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(BG)
        self.setCentralWidget(self.glw)

        self.tpo_plot = self.glw.addPlot(row=0, col=0)
        self.tpo_plot.hideAxis("left"); self.tpo_plot.showAxis("right")
        self.tpo_plot.setLabel("bottom", "TPO brackets (30m)")
        self.tpo_plot.showGrid(x=False, y=True, alpha=0.12)

        self.vp_plot = self.glw.addPlot(row=0, col=1)
        self.vp_plot.hideAxis("left"); self.vp_plot.showAxis("right")
        self.vp_plot.setLabel("bottom", "Volume @ price")
        self.vp_plot.showGrid(x=False, y=True, alpha=0.12)
        self.vp_plot.setYLink(self.tpo_plot)
        self.vp_plot.setMouseEnabled(x=False)

        self.glw.ci.layout.setColumnStretchFactor(0, 3)
        self.glw.ci.layout.setColumnStretchFactor(1, 2)

        for plot in (self.tpo_plot, self.vp_plot):
            for ax in ("right", "bottom"):
                a = plot.getAxis(ax)
                a.setPen(pg.mkPen("#2A3140")); a.setTextPen(pg.mkPen("#8A93A6"))

        self.tpo_item = TPOItem(self.tick)
        self.tpo_plot.addItem(self.tpo_item)
        self.vp_item = VolumeProfileItem(self.tick)
        self.vp_plot.addItem(self.vp_item)

        # POC / VAH / VAL guides on the TPO pane
        self.poc_line = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen("#FFC43C", width=2))
        self.vah_line = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen("#78BEFF", width=1,
                                                     style=Qt.PenStyle.DashLine))
        self.val_line = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen("#78BEFF", width=1,
                                                     style=Qt.PenStyle.DashLine))
        for ln in (self.poc_line, self.vah_line, self.val_line):
            self.tpo_plot.addItem(ln, ignoreBounds=True)

    def _on_va(self, txt: str) -> None:
        self.va_pct = float(txt.rstrip("%")) / 100.0
        self.refresh()

    def _on_tpo_mode(self, txt: str) -> None:
        self.tpo_item.set_collapsed(txt == "Profile")
        self.refresh()
        self._fit()

    def refresh(self) -> None:
        prof = self.profile
        a = prof.analytics(self.va_pct)
        rows = prof.tpo_rows()
        br = prof.bracket_range()
        if not rows or br is None:
            return
        self.tpo_item.set_data(rows, br[0], a["poc"], a["vah"], a["val"])
        self.vp_item.set_data(prof.buy, prof.sell, a["poc"], a["vah"],
                              a["val"], a["hvn"])
        if a["poc"] is not None:
            self.poc_line.setPos(a["poc"] * self.tick)
            self.vah_line.setPos(a["vah"] * self.tick)
            self.val_line.setPos(a["val"] * self.tick)
            self.lbl.setText(
                f"   POC {a['poc'] * self.tick:.2f}   "
                f"VAH {a['vah'] * self.tick:.2f}   "
                f"VAL {a['val'] * self.tick:.2f}   "
                f"Total {prof.total:,}")

    def _fit(self) -> None:
        a = self.profile.analytics(self.va_pct)
        tot = a["totals"]
        if not tot:
            return
        lo, hi = min(tot) * self.tick, max(tot) * self.tick
        pad = (hi - lo) * 0.04 or self.tick
        self.tpo_plot.setYRange(lo - pad, hi + pad, padding=0)
        w = self.tpo_item.boundingRect().width()
        if w > 0:
            self.tpo_plot.setXRange(0, w, padding=0)
        self.vp_plot.setXRange(0, 1.02, padding=0)
