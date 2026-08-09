"""A paint fault must not kill the terminal.

MEASURED, NOT ASSUMED. Under PyQt6 an unhandled Python exception inside a
virtual override - QWidget.paintEvent, QGraphicsItem.paint - does not
propagate. Qt calls qFatal() and the process dies: exit 127, no traceback, no
line in the log, nothing in the faulthandler file. That is the shape of "it
just disappeared".

It really happened here. The signals dock used clock_label without importing
it, so the first time a block print was detected the next paint raised
NameError and took the whole application down with it. One missing import, in
one side panel, in code that was never painted with rows in it.

The lesson is not "audit the imports" - that was done and it found a second
one. It is that the app must survive a bad paint. A trading terminal that
vanishes because one panel could not draw a row is worse in every way than one
that draws that panel blank for a frame.

THE COUNTER IS THE OTHER HALF OF THIS. Swallowing exceptions is how bugs go
quiet, so every fault is counted per site and the test suite asserts the count
is zero. The failure stays loud where loud is useful - in the tests - and stops
being fatal where the user is trying to trade.

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
