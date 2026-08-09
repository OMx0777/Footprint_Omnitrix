"""Session Statistics dock — the at-a-glance terminal readout for the active
symbol: OHLC/range, traded volume and delta, VWAP, profile levels (POC/VAH/VAL)
and tape composition (buy vs sell share, average and largest print).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QRectF
from .framegov import GovernedTimer, watch
from PyQt6.QtGui import QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget
from ..paintguard import safe_paint
from . import design

BG = QColor(8, 11, 17)
HEAD = QColor(143, 160, 182)
LABEL = QColor(150, 157, 170)
VALUE = QColor(214, 220, 230)
UP = QColor(38, 190, 160)
DOWN = QColor(239, 96, 96)
RULE = QColor(28, 34, 44)


def _fmt(v: float, dp: int = 0) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.{dp}f}"


class StatsPanel(QWidget):
    ROW_H = 19

    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app = app_window
        self.setMinimumWidth(232)
        self.f_head = design.font(design.DATA_STRONG)
        self.f_row = design.font(design.DATA)
        # Governed for the same reason as the signals panel: a side panel
        # must be throttleable when the chart is struggling, and its cost has
        # to be visible to the watchdog rather than landing in "unmarked".
        self._timer = GovernedTimer(self, self._tick, 400, priority=1)
        self._timer.start()

    def _tick(self) -> None:
        with watch("stats_panel"):
            self.update()

    # ---- data ------------------------------------------------------------
    def _collect(self) -> list:
        app = self.app
        sym = app.active_symbol
        if not sym or sym not in app.series:
            return []
        series = app.series[sym]
        if series.sess_open is None:
            return []
        tick = app.instruments.tick(sym)
        # O(1) session figures + the memoized VWAP series. This panel used to
        # walk every bar and every price cell four times a second, which at the
        # 10s timeframe is the whole history on a repeating timer.
        o = series.sess_open
        hi = series.sess_high
        lo = series.sess_low
        last = series.sess_last
        chg = last - o
        vol = series.sess_volume
        delta = series.sess_delta

        # vol = buy + sell and delta = buy - sell, so each side follows directly
        # (exact for UNKNOWN prints too — Bar.add splits them the same way).
        buy = (vol + delta) // 2
        sell = vol - buy
        vwap_series, _, _ = series.overlays(app.tf_s, tick)
        vwap = vwap_series[-1] if vwap_series else last
        buy_pct = (buy / vol * 100) if vol else 0

        rows = [
            ("SESSION", None),
            ("Open", f"{o:,.2f}"), ("High", f"{hi:,.2f}"),
            ("Low", f"{lo:,.2f}"), ("Last", f"{last:,.2f}"),
            ("Change", f"{chg:+.2f}"), ("Range", f"{hi - lo:,.2f}"),
            ("VWAP", f"{vwap:,.2f}"),
            ("FLOW", None),
            ("Volume", _fmt(vol)), ("Delta", f"{delta:+,}"),
            ("Buy vol", _fmt(buy)), ("Sell vol", _fmt(sell)),
            ("Buy share", f"{buy_pct:.1f}%"),
        ]

        prof = app.profiles.get(sym)
        if prof and prof.total:
            a = prof.analytics()
            if a["poc"] is not None:
                rows += [
                    ("PROFILE", None),
                    ("POC", f"{a['poc'] * tick:,.2f}"),
                    ("VAH", f"{a['vah'] * tick:,.2f}"),
                    ("VAL", f"{a['val'] * tick:,.2f}"),
                    ("Levels", f"{len(a['totals']):,}"),
                ]

        buf = app.bookmaps.get(sym)
        if buf and buf.trade_count:
            # O(1) running counters. Materialising the 60,000-print deque into
            # a list here cost 6.5 ms at 2.5 Hz and grew until the ring filled.
            n = buf.trade_count
            rows += [
                ("TAPE", None),
                ("Prints", _fmt(n)),
                ("Avg size", _fmt(buf.trade_vol / n)),
                ("Max print", _fmt(buf.trade_max)),
            ]
        return rows

    # ---- paint -----------------------------------------------------------
    @safe_paint
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        rows = self._collect()
        if not rows:
            p.setFont(self.f_row); p.setPen(LABEL)
            p.drawText(10, 24, "waiting for data…")
            return
        w = self.width()
        y = 6
        for label, value in rows:
            if value is None:                       # section header
                y += 6
                p.setFont(self.f_head); p.setPen(HEAD)
                p.drawText(8, y + 12, label)
                p.setPen(RULE)
                p.drawLine(8, y + 16, w - 8, y + 16)
                y += self.ROW_H + 2
                continue
            p.setFont(self.f_row)
            p.setPen(LABEL)
            p.drawText(10, y + 13, label)
            col = VALUE
            if label in ("Change", "Delta") and value.startswith(("+", "-")):
                col = UP if value.startswith("+") else DOWN
            p.setPen(col)
            p.drawText(QRectF(0, y, w - 10, self.ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       value)
            y += self.ROW_H
            if y > self.height():
                break
