"""A bottom axis that labels bar *indices* with their market HH:MM time.

The chart's x-coordinate is the bar's ordinal position (0..n-1), not a
timestamp, so a normal DateAxisItem won't work. This axis looks each integer
position up in the current bar list and formats that bar's `start_ts`.
"""

from __future__ import annotations

import time
import pyqtgraph as pg


class TimeAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bars: list = []

    def set_bars(self, bars: list) -> None:
        self._bars = bars

    def tickStrings(self, values, scale, spacing):
        out = []
        n = len(self._bars)
        for v in values:
            i = int(round(v))
            if 0 <= i < n:
                lt = time.localtime(self._bars[i].start_ts)
                # show the hour only on the hour, else minutes — keeps it clean
                out.append(time.strftime("%H:%M", lt))
            else:
                out.append("")
        return out
