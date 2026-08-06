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

check("no alerts armed means no per-print work",
      win._alert_book_active is False)

# Arm on the symbol that is NOT displayed - the whole point of the feature.
win.sym_combo.setCurrentText("QQQ")
for _ in range(20): app.processEvents(); win._tick(); time.sleep(0.01)
spy = win.series.get("SPY")
last_spy = spy.view(win.tf_s)[-1].close if spy and spy.view(win.tf_s) else 0.0
check("the un-watched symbol has prints", last_spy > 0, f"SPY {last_spy:.2f}")

a = win.alerts.add("SPY", last_spy + 0.05)
check("arming makes the check active", win._alert_book_active is True)

fired_at = None
t0 = time.time()
while time.time() - t0 < 12.0 and a.armed:
    app.processEvents(); win._tick(); time.sleep(0.005)
    if not a.armed and fired_at is None:
        fired_at = time.time()
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
