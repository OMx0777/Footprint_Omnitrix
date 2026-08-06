"""The alert has to work in the RUNNING app, not just in the engine.

Three things the unit test cannot cover: that the check runs where every trade
passes (so a symbol nobody is watching still fires), that the notification
appears without blocking the GUI, and that the sound never runs on the GUI
thread - winsound.Beep blocks for its whole duration, so a 400 ms beep would be
five dropped frames to tell you about one price.
"""
import os, sys, time, logging
sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui import workspace

# NEUTER THE WORKSPACE BEFORE THE WINDOW IS EVER BUILT.
#
# OmnitrixWindow.__init__ restores ~/.omnitrix_workspace.json, which is the
# REAL one - this test was reading the operator's saved alerts and asserting
# an empty book against it, and closeEvent would have written its own back.
#
# It has to be done by replacing the functions, not by pointing
# workspace.PATH somewhere else: `def save(win, path=PATH)` binds the default
# at import time, so reassigning PATH afterwards changes nothing at all.
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.ui import alert_ui
logging.basicConfig(level=logging.CRITICAL)
FAILS = []
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

beeps = []
alert_ui.SOUNDER.play = lambda: beeps.append(time.perf_counter())

app = QApplication.instance() or QApplication([])
feed = SyntheticFeed(symbols=["QQQ", "SPY"], start_price=400.0, tick=0.01,
                     trades_per_sec=200, prefill_minutes=1, seed=21)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1200, 800); win.show(); win.start_feed()
t0 = time.time()
while time.time() - t0 < 4.0:
    app.processEvents(); win._tick(); time.sleep(0.01)

check("no alerts armed means no per-print work", bool(win.alerts) is False)

# Arm on the symbol that is NOT displayed - the whole point of the feature.
win.sym_combo.setCurrentText("QQQ")
for _ in range(20): app.processEvents(); win._tick(); time.sleep(0.01)
spy = win.series.get("SPY")
last_spy = spy.view(win.tf_s)[-1].close if spy and spy.view(win.tf_s) else 0.0
check("the un-watched symbol has prints", last_spy > 0, f"SPY {last_spy:.2f}")

# A HALF-CENT away, not five cents.
#
# This asked a random walk to travel a fixed distance inside a fixed wall-clock
# window. Run on its own it passed every time; run after the other 32 tests, on
# a machine still busy, the walk did not get there and the next four checks all
# failed - a flake that reports the feature as broken when it is not, which is
# worse than no test. Half a cent is one tick, so the very next print on the
# other side fires it whatever the walk does.
a = win.alerts.add("SPY", last_spy + 0.005)
check("arming makes the check active", bool(win.alerts) is True)

fired_at = None
t0 = time.time()
while time.time() - t0 < 12.0 and a.armed:
    app.processEvents(); win._tick(); time.sleep(0.005)
    if not a.armed and fired_at is None:
        fired_at = time.time()
if a.armed:
    # Still nothing: drive one print through the drain directly rather than
    # waiting on the generator's mood. The property under test is "an alert on
    # a symbol nobody is watching fires", not "SPY happens to move".
    from omnitrix.engine.model import Trade, Aggressor
    win._event_q.append(Trade("SPY", last_spy + 0.05, 100, Aggressor.BUY,
                              int(time.time() * 1000)))
    for _ in range(40):
        app.processEvents(); win._tick(); time.sleep(0.005)
check("an alert on a symbol NOT on screen still fires", not a.armed,
      f"{a.describe()} -> fired at {a.fired_price:.2f}" if not a.armed
      else "never fired")
check("it beeped", len(beeps) >= 1, f"{len(beeps)} sounds")
check("the toast is on screen", win._toast.isVisible())
check("the toast names the symbol and price",
      any(r[0] == "SPY" for r in win._toast._rows), str(win._toast._rows[:2]))

# a burst must not become a siren
beeps.clear()
b = [win.alerts.add("QQQ", 1.0), win.alerts.add("QQQ", 2.0),
     win.alerts.add("QQQ", 3.0)]
win._fire_alerts(b)
win._fire_alerts(b)
check("a burst of alerts beeps once, not once each", len(beeps) <= 2,
      f"{len(beeps)} sounds for 6 alerts")

# the real sounder must not block the GUI thread
import threading
real = alert_ui._Sounder()
t = time.perf_counter(); real.play(); dt = (time.perf_counter() - t) * 1000
check("play() returns immediately - it does NOT beep on this thread",
      dt < 20.0, f"{dt:.1f} ms")

# ---- and the fallback must not touch Qt from that worker ------------------
# winsound fails on a machine with no audio device, in a locked-down session,
# and over some RDP configurations - none of which are exotic. The fallback
# taken there used to call QApplication.beep() directly on the worker, which
# breaks Qt's rule that GUI classes belong to one thread. It did not in fact
# deadlock when measured, but "undefined behaviour that happened to work" is
# not a property worth relying on, so the beep is routed back through a
# signal. This asserts the worker only ever emits.
import builtins

seen = {"thread": None, "count": 0}
main_thread = threading.get_ident()
_real_import = builtins.__import__


def _no_winsound(name, *a, **k):
    if name == "winsound":
        raise RuntimeError("simulated: no audio device")
    return _real_import(name, *a, **k)


snd = alert_ui._Sounder()
snd._system_beep = None                     # unbound; the slot below replaces it
snd._fallback.disconnect()
snd._fallback.connect(lambda: seen.update(thread=threading.get_ident(),
                                          count=seen["count"] + 1))
builtins.__import__ = _no_winsound
try:
    snd.play()
    for _ in range(200):                    # let the worker run and Qt deliver
        app.processEvents()
        time.sleep(0.005)
        if seen["count"]:
            break
finally:
    builtins.__import__ = _real_import

check("a failing winsound still reaches the fallback", seen["count"] == 1,
      f"{seen['count']} emissions")
check("...delivered on the GUI thread, not the worker",
      seen["thread"] == main_thread,
      f"ran on {seen['thread']}, GUI is {main_thread}")

qt_in_worker = "QApplication" in alert_ui._Sounder._beep.__code__.co_names
check("the worker body contains no Qt call at all", not qt_in_worker)

# Alt+A path
win.sym_combo.setCurrentText("QQQ")
win._last_cursor = (10.0, 400.123)
before = len(win.alerts.all())
ok = win.add_alert_here()
check("Alt+A arms an alert at the crosshair", ok and len(win.alerts.all()) == before + 1)
newest = [x for x in win.alerts.all() if x.symbol == "QQQ" and abs(x.price - 400.12) < 0.011]
check("...snapped to the instrument's tick", bool(newest),
      f"{[x.price for x in win.alerts.all()][-3:]}")

# persistence
rows = win.alerts.to_list()
check("alerts serialise for the workspace", len(rows) == len(win.alerts.all()))

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS: print("   -", f)
    sys.exit(1)
print("ALERTS IN APP OK")
