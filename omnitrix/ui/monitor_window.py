"""Market Monitor — a Bloomberg-style multi-instrument grid.

One row per tracked symbol with live price, session change, traded volume,
session delta / CVD, and the L2 microstructure reads (book imbalance, spread,
resting depth per side). Cells are colour-coded by sign so the whole book of
instruments can be scanned at a glance.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from .framegov import GOVERNOR, GovernedTimer, GovernedPlotWidget
from ..engine import metrics

COLS = ["Symbol", "Last", "Chg", "Chg %", "Volume", "Delta", "CVD",
        "Imbal", "Spread", "Bid Depth", "Ask Depth", "Trades"]

UP = QColor(38, 190, 160)
DOWN = QColor(239, 96, 96)
NEUTRAL = QColor(200, 205, 214)
BG = "#0B0E14"


def _fmt(v: float, dp: int = 0) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.{dp}f}"


class MarketMonitorWindow(QMainWindow):
    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app = app_window
        self.setWindowTitle("Omnitrix Market Monitor")
        self.resize(1180, 420)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.setFont(QFont("Consolas", 10))
        self.setCentralWidget(self.table)

        self.setStyleSheet(
            f"QMainWindow{{background:{BG};}}"
            "QTableWidget{background:#0F131C;alternate-background-color:#12161F;"
            " color:#C8CDD6;gridline-color:#232A36;border:none;}"
            "QHeaderView::section{background:#1A2130;color:#8FA0B6;"
            " padding:6px;border:none;font-weight:700;}")

        self._rows: dict[str, int] = {}
        self._timer = GovernedTimer(self, self.refresh, 500,
                                    priority=1)
        self._timer.start()
        self.refresh()

    def _cell(self, r: int, c: int, text: str, color=NEUTRAL, bold=False):
        it = self.table.item(r, c)
        if it is None:
            it = QTableWidgetItem()
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, c, it)
        it.setText(text)
        it.setForeground(color)
        if bold:
            f = it.font(); f.setBold(True); it.setFont(f)

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
        app = self.app
        for sym in sorted(app.series):
            if sym not in self._rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                self._rows[sym] = r
            self._update_row(sym, self._rows[sym])

    def _update_row(self, sym: str, r: int) -> None:
        app = self.app
        series = app.series.get(sym)
        # Session figures come from BarSeries' O(1) running accumulators. Calling
        # view() here re-aggregated each symbol's whole history twice a second —
        # with a 100-symbol basket that alone froze the GUI thread.
        if series is None or series.sess_open is None:
            return
        last = series.sess_last
        first = series.sess_open
        chg = last - first
        chg_pct = (chg / first * 100.0) if first else 0.0
        col = UP if chg > 0 else (DOWN if chg < 0 else NEUTRAL)

        volume = series.sess_volume
        # Delta = the live bar's flow (what is happening NOW); CVD = the session
        # cumulative. Both columns previously showed the same session figure,
        # which wasted a column and hid the recent-flow read entirely.
        delta = series.bars[-1].delta if series.bars else 0

        buf = app.bookmaps.get(sym)
        imb = spread = bid_d = ask_d = 0.0
        cvd = series.sess_delta          # session cumulative volume delta
        trades = 0
        if buf:
            # Only the newest column is needed for the L2 reads — no full view().
            latest = buf.latest_book()
            if latest is not None:
                b, a = metrics.sides(latest)
                bid_d, ask_d = b, a
                tot = b + a
                imb = ((b - a) / tot) if tot else 0.0
                if latest.bid_ti is not None and latest.ask_ti is not None:
                    spread = max(0, latest.ask_ti - latest.bid_ti)
            trades = len(buf.trades)

        self._cell(r, 0, sym, NEUTRAL, bold=True)
        self._cell(r, 1, f"{last:,.2f}", col, bold=True)
        self._cell(r, 2, f"{chg:+.2f}", col)
        self._cell(r, 3, f"{chg_pct:+.2f}%", col)
        self._cell(r, 4, _fmt(volume))
        self._cell(r, 5, f"{delta:+,}", UP if delta >= 0 else DOWN)
        self._cell(r, 6, f"{cvd:+,}", UP if cvd >= 0 else DOWN)
        self._cell(r, 7, f"{imb:+.2f}", UP if imb >= 0 else DOWN)
        self._cell(r, 8, f"{spread:.0f}t")
        self._cell(r, 9, _fmt(bid_d), UP)
        self._cell(r, 10, _fmt(ask_d), DOWN)
        self._cell(r, 11, _fmt(trades))
