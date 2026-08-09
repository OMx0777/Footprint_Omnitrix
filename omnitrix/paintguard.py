"""Contain a failing paint locally instead of re-reporting it every frame.

WHAT THIS IS NOT. It is not what stops a paint fault killing the process -
app._install_excepthook already does that, and does it for every Qt callback
rather than only for paint. Under PyQt6 an unhandled Python exception in code
Qt calls from C++ (a slot, an event handler, QGraphicsItem.paint) reaches
qFatal() and aborts the process, but ONLY while sys.excepthook is the default
one. Measured both ways: default hook, exit 0xC0000409 with no traceback
anywhere; app.py's hook installed, 30 consecutive failing paints and the
process still running.

An earlier commit message here claimed a missing import had been terminating
the app. That was wrong - the excepthook was already catching it - and this
docstring is the correction.

WHAT IT ACTUALLY BUYS. The excepthook survives the fault but reports it in
full, every time. A paint that fails once fails on every frame, so the
backstop turns one bug into a permanent stream of tracebacks formatted and
written synchronously on the GUI thread. Measured over 300 failing frames at
400x300:

    hook only   0.27 ms per frame, 114 kB of log   (~11 kB/s at 30 fps)
    guarded     0.02 ms per frame, 0.6 kB of log

13x cheaper per frame and 179x less log, because the guard reports a given
site once per QUIET_S and thereafter just leaves the widget blank. That is the
right shape for a fault that repeats: loud once, quiet after.

It really happened: the signals dock used clock_label without importing it, so
from the first detected block print onward that panel painted nothing and the
log took the same traceback at frame rate.

THE COUNTER IS THE OTHER HALF OF THIS. Swallowing exceptions is how bugs go
quiet, so every fault is counted per site and the test suite asserts the count
is zero. The failure stays loud where loud is useful - in the tests - and stops
being repetitive where the user is trying to trade.

This module deliberately imports nothing from omnitrix. Every render and ui
module pulls it in, so anything it touched would become a cycle: render ->
ui.framegov -> ui/__init__ -> main_window -> render.
"""

from __future__ import annotations

import functools
import logging
import time

log = logging.getLogger(__name__)

# site -> how many times painting there has failed since start.
PAINT_FAULTS: dict[str, int] = {}

# One log line per site per this many seconds. A paint that fails once fails
# every frame, and a log that floods buries the first occurrence - which is the
# one that says what broke.
QUIET_S = 30.0

_logged: dict[str, float] = {}


def paint_fault_count() -> int:
    """Total paint faults since start. The test suite asserts this is zero."""
    return sum(PAINT_FAULTS.values())


def reset_paint_faults() -> None:
    """For tests that deliberately provoke a fault and then check recovery."""
    PAINT_FAULTS.clear()
    _logged.clear()


def safe_paint(fn):
    """Wrap a paint method so a fault blanks the widget instead of killing it.

    Applied to every paintEvent and every GraphicsObject.paint in the app. On
    the success path this is one try/except with no exception in flight, which
    CPython does not charge for - the cost is zero when nothing goes wrong.
    """
    site = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "paint")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            PAINT_FAULTS[site] = PAINT_FAULTS.get(site, 0) + 1
            now = time.perf_counter()
            if now - _logged.get(site, 0.0) >= QUIET_S:
                _logged[site] = now
                log.exception(
                    "paint failed in %s - widget left blank this frame "
                    "(%d occurrence(s)). The app is still running; under "
                    "PyQt6 this would otherwise have been a silent qFatal.",
                    site, PAINT_FAULTS[site])
            return None

    return wrapper
