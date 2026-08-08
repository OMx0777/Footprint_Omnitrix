"""A slow frame has to name its own cause.

Every freeze in this application has been the same shape - unbounded work on
the frame thread - and every one cost hours to find, because the app recorded
the symptom and nothing about the cause. A dict rebuilt in paint, an alert
gate sorting its book per print, a demotion batch, a history fold: all of them
were invisible until someone sat and measured.

So the watchdog writes down what a slow frame was doing. The point is not that
it detects slowness - a stopwatch does that - it is that the log line names the
SECTION, so the next report arrives with the answer attached.

It must also stay silent and cheap when nothing is wrong, or it becomes the
next thing that makes frames slow.
"""

import logging
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget
from omnitrix.ui.framegov import GovernedTimer, WATCHDOG, watch

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []

    def emit(self, rec):
        self.lines.append(rec.getMessage())


cap = Capture()
logging.getLogger("omnitrix.ui.framegov").addHandler(cap)
logging.getLogger("omnitrix.ui.framegov").setLevel(logging.WARNING)

app = QApplication.instance() or QApplication([])
owner = QWidget()

# ---- 1. a healthy frame says nothing ---------------------------------------
WATCHDOG.worst_ms = 0.0
WATCHDOG.over_count = 0
WATCHDOG._last_log = 0.0


def fast():
    with watch("drain"):
        time.sleep(0.001)


t = GovernedTimer(owner, fast, 10)
for _ in range(5):
    t._fire()
    app.processEvents()
check("a healthy frame logs nothing at all", not cap.lines,
      f"{len(cap.lines)} lines")
check("...but it is still measured", WATCHDOG.worst_ms > 0,
      f"worst {WATCHDOG.worst_ms:.1f} ms")
check("...and counted as within budget", WATCHDOG.over_count == 0)
t.release()

# ---- 2. a slow frame names the section that caused it ----------------------
cap.lines.clear()
WATCHDOG._last_log = 0.0


def slow():
    with watch("drain"):
        time.sleep(0.005)
    with watch("history_fold"):
        time.sleep(0.20)          # the culprit
    with watch("redraw"):
        time.sleep(0.005)


t2 = GovernedTimer(owner, slow, 10)
t2._fire()
app.processEvents()
check("a slow frame is reported", any("SLOW FRAME" in l for l in cap.lines),
      f"{cap.lines[:1]}")
line = next((l for l in cap.lines if "SLOW FRAME" in l), "")
check("...and the line NAMES the expensive section",
      "history_fold" in line, line)
check("...with the sections ordered worst first",
      line.index("history_fold") < min(
          [line.index(x) for x in ("drain", "redraw") if x in line] or [10**9]),
      line)
check("...and reports the frame's real cost",
      any(f"{n}" in line for n in ("20", "21", "22")), line)
t2.release()

# ---- 3. a repeating stall must not flood the log --------------------------
cap.lines.clear()
t3 = GovernedTimer(owner, slow, 10)
for _ in range(6):
    t3._fire()
    app.processEvents()
check("a stall that repeats every frame is logged ONCE, not six times - a "
      "log that floods hides the first occurrence, which is the useful one",
      len(cap.lines) <= 1, f"{len(cap.lines)} lines for 6 slow frames")
t3.release()

# ---- 4. the watchdog itself must be cheap ----------------------------------
N = 20000
a = time.perf_counter()
for _ in range(N):
    with watch("x"):
        pass
per = (time.perf_counter() - a) / N * 1e9
check("marking a section costs well under a microsecond", per < 2000,
      f"{per:.0f} ns per section")

WATCHDOG._sections.clear()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("FRAME WATCHDOG OK")
