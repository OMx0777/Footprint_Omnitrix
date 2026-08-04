"""A shared frame budget across every Omnitrix window.

WHY THIS EXISTS. Qt serves every window from ONE GUI thread. Each window used
to own a fixed timer - the main chart at 33 ms, each bookmap at 80 ms - chosen
as if it were the only thing running. Open four bookmaps alongside the
footprint and the arithmetic stops working:

    main      26 ms of work every 33 ms  =  79% of the thread
    4 bookmaps 11 ms of work every 80 ms  =  55%
                                            ----
                                            134%   on a thread that has 100%

Nothing gets 134% of a thread. Qt resolves the overdraft by firing timers late,
and the cost lands wherever it happens to land: measured, the main chart fell
from 19.8 fps on its own to 6.2 fps with four bookmaps open, with gaps up to
206 ms. That is the dropped frames and the smeared drawings, and no amount of
shaving milliseconds fixes it, because the demand is what is wrong.

WHAT THIS DOES. Windows stop declaring a rate and start declaring a PRIORITY.
The governor measures what each window actually costs - refresh and paint, on
this machine, at this window size, with this much data - and hands back an
interval that keeps total demand under `TARGET`. If the sum fits, everyone runs
at their preferred rate and this is invisible. If it does not, background
windows are stretched first and the focused window is protected, because the
window you are looking at is the only one whose frame rate you can perceive.

The result is a guarantee that does not depend on how many windows are open,
how big they are, or how fast the machine is: total UI demand is bounded, so
frames stop being dropped by accident and start being skipped on purpose,
where it does not show.

Deliberately NOT a thread. Moving rendering off the GUI thread is not possible
with QGraphicsScene, and moving the DATA off it would not help - the measured
cost is paint, not compute.
"""

from __future__ import annotations

import math
import time

import pyqtgraph as pg

# Fraction of one GUI thread we allow the UI to request. The remainder absorbs
# Qt's own event handling, the feed thread's GIL slices, styling, layout and
# garbage collection - none of which run inside our timers but all of which
# compete for the same thread. Measured at 1.0 the UI still stutters, because
# "100% of the thread" leaves nothing for the work that delivers the frame.
# MEASURED, not guessed. On the target machine, running the footprint plus four
# bookmaps with no throttling at all, the windows between them achieved:
#     main       12.6 fps x 26 ms = 328 ms/s
#     4 bookmaps  8.0 fps x 11 ms = 352 ms/s
#                                   ------
#                                   680 ms/s
# and the thread was plainly saturated - every timer was firing late. So ~68%
# is where this application actually runs out of GUI thread, and the remaining
# 32% is Qt's own event handling, layout, styling, the feed thread's GIL slices
# and GC. Budgeting at 1.0 would be budgeting for a thread we do not have.
TARGET = 0.65

# An unfocused window is never stretched past this, so a background bookmap
# still updates four times a second - enough to glance at and see live data.
# This floor is why the budget can be exceeded in the worst case: we would
# rather overshoot slightly than freeze a window the user can see.
MAX_INTERVAL_MS = 250

# Cost estimates settle with an exponential moving average: a single slow frame
# (a GC pause, a window resize) must not blow the budget for everyone.
_ALPHA = 0.25


class _Entry:
    """Cost is split because the two halves are measured in different places.

    refresh_s is timed around the window's own callback. paint_s is timed in
    the plot widget's paintEvent, which Qt calls LATER - after the callback has
    returned and the event loop has processed the damage. Timing only the
    callback would miss 90% of the real cost: measured on the main window,
    refresh is 0.3 ms and paint is 26 ms.
    """

    __slots__ = ("base_ms", "refresh_s", "paint_s", "priority", "alive", "last_ms")

    def __init__(self, base_ms: int, priority: int):
        self.base_ms = base_ms
        # Assume half-loaded until measured, so a window that has not painted
        # yet still contributes something and cannot cause a burst of
        # over-scheduling on the first few frames.
        self.refresh_s = base_ms / 1000.0 * 0.25
        self.paint_s = base_ms / 1000.0 * 0.25
        self.priority = priority
        self.alive = True
        self.last_ms = base_ms

    @property
    def cost_s(self) -> float:
        return self.refresh_s + self.paint_s


class FrameGovernor:
    """Process-wide. One GUI thread means one budget."""

    def __init__(self) -> None:
        self._reg: dict[int, _Entry] = {}
        self._focus: int | None = None

    # ---- registration ----------------------------------------------------
    def register(self, key: int, base_ms: int, priority: int = 1) -> None:
        """`priority` 0 = foreground-critical (the chart being interacted with),
        1 = normal. Lower is protected first."""
        self._reg[key] = _Entry(base_ms, priority)

    def unregister(self, key: int) -> None:
        self._reg.pop(key, None)
        if self._focus == key:
            self._focus = None

    def set_focus(self, key: int | None) -> None:
        self._focus = key

    # ---- measurement -----------------------------------------------------
    def report(self, key: int, seconds: float) -> None:
        """Cost of one refresh callback."""
        e = self._reg.get(key)
        if e is None:
            return
        e.refresh_s += (seconds - e.refresh_s) * _ALPHA

    def report_paint(self, key: int, seconds: float) -> None:
        """Cost of one paint of this window's plot widget."""
        e = self._reg.get(key)
        if e is None:
            return
        e.paint_s += (seconds - e.paint_s) * _ALPHA

    def set_alive(self, key: int, alive: bool) -> None:
        """A hidden window costs nothing and must not hold budget.

        Without this, minimising three bookmaps would still throttle the one
        you are looking at - the opposite of what the guard was for.
        """
        e = self._reg.get(key)
        if e is not None:
            e.alive = alive

    # ---- the decision ----------------------------------------------------
    def interval(self, key: int) -> int:
        """Milliseconds this window should wait before its next frame."""
        e = self._reg.get(key)
        if e is None:
            return 33

        demand = sum(x.cost_s / (x.base_ms / 1000.0)
                     for x in self._reg.values() if x.alive)
        if demand <= TARGET:
            e.last_ms = e.base_ms
            return e.base_ms

        # Over budget. The policy, in order:
        #
        #   1. background windows are stretched first, but never past
        #      MAX_INTERVAL_MS - a window updating twice a second is still
        #      worth glancing at, one updating every two seconds is not;
        #   2. if that frees enough, the protected windows keep their rate;
        #   3. if it does not, the protected windows are stretched as well,
        #      to fit whatever is left AFTER the background floor.
        #
        # Step 3 is what an earlier version got wrong. It handed the protected
        # set the whole TARGET and then let the floored windows add their cost
        # on top, so actual demand landed at 64% against a 55% target - over
        # budget in exactly the case the budget existed for. The floor is
        # irreducible, so it has to be subtracted BEFORE the protected set is
        # served, not after.
        prot_keys = [k for k, x in self._reg.items()
                     if x.alive and (k == self._focus or x.priority == 0)]
        prot_raw = sum(self._reg[k].cost_s / (self._reg[k].base_ms / 1000.0)
                       for k in prot_keys)
        unprot = [x for k, x in self._reg.items()
                  if x.alive and k not in prot_keys]
        unprot_raw = sum(x.cost_s / (x.base_ms / 1000.0) for x in unprot)
        # What the background costs even when stretched as far as we allow.
        unprot_floor = sum(x.cost_s / (MAX_INTERVAL_MS / 1000.0) for x in unprot)

        protected = key in prot_keys
        if prot_raw + unprot_floor <= TARGET:
            # Room exists: protected keep their rate, background absorbs it all.
            if protected:
                e.last_ms = e.base_ms
                return e.base_ms
            slack = TARGET - prot_raw
            scale = unprot_raw / slack if slack > 0 else float("inf")
        else:
            # No room: background goes to the floor and the protected set
            # divides what remains.
            if not protected:
                e.last_ms = MAX_INTERVAL_MS
                return MAX_INTERVAL_MS
            avail = TARGET - unprot_floor
            if avail <= 0.0:
                e.last_ms = MAX_INTERVAL_MS
                return MAX_INTERVAL_MS
            scale = prot_raw / avail

        # Round the interval UP. int() truncates, and a shorter interval means
        # MORE demand, so truncating biases every window over budget - which is
        # the one direction this class must never drift in. It cost 1% of the
        # thread across five windows, enough to fail the budget gate.
        ms = int(min(float(MAX_INTERVAL_MS), math.ceil(e.base_ms * scale)))
        e.last_ms = max(e.base_ms, ms)
        return e.last_ms

    # ---- introspection, for the perf gate and the status bar -------------
    def demand(self) -> float:
        return sum(x.cost_s / (x.base_ms / 1000.0)
                   for x in self._reg.values() if x.alive)

    def snapshot(self) -> list[tuple[int, float, int, int, bool]]:
        return [(k, e.cost_s * 1000.0, e.base_ms, e.last_ms, e.alive)
                for k, e in self._reg.items()]


GOVERNOR = FrameGovernor()


class GovernedTimer:
    """Drives one window's refresh under the shared budget.

    Uses a single-shot timer re-armed after each frame rather than a repeating
    one. A repeating QTimer whose callback overruns its interval queues the next
    fire immediately, so an overloaded window spins as fast as it can and
    starves its neighbours - which is the very failure this class exists to
    prevent. Re-arming after the work means the gap is always measured from the
    END of the last frame.
    """

    def __init__(self, owner, callback, base_ms: int, priority: int = 1):
        from PyQt6.QtCore import QTimer
        self._owner = owner
        self._cb = callback
        self._key = id(owner)
        GOVERNOR.register(self._key, base_ms, priority)
        self._timer = QTimer(owner)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)

    def start(self) -> None:
        self._timer.start(GOVERNOR.interval(self._key))

    def stop(self) -> None:
        self._timer.stop()

    def _fire(self) -> None:
        t = time.perf_counter()
        try:
            self._cb()
        finally:
            # Measure even when the callback raised: a frame that throws still
            # consumed the thread, and pretending it was free would let a
            # failing window keep its rate while everyone else is throttled.
            GOVERNOR.report(self._key, time.perf_counter() - t)
            self._timer.start(GOVERNOR.interval(self._key))

    def release(self) -> None:
        self._timer.stop()
        GOVERNOR.unregister(self._key)


class GovernedPlotWidget(pg.GraphicsLayoutWidget):
    """A plot widget that reports what it costs to draw.

    The governor cannot schedule honestly without this: paint is where the time
    goes, and paint happens in Qt's own event handling, not in ours.

    `gov_key` is the owning WINDOW's id, not this widget's, so the paint cost
    lands on the same budget entry as the refresh that caused it.
    """

    def __init__(self, gov_key: int | None = None, **kwargs):
        super().__init__(**kwargs)
        self._gov_key = gov_key

    def set_gov_key(self, key: int) -> None:
        self._gov_key = key

    def paintEvent(self, ev):
        if self._gov_key is None:
            return super().paintEvent(ev)
        t = time.perf_counter()
        try:
            return super().paintEvent(ev)
        finally:
            GOVERNOR.report_paint(self._gov_key, time.perf_counter() - t)
