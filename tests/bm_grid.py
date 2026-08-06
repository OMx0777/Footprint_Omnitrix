"""Bookmap grid: 1 / 2 / 4 order books in ONE window.

What has to hold, or the grid is worse than four separate windows:
  * each pane shows its OWN symbol's book, not a copy of the selected one;
  * background panes keep updating;
  * per-pane state that MUST NOT leak across symbols - the accumulated
    support/resistance and the fitted price range describe one instrument;
  * selecting a pane retargets the toolbar;
  * the whole grid is ONE frame-budget entry, not four;
  * opening a second symbol fills a pane instead of opening a second window.
"""
import os
import sys
import time
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The window RESTORES ~/.omnitrix_workspace.json on construction and SAVES it
# on close, so without this the test both reads the operator's live desk (which
# makes its preconditions depend on whatever they last had open) and can
# overwrite it. Enforced by tests/clock_guard.py.
from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None


from PyQt6.QtWidgets import QApplication, QSplitter

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.ui.bookmap_window import BookmapWindow
from omnitrix.ui.framegov import GOVERNOR

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def pump(app, win, n=40):
    for _ in range(n):
        app.processEvents()
        win._tick()
        time.sleep(0.005)


app = QApplication.instance() or QApplication([])
SYMS = ["QQQ", "SPY", "AAPL", "NVDA"]
feed = SyntheticFeed(symbols=SYMS, start_price=400.0, tick=0.01,
                     trades_per_sec=80, prefill_minutes=6, seed=13)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1400, 900)
win.show()
win.start_feed()
t0 = time.time()
while time.time() - t0 < 6.0:
    app.processEvents()
    time.sleep(0.01)

# ---- ONE window, not one per symbol ---------------------------------------
for s in SYMS:
    win.open_bookmap_for(s)
    app.processEvents()
bms = [w for w in app.topLevelWidgets() if isinstance(w, BookmapWindow)]
check("four symbols open ONE window, not four", len(bms) == 1, f"{len(bms)} windows")
bm = bms[0]
bm.show()
app.processEvents()

check("it holds four panes", len(bm._panes) == 4)
bm.layout_combo.setCurrentText("4 books")
for _ in range(30):
    app.processEvents()
    bm.refresh()
    time.sleep(0.01)
check("layout 4 shows four", bm._n_panes == 4
      and sum(p.container.isVisible() for p in bm._panes) == 4)

# ---- each pane must carry its OWN book ------------------------------------
for pane, sym in zip(bm._panes, SYMS):
    bm.set_pane_symbol(pane, sym)
for _ in range(60):
    app.processEvents()
    win._tick()
    bm.refresh()
    time.sleep(0.008)

got = [p.buffer.symbol for p in bm._panes]
check("every pane holds its own symbol", got == SYMS, f"{got}")
check("every pane has real depth",
      all(len(p.buffer.view(p.agg)) > 0 for p in bm._panes),
      f"{[len(p.buffer.view(p.agg)) for p in bm._panes]}")
check("the buffers are distinct objects",
      len({id(p.buffer) for p in bm._panes}) == 4)
check("panes hold independent price ranges",
      len({tuple(round(v, 2) for v in p.main.getViewBox().viewRange()[1])
           for p in bm._panes}) > 1)

# ---- overlay items must be repointed, not left on the old book ------------
check("tape overlays follow their pane's buffer",
      all(p.bubbles.buffer is p.buffer and p.pie.buffer is p.buffer
          and p.bars.buffer is p.buffer for p in bm._panes))

# ---- state that must NOT survive a symbol change --------------------------
p0 = bm._panes[0]
p0.fit_price(p0.buffer.view(p0.agg))
before_y = p0._y_range
sr_before = id(p0.sr)
bm.set_pane_symbol(p0, "SPY")
check("changing a pane's symbol resets its support/resistance",
      id(p0.sr) != sr_before,
      "S/R levels from one instrument must not be drawn on another")
check("changing a pane's symbol drops the old fitted range",
      p0._y_range is None, f"{before_y} -> {p0._y_range}")
bm.set_pane_symbol(p0, "QQQ")

# ---- selection ------------------------------------------------------------
bm._select_pane(bm._panes[2])
check("clicking a book selects it", bm._active_pane is bm._panes[2])
check("window aliases follow the selection",
      bm.buffer is bm._panes[2].buffer and bm.heat is bm._panes[2].heat)
check("the toolbar acts on the selected book only", bm.buffer.symbol == "AAPL",
      f"{bm.buffer.symbol}")

# ---- a toolbar change must not touch the other three ---------------------
others = [(p.agg, p.row_ticks) for p in bm._panes if p is not bm._active_pane]
bm.tf_combo.setCurrentText("10s")
app.processEvents()
after = [(p.agg, p.row_ticks) for p in bm._panes if p is not bm._active_pane]
check("changing the timeframe changes ONLY the selected book",
      after == others and bm._active_pane.agg == 10,
      f"selected agg={bm._active_pane.agg}, others {after}")

# ---- headers --------------------------------------------------------------
check("pane headers appear in a grid",
      all(p.header.isVisible() for p in bm._visible_panes()))
bm.layout_combo.setCurrentText("1 book")
app.processEvents()
check("pane header hides for a single book", not bm._panes[0].header.isVisible())
bm.layout_combo.setCurrentText("4 books")
app.processEvents()

# ---- resizable, and aligned ----------------------------------------------
check("the grid is built from splitters",
      isinstance(bm._grid_host, QSplitter)
      and all(isinstance(r, QSplitter) for r in bm._rows))
bm._rows[0].setSizes([700, 300])
bm._link_rows(bm._rows[0])
app.processEvents()
check("dragging a column divider moves BOTH rows",
      bm._rows[0].sizes() == bm._rows[1].sizes(),
      f"top {bm._rows[0].sizes()} bottom {bm._rows[1].sizes()}")
check("the outer splitter holds exactly the two rows",
      len(bm._grid_host.sizes()) == 2, f"{bm._grid_host.sizes()}")
check("panes cannot be collapsed to nothing",
      not bm._grid_host.childrenCollapsible()
      and not bm._rows[0].childrenCollapsible())

# ---- budget ---------------------------------------------------------------
keys = [k for k, *_ in GOVERNOR.snapshot()]
check("four books cost ONE frame-budget entry",
      keys.count(id(bm)) == 1 and len(keys) == 2,      # main window + bookmap
      f"registry {len(keys)} entries")

# ---- re-opening a symbol already on screen selects it ---------------------
win.open_bookmap_for("NVDA")
app.processEvents()
check("re-opening a visible symbol selects its pane, no new window",
      bm._active_pane.buffer.symbol == "NVDA"
      and len([w for w in app.topLevelWidgets()
               if isinstance(w, BookmapWindow)]) == 1)

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("BOOKMAP GRID OK")
