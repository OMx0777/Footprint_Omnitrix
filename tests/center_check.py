"""Switching symbol must frame the NEW symbol without the user pressing Alt+R.

The bug: _centered_once latched true after the first symbol ever drawn and was
never reset, so every later switch left the previous instrument's price range
on screen. Two symbols at different prices made that obvious - the new one was
off-screen entirely until you hit Alt+R.
"""
import os, sys, time, logging
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The window RESTORES ~/.omnitrix_workspace.json on construction and SAVES it
# on close, so without this the test both reads the operator's live desk (which
# makes its preconditions depend on whatever they last had open) and can
# overwrite it. Enforced by tests/clock_guard.py.
from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None


from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


app = QApplication([])
# Deliberately far apart in price: framing the wrong one is then unmissable.
feed = SyntheticFeed(symbols=["QQQ"], start_price=400.0, tick=0.01,
                     trades_per_sec=60, prefill_minutes=6, seed=3)
feed2 = SyntheticFeed(symbols=["PENNY"], start_price=12.0, tick=0.01,
                      trades_per_sec=60, prefill_minutes=6, seed=4)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1400, 900)
win.show()
win.start_feed()
feed2.on_trade(win._enqueue)
feed2.on_book(win._enqueue)
feed2.start()

t0 = time.time()
while time.time() - t0 < 6.0:
    app.processEvents(); time.sleep(0.01)


def framed(sym):
    """Is the visible y-range actually around this symbol's last price?"""
    s = win.series.get(sym)
    bars = s.view(win.tf_s) if s else []
    if not bars:
        return None, None
    last = bars[-1].close
    (y0, y1) = win.price_plot.getViewBox().viewRange()[1]
    return (y0 <= last <= y1), last


win.sym_combo.setCurrentText("QQQ")
for _ in range(60):
    app.processEvents(); win._tick(); time.sleep(0.01)
ok, px = framed("QQQ")
y0, y1 = win.price_plot.getViewBox().viewRange()[1]
check("first symbol is framed on its own", ok is True,
      f"QQQ last {px:.2f}, view {y0:.2f}..{y1:.2f}, active={win.active_symbol!r}, "
      f"needs_center={win._needs_center}")

# ---- the actual bug: switch to a symbol at a very different price ----------
win.sym_combo.setCurrentText("PENNY")
for _ in range(60):
    app.processEvents(); win._tick(); time.sleep(0.01)
ok, px = framed("PENNY")
y0, y1 = win.price_plot.getViewBox().viewRange()[1]
check("switching symbol re-frames WITHOUT Alt+R", ok is True,
      f"PENNY last {px:.2f}, view {y0:.2f}..{y1:.2f}")

# ---- and back again --------------------------------------------------------
win.sym_combo.setCurrentText("QQQ")
for _ in range(60):
    app.processEvents(); win._tick(); time.sleep(0.01)
ok, px = framed("QQQ")
y0, y1 = win.price_plot.getViewBox().viewRange()[1]
check("switching back re-frames too", ok is True,
      f"QQQ last {px:.2f}, view {y0:.2f}..{y1:.2f}")

# ---- following must not fight a MANUAL zoom -------------------------------
# A real mouse pan/zoom emits sigRangeChangedManually; setYRange() alone does
# not, so the signal has to be raised here or this tests nothing.
vb = win.price_plot.getViewBox()
vb.setYRange(396, 404, padding=0)
vb.sigRangeChangedManually.emit([False, True])
for _ in range(40):
    app.processEvents(); win._tick(); time.sleep(0.01)
y0, y1 = vb.viewRange()[1]
check("a manual zoom is NOT stolen back on the next frame",
      abs(y0 - 396) < 1 and abs(y1 - 404) < 1,
      f"view {y0:.2f}..{y1:.2f}, auto_y={win._auto_y}")

# ---- but Alt+R / Center hands control back --------------------------------
win._center()
for _ in range(40):
    app.processEvents(); win._tick(); time.sleep(0.01)
check("Center re-enables following", win._auto_y is True)

# ---- and following keeps price on screen as it drifts ---------------------
worst = None
for _ in range(400):
    app.processEvents(); win._tick(); time.sleep(0.005)
    s_ = win.series.get(win.active_symbol)
    b = s_.view(win.tf_s) if s_ else []
    if b:
        yy0, yy1 = vb.viewRange()[1]
        if not (yy0 <= b[-1].close <= yy1):
            worst = (b[-1].close, yy0, yy1)
            break
check("price stays on screen while following", worst is None,
      "never left the view" if worst is None
      else f"price {worst[0]:.2f} outside {worst[1]:.2f}..{worst[2]:.2f}")

feed.stop(); feed2.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    sys.exit(1)
print("AUTO-CENTRE OK")
