"""Break things on purpose and check the terminal survives and stays honest.

A demo does not fail on the happy path. It fails when the replay server is
down, when a symbol has no history, when the tick is wrong, when someone
clicks faster than the app expects, or when a print arrives that should be
impossible.

The bar for each of these is not "no traceback". It is:

  * the app keeps running and keeps drawing;
  * it does not display something FALSE - an empty chart is fine, a chart
    showing the wrong number is not;
  * and where a thing genuinely failed, it says so rather than implying
    success.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.engine.model import Trade, BookSnapshot, Aggressor
from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.paintguard import paint_fault_count, reset_paint_faults

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])
SYMS = ["AAA", "BBB", "CCC"]
feed = SyntheticFeed(symbols=SYMS, start_price=100.0, tick=0.01,
                     trades_per_sec=120, prefill_minutes=4, seed=44)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1500, 900)
win.show()
win.start_feed()
win.active_symbol = "AAA"
for _ in range(120):
    app.processEvents()
    win._tick()
    time.sleep(0.003)
reset_paint_faults()


def pump(n=40):
    for _ in range(n):
        app.processEvents()
        win._tick()
        time.sleep(0.002)


def survives(label, fn, frames=40):
    """Run fn, then keep ticking. Any exception is a failure."""
    try:
        fn()
        pump(frames)
        return True, ""
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ---- 1. THE REPLAY SERVER IS DOWN -----------------------------------------
# The most likely demo-day fault, and the app must not hang on it.
feed.replay_host = "127.0.0.1"
feed.replay_port = 1            # nothing listening
feed.token = ""
win._sess_done.clear()
win._sess_fetchers.clear()
ok, err = survives("dead replay server",
                   lambda: win.request_session_history("BBB"), frames=90)
check("a dead replay server does not raise on the GUI thread", ok, err)

# GIVE IT THE TIMEOUT IT IS ENTITLED TO. history.TIMEOUT_S is 8 s, so pumping
# for a third of a second and declaring the app stuck tests the harness's
# patience, not the app's behaviour.
from omnitrix.engine.history import TIMEOUT_S
t_end = time.time() + TIMEOUT_S * 2.5 + 5
while time.time() < t_end and win._sess_fetchers:
    app.processEvents()
    win._tick()
    time.sleep(0.005)
check("a dead server is given up on within its timeout, not retried forever",
      not win._sess_fetchers and "BBB" in win._sess_done,
      f"inflight={len(win._sess_fetchers)} done={'BBB' in win._sess_done} "
      f"(timeout {TIMEOUT_S}s)")
check("...and the failure is recorded, so a later request is not fired again "
      "at a server already known to be down",
      "BBB" in win._sess_done)
check("...and the chart still has its live data - a failed fetch must not "
      "destroy what was already there",
      win.series.get("BBB") is not None and len(win.series["BBB"].bars) > 0,
      f"{len(win.series['BBB'].bars) if win.series.get('BBB') else 0} bars")

# ---- 2. IMPOSSIBLE / HOSTILE PRINTS ---------------------------------------
bad_events = [
    Trade("AAA", 0.0, 100, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", -5.0, 100, Aggressor.SELL, int(time.time() * 1000)),
    Trade("AAA", 1e12, 100, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", 100.0, 0, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", 100.0, -50, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", float("nan"), 100, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", float("inf"), 100, Aggressor.BUY, int(time.time() * 1000)),
    Trade("AAA", 100.0, 100, Aggressor.BUY, 0),
    Trade("AAA", 100.0, 100, Aggressor.BUY, -1),
    Trade("", 100.0, 100, Aggressor.BUY, int(time.time() * 1000)),
    BookSnapshot("AAA", {}, {}, int(time.time() * 1000)),
    BookSnapshot("AAA", {float("nan"): 100}, {}, int(time.time() * 1000)),
]
vol_before = win.series["AAA"].sess_volume
ok, err = survives("hostile prints",
                   lambda: [win._enqueue(e) for e in bad_events])
check("prices of 0, negative, NaN, inf and 1e12, sizes of 0 and -50, "
      "timestamps of 0 and -1, an empty symbol and an empty book are all "
      "survived", ok, err)
check("...and the app is still drawing afterwards",
      win._tick() is None and paint_fault_count() == 0,
      f"{paint_fault_count()} paint faults")

# ---- 3. THE TICK CHANGES UNDER A LIVE SYMBOL ------------------------------
# Every structure keyed by tick index becomes wrong at once.
ok, err = survives("tick change",
                   lambda: win.instruments.set_tick("AAA", 0.05))
check("changing an instrument's tick does not raise", ok, err)
win.instruments.set_tick("AAA", 0.01)
pump(20)

# ---- 4. CLICKING FASTER THAN THE APP EXPECTS ------------------------------
def hammer():
    for i in range(40):
        win.tf_combo.setCurrentText(["10s", "1m", "5m", "15m"][i % 4])
        win.mode_combo.setCurrentText(
            ["Footprint", "Cluster", "Profile", "Delta"][i % 4])
        win.layout_combo.setCurrentText(["1 chart", "2 charts", "4 charts"][i % 3])
        app.processEvents()


ok, err = survives("rapid UI hammering", hammer, frames=60)
check("forty rounds of timeframe + mode + layout with no frames between are "
      "survived", ok, err)
check("...with no paint faults", paint_fault_count() == 0,
      f"{paint_fault_count()}")

# ---- 5. A SYMBOL THAT DOES NOT EXIST --------------------------------------
ok, err = survives("unknown symbol",
                   lambda: win._panes[0].sym_combo.setCurrentText("NOSUCH"))
check("selecting a symbol that has never printed does not raise", ok, err)
check("...and it shows an empty chart rather than another symbol's data",
      win._panes[0].symbol == "NOSUCH"
      and not (win.series.get("NOSUCH") and win.series["NOSUCH"].bars),
      f"symbol={win._panes[0].symbol}")
win._panes[0].sym_combo.setCurrentText("AAA")
pump(20)

# ---- 6. THE FEED STOPS DEAD ----------------------------------------------
ok, err = survives("feed stops", lambda: feed.stop(), frames=80)
check("the feed stopping mid-session does not raise", ok, err)
check("...and the terminal keeps painting the data it has",
      paint_fault_count() == 0, f"{paint_fault_count()} paint faults")

# ---- 7. EVERY CHART MODE OVER BROKEN DATA ---------------------------------
reset_paint_faults()
for mode in ("Footprint", "Cluster", "Profile", "Delta", "Heatmap"):
    win.mode_combo.setCurrentText(mode)
    pump(12)
check("every chart mode paints after all of the above, with no fault",
      paint_fault_count() == 0, f"{paint_fault_count()} paint faults")

# ---- 8. NOTHING WAS FABRICATED -------------------------------------------
# The point of surviving bad input is not to invent numbers from it.
s = win.series["AAA"]
vol = sum(b.volume for b in s.bars)
check("session volume is a sane non-negative number after hostile input",
      s.sess_volume >= 0 and vol >= 0,
      f"sess={s.sess_volume:,} bars={vol:,}")
check("...and no bar carries a NaN or infinite price",
      all(b.high == b.high and b.low == b.low
          and abs(b.high) < 1e11 and abs(b.low) < 1e11 for b in s.bars),
      "a NaN price would poison every axis it touches")
check("...and no bar has a high below its low",
      all(b.high >= b.low for b in s.bars))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("FAULT INJECTION OK")
