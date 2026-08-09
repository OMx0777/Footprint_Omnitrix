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
import logging
import time

log = logging.getLogger(__name__)

import pyqtgraph as pg
from ..paintguard import safe_paint

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
# Frames may run this much over their granted interval before we treat the
# thread as oversubscribed. Some slack is ALWAYS present and is not a problem:
# our callback returns before Qt paints, so the paint lands between frames and
# pushes the next one out. Measured, an unloaded single chart sits at ~1.18 and
# a genuinely oversubscribed desk (4 charts + 4 bookmaps) at ~2.1, so the
# threshold goes between them rather than near either.
LATE_HIGH = 1.40
# Ceiling on how far the requested rate can be stretched, so a pathological
# machine cannot drive every window to a standstill.
MAX_SCALE = 6.0

# An unfocused window is never stretched past this, so a background bookmap
# still updates four times a second - enough to glance at and see live data.
# This floor is why the budget can be exceeded in the worst case: we would
# rather overshoot slightly than freeze a window the user can see.
MAX_INTERVAL_MS = 250

# Cost estimates settle with an exponential moving average: a single slow frame
# (a GC pause, a window resize) must not blow the budget for everyone.
_ALPHA = 0.25
_LATE_ALPHA = 0.15


class _Entry:
    """Cost is split because the two halves are measured in different places.

    refresh_s is timed around the window's own callback. paint_s is timed in
    the plot widget's paintEvent, which Qt calls LATER - after the callback has
    returned and the event loop has processed the damage. Timing only the
    callback would miss 90% of the real cost: measured on the main window,
    refresh is 0.3 ms and paint is 26 ms.
    """

    __slots__ = ("base_ms", "refresh_s", "paint_s", "paint_accum",
                 "priority", "alive", "last_ms")

    def __init__(self, base_ms: int, priority: int):
        self.base_ms = base_ms
        # Assume half-loaded until measured, so a window that has not painted
        # yet still contributes something and cannot cause a burst of
        # over-scheduling on the first few frames.
        self.refresh_s = base_ms / 1000.0 * 0.25
        self.paint_s = base_ms / 1000.0 * 0.25
        # Paint cost ACCUMULATES within a frame and is folded into the EMA at
        # the frame boundary - see report_paint.
        self.paint_accum = 0.0
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
        # Smoothed "how late are our frames", as a ratio of the interval we
        # granted. 1.0 = exactly on time.
        self._late = 1.0
        # Multiplier applied to every window's requested interval. 1.0 = the
        # governor is doing nothing, which is where it sits until frames
        # actually start arriving late.
        self._scale = 1.0

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
        """Cost of one paint against this window's budget. ACCUMULATES.

        A window can own several plot widgets - the chart grid runs up to four,
        all reporting under the main window's key - and they all paint within
        the same frame. Folding each report straight into the EMA averaged them
        instead of adding them, so the estimate converged on the cost of ONE
        pane however many were on screen.

        Measured: a 4-chart grid reported 29% demand while a single chart
        reported 80%, for the same real work. The governor was handing out
        budget it did not have, which is precisely the failure it exists to
        prevent. Sum within the frame; smooth across frames.
        """
        e = self._reg.get(key)
        if e is None:
            return
        e.paint_accum += seconds

    def fold_paint(self, key: int) -> None:
        """Commit a frame's accumulated paint. Called once per frame.

        A frame with no paint at all folds in a genuine zero - a window Qt did
        not need to repaint really did cost nothing, and should give its budget
        back rather than hold it on the strength of an old measurement.
        """
        e = self._reg.get(key)
        if e is None:
            return
        e.paint_s += (e.paint_accum - e.paint_s) * _ALPHA
        e.paint_accum = 0.0

    def set_alive(self, key: int, alive: bool) -> None:
        """A hidden window costs nothing and must not hold budget.

        Without this, minimising three bookmaps would still throttle the one
        you are looking at - the opposite of what the guard was for.
        """
        e = self._reg.get(key)
        if e is not None:
            e.alive = alive

    # ---- the decision ----------------------------------------------------
    #
    # CONTROL SIGNAL: LATENESS, NOT COST.
    #
    # The first version of this modelled demand as cost-per-frame over the
    # requested interval. That needs paints and refreshes to be 1:1, and they
    # are not - Qt repaints on its own schedule (compositor damage, hover,
    # expose, a sibling widget), so the accumulated paint time per refresh
    # over-stated demand at 125-190% and the governor throttled work that was
    # not there. Measured, it made things WORSE than not governing at all:
    #
    #       config                  ungoverned   modelled-cost governor
    #       1 chart                   20.7 fps         13.5 fps
    #       4 charts                  15.1 fps          9.4 fps
    #       4 charts + 4 bookmaps      6.8 fps          4.5 fps
    #
    # So the cost model is gone. What the user actually experiences is whether
    # a frame arrives when it was promised, and that is directly observable:
    # compare the gap between consecutive frames against the interval we
    # granted. Chronically late means the thread is oversubscribed, whatever
    # the reason; on time means it is not, whatever the cost happens to be.
    #
    # The loop then finds the rate the machine can actually sustain, with no
    # assumption about paints, widgets, window size or CPU speed - and it
    # cannot repeat the mistake above, because throttling something that was
    # not the problem shows up immediately as "still late" and is undone.

    def note_gap(self, key: int, gap_s: float) -> None:
        """Report the measured interval between two frames of this window."""
        e = self._reg.get(key)
        if e is None or not e.alive:
            return
        granted = max(e.last_ms, 1) / 1000.0
        # Ratio > 1 means we asked for a frame rate we are not getting.
        ratio = gap_s / granted
        # Clamp a single pathological sample (a GC pause, a resize, the machine
        # sleeping) so one outlier cannot slam every window to the floor.
        ratio = min(ratio, 4.0)
        self._late += (ratio - self._late) * _LATE_ALPHA
        self._adjust()

    def _adjust(self) -> None:
        if self._late > LATE_HIGH:
            # Late: ask for less. Ramp gently - overshooting costs frames that
            # were achievable.
            self._scale = min(MAX_SCALE, self._scale * 1.06)
        else:
            # Not late: hand the frame rate back. Deliberately NO dead band on
            # this side. An earlier version only decayed below a second, lower
            # threshold, and startup - where the prefill genuinely does run
            # late - drove the scale up and then left it stuck there forever,
            # because steady-state lateness sat between the two thresholds. A
            # single chart was being throttled from 20.7 fps to 12.3 fps for a
            # backlog that had cleared minutes earlier.
            self._scale = max(1.0, self._scale * 0.97)

    def interval(self, key: int) -> int:
        """Milliseconds this window should wait before its next frame."""
        e = self._reg.get(key)
        if e is None:
            return 33
        scale = self._scale
        if scale <= 1.0001:
            e.last_ms = e.base_ms
            return e.base_ms
        # Protect the window being looked at: it absorbs a fraction of the
        # throttling the background windows take in full. The loop still
        # converges, because it measures the RESULT of this split rather than
        # predicting it.
        if key == self._focus or e.priority == 0:
            scale = 1.0 + (scale - 1.0) * 0.30
        ms = int(min(float(MAX_INTERVAL_MS), math.ceil(e.base_ms * scale)))
        e.last_ms = max(e.base_ms, ms)
        return e.last_ms

    # ---- introspection, for the perf gate and the status bar -------------
    def demand(self) -> float:
        """Observed lateness: 1.0 means every frame is arriving on schedule."""
        return self._late

    def scale(self) -> float:
        return self._scale

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
        self._last_end = None
        self._timer = QTimer(owner)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)

    def start(self) -> None:
        self._timer.start(GOVERNOR.interval(self._key))

    def stop(self) -> None:
        self._timer.stop()

    def _fire(self) -> None:
        t = time.perf_counter()
        WATCHDOG.begin()
        if self._last_end is not None:
            # Measured from the END of the previous frame, which is when the
            # timer was re-armed. Timing fire-to-fire instead would include our
            # own callback in the gap, so the ratio could never reach 1.0 and
            # the governor would read a permanent 17% lateness on a machine
            # that was perfectly on time - and never hand the frame rate back.
            GOVERNOR.note_gap(self._key, t - self._last_end)
        try:
            self._cb()
        finally:
            # Measure even when the callback raised: a frame that throws still
            # consumed the thread, and pretending it was free would let a
            # failing window keep its rate while everyone else is throttled.
            end = time.perf_counter()
            WATCHDOG.end(self._key, end - t)
            GOVERNOR.report(self._key, end - t)
            self._last_end = end
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

    @safe_paint
    def paintEvent(self, ev):
        if self._gov_key is None:
            return super().paintEvent(ev)
        t = time.perf_counter()
        try:
            return super().paintEvent(ev)
        finally:
            GOVERNOR.report_paint(self._gov_key, time.perf_counter() - t)


# ---------------------------------------------------------------- watchdog


class FrameWatchdog:
    """Names the culprit when a frame runs long.

    EVERY freeze in this application has been the same shape: unbounded work
    on the frame thread. A dict rebuilt in paint, an alert gate sorting its
    book per print, a demotion batch, a history fold. Each one was found by a
    user saying "it froze" and then hours of measurement, because the app
    recorded the symptom and nothing about the cause.

    So a slow frame now writes down what it was doing. Sections are marked by
    the code that could be expensive; when a frame goes over budget the
    watchdog logs the total and the sections that made it up. The next report
    of a stall arrives with the answer attached.

    IT MUST NOT BECOME THE PROBLEM. perf_counter twice per section is ~80 ns,
    the section table is a handful of dict writes, and a frame that is fine
    logs nothing at all. A frame that is NOT fine has already lost far more
    than that.
    """

    __slots__ = ("over_s", "quiet_s", "_sections", "_t0", "_last_log",
                 "worst_ms", "over_count")

    def __init__(self, over_ms: float = 120.0, quiet_s: float = 5.0):
        self.over_s = over_ms / 1000.0
        # A stall usually repeats every frame. Logging each one turns the file
        # into noise and hides the first occurrence, which is the useful one.
        self.quiet_s = quiet_s
        self._sections: dict = {}
        self._t0 = 0.0
        self._last_log = 0.0
        self.worst_ms = 0.0
        self.over_count = 0

    def begin(self) -> None:
        if self._sections:
            self._sections.clear()
        self._t0 = time.perf_counter()

    def add(self, name: str, secs: float) -> None:
        s = self._sections
        s[name] = s.get(name, 0.0) + secs

    def end(self, key: int, secs: float) -> None:
        ms = secs * 1000.0
        if ms > self.worst_ms:
            self.worst_ms = ms
        if secs < self.over_s:
            return
        self.over_count += 1
        now = time.perf_counter()
        if now - self._last_log < self.quiet_s:
            return
        self._last_log = now
        parts = sorted(self._sections.items(), key=lambda kv: -kv[1])[:5]
        detail = "  ".join(f"{n} {v*1000:.0f}ms" for n, v in parts) or "unmarked"
        log.warning("SLOW FRAME %.0f ms (window %s) - %s", ms, key, detail)


WATCHDOG = FrameWatchdog()


class watch:
    """`with watch("fold"): ...` - attribute this block to the current frame."""

    __slots__ = ("name", "t")

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *_exc):
        WATCHDOG.add(self.name, time.perf_counter() - self.t)
        return False
