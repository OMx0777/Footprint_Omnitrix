"""Classic DOM price ladder — the vertical depth grid every professional
trading terminal has.

Rows are prices (descending). Columns:

    BID SIZE | resting bid depth bar | PRICE | resting ask depth bar | ASK SIZE
                                     | VOLUME traded at that price (session)

The best bid / best ask rows are highlighted, the session POC is marked, and
depth bars are scaled to the largest resting order on screen so liquidity walls
stand out immediately.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget, QMainWindow
from .framegov import GovernedTimer, watch
from ..paintguard import safe_paint
from . import design

BG = QColor(11, 14, 20)
GRID = QColor(30, 36, 47)
TEXT = QColor(206, 212, 222)
DIM = QColor(132, 140, 154)
BID = QColor(46, 158, 126)
ASK = QColor(212, 86, 79)
BID_BAR = QColor(38, 166, 154, 90)
ASK_BAR = QColor(239, 83, 80, 90)
MID_ROW = QColor(40, 48, 62)
POC_COL = QColor(255, 196, 60)
VOL_BAR = QColor(120, 132, 156, 80)


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if a >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.0f}"


class DomLadderWidget(QWidget):
    ROW_H = 18

    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app = app_window
        self.setMinimumWidth(460)
        self.f_head = design.font(design.DATA_STRONG)
        self.f_row = design.font(design.DATA)
        # Governed. A bare timer cannot be throttled when the chart is
        # already late, and its cost is invisible to the watchdog - a stall
        # here would be reported as "unmarked", which is the state that made
        # every previous freeze expensive to find. Priority 1: a side panel
        # gives way to the chart being traded from.
        self._timer = GovernedTimer(self, self._tick, 200, priority=1)
        self._timer.start()

    def _tick(self) -> None:
        with watch("dom_ladder"):
            self.update()

    @safe_paint
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        app = self.app
        sym = app.active_symbol
        buf = app.bookmaps.get(sym) if sym else None
        # latest() can land on a column created by a trade *after* the last book
        # sweep, which carries no depth at all — using it made the ladder blink
        # "waiting for L2 depth…" with a perfectly good book available.
        col = buf.latest_book() if buf else None
        w, h = self.width(), self.height()

        p.setFont(self.f_head); p.setPen(DIM)
        p.drawText(8, 14, "BID")
        p.drawText(int(w * 0.42), 14, "PRICE")
        p.drawText(int(w * 0.62), 14, "ASK")
        p.drawText(w - 60, 14, "VOLUME")
        p.setPen(GRID); p.drawLine(0, 19, w, 19)

        if col is None or not col.book:
            p.setFont(self.f_row); p.setPen(DIM)
            p.drawText(10, 40, "waiting for L2 depth…")
            return

        tick = app.instruments.tick(sym)
        rows = max(1, (h - 24) // self.ROW_H)
        bid_ti, ask_ti = col.bid_ti, col.ask_ti
        centre = ((bid_ti + ask_ti) // 2 if bid_ti is not None and ask_ti is not None
                  else max(col.book))
        top = centre + rows // 2

        prof = app.profiles.get(sym)
        vols = prof.analytics()["totals"] if prof and prof.total else {}
        poc = prof.analytics()["poc"] if prof and prof.total else None
        max_sz = col.book.max_size() or 1      # cached on the ladder
        max_vol = max(vols.values()) if vols else 1

        px_bid_bar = int(w * 0.14)
        px_price = int(w * 0.40)
        px_ask_bar = int(w * 0.60)
        bar_w = int(w * 0.24)

        p.setFont(self.f_row)
        y = 22
        for k in range(rows):
            ti = top - k
            size = col.book.get(ti, 0)
            is_bid = bid_ti is not None and ti <= bid_ti
            is_ask = ask_ti is not None and ti >= ask_ti
            price = ti * tick

            if (bid_ti is not None and ti == bid_ti) or \
               (ask_ti is not None and ti == ask_ti):
                p.fillRect(QRectF(0, y, w, self.ROW_H), MID_ROW)

            if size > 0:
                frac = size / max_sz
                bw = max(1, int(bar_w * frac))
                if is_bid:
                    p.fillRect(QRectF(px_bid_bar + bar_w - bw, y + 2, bw,
                                      self.ROW_H - 4), BID_BAR)
                    p.setPen(BID)
                    p.drawText(QRectF(4, y, px_bid_bar - 8, self.ROW_H),
                               Qt.AlignmentFlag.AlignRight |
                               Qt.AlignmentFlag.AlignVCenter, _fmt(size))
                elif is_ask:
                    p.fillRect(QRectF(px_ask_bar, y + 2, bw, self.ROW_H - 4),
                               ASK_BAR)
                    p.setPen(ASK)
                    p.drawText(QRectF(px_ask_bar + bar_w + 6, y,
                                      w - px_ask_bar - bar_w - 70, self.ROW_H),
                               Qt.AlignmentFlag.AlignLeft |
                               Qt.AlignmentFlag.AlignVCenter, _fmt(size))

            v = vols.get(ti, 0)
            if v:
                vw = max(1, int(52 * (v / max_vol)))
                p.fillRect(QRectF(w - 8 - vw, y + 3, vw, self.ROW_H - 6), VOL_BAR)
                p.setPen(DIM)
                p.drawText(QRectF(0, y, w - 10, self.ROW_H),
                           Qt.AlignmentFlag.AlignRight |
                           Qt.AlignmentFlag.AlignVCenter, _fmt(v))

            p.setPen(POC_COL if (poc is not None and ti == poc) else TEXT)
            p.drawText(QRectF(px_price, y, 70, self.ROW_H),
                       Qt.AlignmentFlag.AlignCenter, f"{price:,.2f}")

            y += self.ROW_H
            if y > h:
                break


class DomLadderWindow(QMainWindow):
    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        sym = app_window.active_symbol or ""
        self.setWindowTitle(f"Omnitrix DOM Ladder — {sym}")
        self.resize(520, 820)
        self.setCentralWidget(DomLadderWidget(app_window, self))
        self.setStyleSheet("QMainWindow{background:#05070C;}")
