"""Order-Flow Signals dock — live list of detected block prints, absorption
events, broken liquidity walls and delta divergences for the active symbol.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QRectF
from .framegov import GovernedTimer, watch
from PyQt6.QtGui import QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget

from ..engine import signals
# Times in this panel are EXCHANGE times, like every other clock in the app.
# clock_label applies the display offset; time.strftime would show IST and
# quietly disagree with the chart's own axis by nine and a half hours.
from ..render.crosshair import clock_label
from ..paintguard import safe_paint
from . import design

BG = QColor(8, 11, 17)
HEAD = QColor(143, 160, 182)
RULE = QColor(28, 34, 44)
TEXT = QColor(206, 212, 222)
DIM = QColor(140, 148, 162)

KIND_COL = {
    "block": QColor(156, 123, 255),
    "absorption": QColor(255, 179, 0),
    "wall_break": QColor(239, 96, 96),
    # Divergences take the app's existing sell/buy hues rather than new ones:
    # a bearish divergence is a sell warning and should read as one at a glance.
    "bear_div": QColor(239, 96, 96),
    "bull_div": QColor(46, 158, 126),
}
# ASCII only. The tag column is Consolas and an arrow glyph that the font
# lacks renders as a box, which looks like a bug rather than a signal.
KIND_TAG = {"block": "BLK", "absorption": "ABS", "wall_break": "BRK",
            "bear_div": "DV-", "bull_div": "DV+"}

# How far back the divergence detector looks, in BASE bars. Bounded on purpose:
# every repeating timer in this app must cost O(visible), not O(session), or it
# gets slower the longer the terminal is left open.
DIV_LOOKBACK = 120


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.0f}"


class SignalsPanel(QWidget):
    ROW_H = 19

    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app = app_window
        self.setMinimumWidth(250)
        self.f_head = design.font(design.DATA_STRONG)
        self.f_row = design.font(design.DATA)
        self._events: list = []
        # GOVERNED, not a bare QTimer. detect_all costs 26.7 ms on a full
        # 1400-column buffer, so this panel alone is ~3% of a core running
        # continuously - and on a bare timer the governor could not throttle
        # it when frames were ALREADY late, which is exactly when it should
        # give way. It was also invisible to the watchdog, so a stall here
        # would have been reported as "unmarked".
        #
        # Priority 1: this is a side panel. The chart being traded from comes
        # first when the budget is tight.
        self._timer = GovernedTimer(self, self._tick, 900, priority=1)
        self._timer.start()

    def _tick(self) -> None:
        with watch("signals_panel"):
            self._refresh()

    def _refresh(self) -> None:
        app = self.app
        sym = app.active_symbol
        if not sym:
            self.update()
            return
        buf = app.bookmaps.get(sym)
        ev: list = []
        if buf:
            try:
                ev = signals.detect_all(buf, agg=1)[:60]
            except Exception:
                ev = []
        ev.extend(self._divergences(sym, buf))
        # One list, newest first, so a divergence sits in time order beside the
        # block prints and absorption that led to it - which is how it is read.
        ev.sort(key=lambda d: -d["bucket"])
        self._events = ev[:60]
        self.update()

    def _divergences(self, sym: str, buf) -> list:
        """Delta divergence, normalised into this panel's row shape.

        The detector reads BARS, not columns, so it is the one signal here that
        does not come from the bookmap buffer - it needs the BarSeries. Its
        output is converted to the same {kind, ti, size, bucket} the painter
        already draws, rather than teaching the painter a second shape:

          * `ti` is a tick index, so the detector's float price is converted;
          * `size` becomes the delta SHORTFALL - how much cumulative delta
            failed to confirm the new extreme - because that magnitude is the
            whole content of the signal, and the panel's size column is where
            a trader is already looking for "how big";
          * `bucket` is column units, so it sorts and prints alongside the rest.
        """
        ser = self.app.series.get(sym)
        if ser is None or not ser.bars:
            return []
        try:
            divs = signals.detect_delta_divergence(ser.bars,
                                                   lookback=DIV_LOOKBACK)
        except Exception:
            return []
        tick = self.app.instruments.tick(sym) or 0.01
        dt = buf.col_dt if buf is not None else 1.0
        out = []
        for d in divs:
            out.append({
                "kind": d["kind"],
                "ti": int(round(d["price"] / tick)),
                "size": abs(d["delta"] - d["prev_delta"]),
                "bucket": (d["ts"] / 1000.0) / dt if dt else 0.0,
            })
        return out

    @safe_paint
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        w = self.width()

        p.setFont(self.f_head); p.setPen(HEAD)
        p.drawText(8, 16, "TYPE   PRICE      SIZE   TIME")
        p.setPen(RULE); p.drawLine(8, 21, w - 8, 21)

        if not self._events:
            p.setFont(self.f_row); p.setPen(DIM)
            p.drawText(10, 40, "no signals yet…")
            return

        sym = self.app.active_symbol or "QQQ"
        tick = self.app.instruments.tick(sym)
        dt = self.app.bookmaps[sym].col_dt if sym in self.app.bookmaps else 1.0

        p.setFont(self.f_row)
        y = 26
        for ev in self._events:
            col = KIND_COL.get(ev["kind"], TEXT)
            p.setPen(col)
            p.drawText(9, y + 13, KIND_TAG.get(ev["kind"], "?"))
            p.setPen(TEXT)
            p.drawText(52, y + 13, f"{ev['ti'] * tick:,.2f}")
            p.drawText(QRectF(0, y, w - 66, self.ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       _fmt(ev["size"]))
            p.setPen(DIM)
            p.drawText(QRectF(0, y, w - 8, self.ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       clock_label(ev["bucket"] * dt))
            y += self.ROW_H
            if y > self.height():
                break
