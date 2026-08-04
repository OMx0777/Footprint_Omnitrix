"""A bottom axis that labels bar *indices* with their market time.

The chart's x-coordinate is the bar's ordinal position (0..n-1), not a
timestamp, so a normal DateAxisItem won't work. This axis looks each integer
position up in the current bar list and formats that bar's `start_ts`.

Reads like TradingView rather than a plain HH:MM strip:

  * spacing is chosen from the MEASURED label width and the axis's pixel
    length, so labels cannot overlap however far you zoom out. pyqtgraph's
    default spacing is picked from the numeric range alone - it has no idea a
    bar index renders as "09:30:15" - which is why the old axis crowded into an
    unreadable smear.
  * ticks land on round wall-clock boundaries (5s, 15s, 1m, 5m, 1h, 1 day...),
    not on every Nth bar, so the same times appear as you scroll.
  * resolution follows the timeframe: seconds are shown when bars are
    sub-minute and dropped when they are not, instead of being omitted always.
  * the first bar of a new day is labelled with the date, the way a session
    boundary is marked on a real terminal.
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtGui import QFontMetrics

# Round wall-clock spacings, in seconds. A tick lands on a multiple of one of
# these, so labels read 09:30, 09:35, 09:40 rather than drifting with the pan.
_NICE_S = (1, 2, 5, 10, 15, 30,
           60, 120, 300, 600, 900, 1800,
           3600, 7200, 14400, 21600, 43200,
           86400, 172800, 604800)

_PAD_PX = 16          # blank space demanded between neighbouring labels


class PriceAxis(pg.AxisItem):
    """A price scale that never draws a line at a price that cannot exist.

    pyqtgraph picks tick spacing from the numeric range alone, so on a penny
    instrument zoomed into a 23-cent window it emits three levels - 0.05, 0.01
    and 0.005. That last one is HALF A TICK. No trade, quote or footprint cell
    can ever land on it, so it labels nothing and separates nothing.

    It is not free, either. `showGrid` is implemented by extending every tick
    across the whole plot, so each sub-tick level is another full-width line
    drawn every frame. Dropping the levels finer than one instrument tick cut
    the main window's paint by a third (52.1 -> 35.4 ms/frame at 1920x1080)
    without removing a single line a trader could have used.

    `tick` is mutable because the instrument's tick size is a user setting; the
    axis re-reads it through a callable so a change in Settings takes effect
    without rebuilding the plot.
    """

    def __init__(self, *args, tick_fn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._tick_fn = tick_fn or (lambda: 0.01)

    def tickValues(self, minVal, maxVal, size):
        levels = super().tickValues(minVal, maxVal, size)
        try:
            quantum = float(self._tick_fn())
        except Exception:
            return levels
        if not (quantum > 0.0):
            return levels
        # Keep every level at or coarser than one tick. Compare with a small
        # relative slack so a level that IS the tick size survives floating
        # point (0.01 can arrive as 0.009999999999999998).
        keep = [(sp, v) for sp, v in levels if sp >= quantum * 0.999]
        # Never return nothing: if the zoom is so tight that even one tick is
        # coarser than the whole view, the finest available level is still the
        # most useful thing to show.
        return keep or levels[:1]


class TimeAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bars: list = []
        self._tf_s = 60           # bar timeframe; drives seconds vs minutes

    def set_bars(self, bars: list) -> None:
        self._bars = bars
        if bars:
            self._tf_s = getattr(bars[0], "tf_s", 60) or 60

    # ---- helpers ---------------------------------------------------------
    def _show_seconds(self) -> bool:
        """Seconds matter only when a bar is shorter than a minute."""
        return self._tf_s < 60

    def _ts(self, i: int):
        bars = self._bars
        if 0 <= i < len(bars):
            return bars[i].start_ts
        return None

    def _label_px(self) -> float:
        """Width of the widest label this axis can currently produce."""
        fm = QFontMetrics(self.font())
        sample = "88:88:88" if self._show_seconds() else "88:88"
        return max(fm.horizontalAdvance(sample),
                   fm.horizontalAdvance("88 Sep")) + _PAD_PX

    # ---- tick placement --------------------------------------------------
    def tickValues(self, minVal, maxVal, size):
        """Bar positions to label, spaced so text cannot collide.

        `size` is the axis length in pixels, which is the piece pyqtgraph's own
        spacing logic does not relate to label content.
        """
        bars = self._bars
        n = len(bars)
        if n == 0 or size <= 0:
            return []
        lo = max(0, int(minVal))
        hi = min(n - 1, int(maxVal) + 1)
        if hi <= lo:
            return []

        span_bars = hi - lo
        max_labels = max(1, int(size / self._label_px()))
        if span_bars <= max_labels:
            step_bars = 1
        else:
            # Convert the bar budget into a wall-clock spacing, then snap up to
            # a round one so ticks sit on real boundaries.
            want_s = (span_bars / max_labels) * self._tf_s
            step_s = next((s for s in _NICE_S if s >= want_s), _NICE_S[-1])
            step_bars = max(1, int(round(step_s / self._tf_s)))

        t_lo = self._ts(lo)
        if t_lo is None:
            return []

        if step_bars == 1:
            vals = list(range(lo, hi + 1))
        else:
            # Walk forward and keep bars whose timestamp crosses the next round
            # boundary. Indexing by `lo + k*step` would drift whenever bars are
            # missing (a gap, or a quiet premarket), and the labels would stop
            # being round numbers.
            #
            # The walk starts at the first boundary AT OR AFTER the left edge -
            # it deliberately does not emit a tick for `lo` itself. Forcing one
            # there put a label at an arbitrary offset immediately before the
            # first round one, and those two collided at every zoom: that single
            # unaligned tick was the whole overlap bug.
            step_s = step_bars * self._tf_s
            vals = []
            nxt = -(-t_lo // step_s) * step_s          # ceil to the boundary
            for i in range(lo, hi + 1):
                t = self._ts(i)
                if t is None:
                    continue
                if t >= nxt:
                    vals.append(i)
                    nxt = (t // step_s) * step_s + step_s
        # pyqtgraph wants [(spacing, [positions])]; spacing is informational.
        return [(float(max(step_bars, 1)), vals)]

    def tickStrings(self, values, scale, spacing):
        bars = self._bars
        n = len(bars)
        secs = self._show_seconds()
        out = []
        for v in values:
            i = int(round(v))
            if not (0 <= i < n):
                out.append("")
                continue
            lt = time.localtime(bars[i].start_ts)
            prev = self._ts(i - 1)
            new_day = (prev is None
                       or time.localtime(prev).tm_yday != lt.tm_yday)
            if new_day:
                # Session boundary: name the day instead of repeating 00:00.
                out.append(time.strftime("%d %b", lt))
            elif secs:
                out.append(time.strftime("%H:%M:%S", lt))
            else:
                out.append(time.strftime("%H:%M", lt))
        return out
