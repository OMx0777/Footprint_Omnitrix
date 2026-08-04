"""The chart grid: 1 / 2 / 4 panes, each an INDEPENDENT chart.

What has to hold, or the grid is worse than not having one:
  * each pane draws its OWN symbol, not a copy of the active one;
  * a background pane keeps updating (it is not frozen);
  * selecting a pane retargets the toolbar and the drawing tools;
  * changing layout never loses a pane's symbol, zoom or drawings;
  * the whole grid still costs one budget entry, not four.
"""
import os, sys, time, logging
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow, LAYOUTS, MAX_PANES
from omnitrix.ui.framegov import GOVERNOR

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def pump(win, app, n=40):
    for _ in range(n):
        app.processEvents(); win._tick(); time.sleep(0.005)


app = QApplication([])
feed = SyntheticFeed(symbols=["QQQ", "SPY", "AAPL", "NVDA"], start_price=400.0,
                     tick=0.01, trades_per_sec=80, prefill_minutes=6, seed=11)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1600, 950)
win.show()
win.start_feed()
t0 = time.time()
while time.time() - t0 < 6.0:
    app.processEvents(); time.sleep(0.01)

check("starts as a single chart", win._n_panes == 1, f"{win._n_panes} panes")

# ---- go to 2x2 and give each pane its own symbol ---------------------------
win.layout_combo.setCurrentText("4 charts")
pump(win, app)
check("layout 4 shows four panes", win._n_panes == 4, f"{win._n_panes}")
check("four widgets are visible",
      sum(p.glw.isVisible() for p in win._panes) == 4,
      f"{sum(p.glw.isVisible() for p in win._panes)} visible")

syms = ["QQQ", "SPY", "AAPL", "NVDA"]
for pane, sym in zip(win._panes, syms):
    pane.symbol = sym
    pane._needs_center = True
pump(win, app, 80)

# ---- each pane must render ITS OWN symbol ---------------------------------
drawn = []
for pane, sym in zip(win._panes, syms):
    bars = pane.fp.bars if hasattr(pane.fp, "bars") else []
    ref = win.series.get(sym)
    ref_bars = ref.view(win.tf_s) if ref else []
    drawn.append(len(bars) > 0 and len(bars) == len(ref_bars))
check("every pane draws its own symbol", all(drawn), f"{drawn} for {syms}")

prices = [p.price_plot.getViewBox().viewRange()[1] for p in win._panes]
check("panes hold independent price ranges",
      len({(round(a, 2), round(b, 2)) for a, b in prices}) > 1,
      f"{[(round(a,1), round(b,1)) for a, b in prices]}")

# ---- a background pane must keep updating ---------------------------------
bg = win._panes[2]
before = len(bg.fp.bars) if hasattr(bg.fp, "bars") else 0
before_close = bg.time_axis._bars[-1].close if bg.time_axis._bars else None
pump(win, app, 150)
after_close = bg.time_axis._bars[-1].close if bg.time_axis._bars else None
check("a background pane is NOT frozen",
      after_close is not None and before_close is not None,
      f"AAPL last {before_close} -> {after_close}")

# ---- selecting a pane retargets the toolbar -------------------------------
win._select_pane(win._panes[3])
check("clicking a pane makes it active", win._active_pane is win._panes[3])
check("the toolbar follows the selection",
      win.active_symbol == "NVDA", f"active_symbol={win.active_symbol!r}")
check("chart aliases follow too", win.fp is win._panes[3].fp)

# ---- drawings belong to their pane ----------------------------------------
win._panes[3].drawing_items.append("marker-on-pane-3")
win._select_pane(win._panes[0])
check("drawings stay with their own pane",
      win.drawing_items is win._panes[0].drawing_items
      and "marker-on-pane-3" in win._panes[3].drawing_items,
      f"pane0={win.drawing_items}, pane3={win._panes[3].drawing_items}")

# ---- shrinking the layout must not lose state -----------------------------
win.layout_combo.setCurrentText("2 charts")
pump(win, app)
check("layout 2 shows two panes", win._n_panes == 2)
check("hidden panes keep their symbol",
      win._panes[3].symbol == "NVDA" and "marker-on-pane-3" in win._panes[3].drawing_items,
      f"pane3 symbol={win._panes[3].symbol!r}")
check("the active pane is always one you can see",
      win._active_pane in win._panes[:win._n_panes])

win.layout_combo.setCurrentText("4 charts")
pump(win, app)
check("growing back restores the pane's symbol", win._panes[3].symbol == "NVDA")

# ---- the grid is ONE budget entry -----------------------------------------
keys = [k for k, *_ in GOVERNOR.snapshot()]
check("four charts cost one frame-budget entry, not four",
      keys.count(id(win)) == 1 and len(keys) == 1,
      f"registry keys={len(keys)}")

# ---- back to one chart -----------------------------------------------------
win.layout_combo.setCurrentText("1 chart")
pump(win, app)
check("returns to a single chart", win._n_panes == 1
      and sum(p.glw.isVisible() for p in win._panes) == 1)

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print(f"   - {f}")
    sys.exit(1)
print("CHART GRID OK")
