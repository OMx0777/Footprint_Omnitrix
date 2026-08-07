"""Crosshair with readouts pinned to the ENDS of its lines.

One implementation for every chart. It was written inline in the main window
first and then wanted on the Bookmap, the tape reader and the analytics panes -
copying it four times is how four crosshairs end up behaving in four subtly
different ways.

The readouts sit against the axes, not next to the pointer. Beside the pointer
is the one place the value must not be: that is exactly where the candles being
read are. Both badges re-pin on zoom and pan, because the edges move without
the pointer moving, and both clear when the pointer leaves so they can never
sit there asserting a price the cursor is no longer on.
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt

# ---- exchange time -------------------------------------------------------
#
# EVERY CLOCK IN THE APPLICATION READS THE EXCHANGE, NOT THE DESK.
#
# A trader works in market time: the open, the close, the 09:30 and 16:00
# boundaries, the session high - all of those are exchange wall clock, and a
# chart labelled in the desk's own timezone forces a subtraction in the head on
# every glance. Measured on the live feed, the desk is 9.5 hours ahead of the
# exchange, so a print at 07:17 ET was being drawn at 16:47.
#
# STORED TIMESTAMPS ARE NOT TOUCHED. Everything internal stays absolute epoch
# milliseconds: the sequence seam, gap repair, alert crossings, bar buckets and
# the recording all depend on one unambiguous clock, and shifting that to suit
# a label would be the kind of two-clocks-that-disagree bug this codebase keeps
# removing. Only the CONVERSION TO TEXT shifts, at the one place every
# conversion already goes through (enforced by tests/clock_guard.py).
#
# The offset is MEASURED, not configured: TakionDecoder already derives it by
# comparing the exchange's ms-since-midnight against the wall clock and
# snapping to 15 minutes, and it publishes that here. Zero until it locks, so
# an app with no feed yet simply shows local time rather than a guess.
_DISPLAY_OFFSET_S = 0.0


def set_display_offset(ms: int) -> None:
    """Milliseconds the DESK is ahead of the EXCHANGE. See above."""
    global _DISPLAY_OFFSET_S
    _DISPLAY_OFFSET_S = float(ms) / 1000.0


def display_offset_ms() -> int:
    return int(_DISPLAY_OFFSET_S * 1000)


def clock_label(t: float, fmt: str = "%H:%M:%S") -> str:
    """Epoch seconds -> clock text, or "" when the value is not a real time.

    `time.localtime()` raises OSError on a negative or absurdly large value,
    and both the crosshair and the time axis are asked to label the whole VIEW
    - which extends past the data whenever you pan or zoom out beyond it. That
    exception came out of paint(), aborting the render half-drawn, which is a
    blank label at best and a smear of leftover pixels at worst. Every
    epoch->text conversion on a paint path must go through here.
    """
    if not (0.0 < t < 32503680000.0) or t != t:      # 1970..3000, and not NaN
        return ""
    try:
        # Shift THEN localtime: subtracting the desk-ahead-of-exchange offset
        # and letting localtime add the desk's own UTC offset back leaves the
        # exchange's wall-clock digits, whatever timezone the desk sits in.
        return time.strftime(fmt, time.localtime(t - _DISPLAY_OFFSET_S))
    except (OSError, OverflowError, ValueError):
        return ""


def safe_localtime(t: float):
    """`time.localtime()` or None, never an exception.

    Same guard as clock_label, for the callers that need the struct rather than
    a formatted string (day-boundary tests, date labels).
    """
    if not (0.0 < t < 32503680000.0) or t != t:
        return None
    try:
        return time.localtime(t - _DISPLAY_OFFSET_S)
    except (OSError, OverflowError, ValueError):
        return None


BADGE_BG = "#9FB0C8"
BADGE_FG = "#05070C"
LINE_PEN = pg.mkPen("#666", style=Qt.PenStyle.DashLine)


class Crosshair:
    """Dashed crosshair over `plot`, with price and time badges on the axes.

    `x_label(x)` turns an x coordinate into a label; return "" to suppress the
    time badge. Charts whose x is not a time (the volume profile's TPO
    brackets, for instance) simply pass None and get the price badge only.
    """

    def __init__(self, plot, x_label=None, price_fmt="{:,.2f}",
                 colour: str = BADGE_BG, add_lines: bool = True,
                 connect: bool = True):
        """`add_lines`/`connect` are False for a chart that already owns its
        crosshair and hover logic - the Bookmap has both, and it needs the
        badges, not a second set of lines fighting the first."""
        self.plot = plot
        self.x_label = x_label
        self.price_fmt = price_fmt
        self.vline = self.hline = None
        if add_lines:
            self.vline = pg.InfiniteLine(angle=90, movable=False, pen=LINE_PEN)
            self.hline = pg.InfiniteLine(angle=0, movable=False, pen=LINE_PEN)
            plot.addItem(self.vline, ignoreBounds=True)
            plot.addItem(self.hline, ignoreBounds=True)

        # Anchors point the text INWARD.
        #
        # A TextItem's anchor is the fraction of its own box placed at the given
        # position, so (0, 0.5) puts its LEFT edge on the right border and the
        # label extends outward, where the ViewBox clips it - measured at 1% of
        # the badge actually on screen. (1, 0.5) hangs it inside instead.
        # Likewise (0.5, 1) sits the bottom edge on the lower border so the
        # label rises into the chart rather than dropping under the axis.
        self.price = self._badge(plot, colour, (1, 0.5))
        self.time = self._badge(plot, colour, (0.5, 1))
        self._last = None

        if connect:
            plot.scene().sigMouseMoved.connect(self._moved)
        # A zoom or pan moves the edges the badges are pinned to, and the
        # pointer need not move for that to happen.
        plot.vb.sigRangeChanged.connect(lambda *_: self.place())

    @staticmethod
    def _badge(plot, colour, anchor):
        it = pg.TextItem(color=BADGE_FG, anchor=anchor,
                         fill=pg.mkBrush(colour))
        it.setZValue(90)
        it.setVisible(False)
        plot.addItem(it, ignoreBounds=True)
        return it

    # ---- behaviour -------------------------------------------------------
    def _moved(self, pos) -> None:
        if not self.plot.sceneBoundingRect().contains(pos):
            self.hide()
            return
        mp = self.plot.vb.mapSceneToView(pos)
        self.set(mp.x(), mp.y())

    def set(self, x: float, y: float) -> None:
        """Point the crosshair at (x, y). Public so a chart with its own mouse
        handling can drive the badges without a duplicate connection."""
        if self.vline is not None:
            self.vline.setPos(x)
            self.hline.setPos(y)
        self._last = (x, y)
        self.place()

    def place(self) -> None:
        if self._last is None:
            return
        x, y = self._last
        (_x0, x1), (y0, _y1) = self.plot.vb.viewRange()
        # Price rides the horizontal line to the right edge, beside the price
        # axis; time rides the vertical line to the bottom edge.
        self.price.setText(self.price_fmt.format(y))
        self.price.setPos(x1, y)
        self.price.setVisible(True)
        label = self.x_label(x) if self.x_label else ""
        self.time.setText(label)
        self.time.setPos(x, y0)
        self.time.setVisible(bool(label))

    def hide(self) -> None:
        self._last = None
        self.price.setVisible(False)
        self.time.setVisible(False)
