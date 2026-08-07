"""Per-pane ticker + chart type, resizable grid, CVD off by default.

The target is the user's own example: NVDA footprint, QQQ delta, SPY profile,
NVDA heatmap - four charts, four independent configurations, at the same time.
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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QSplitter
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow, MODES
logging.basicConfig(level=logging.CRITICAL)
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)
def pump(w,a,n=40):
    for _ in range(n): a.processEvents(); w._tick(); time.sleep(0.005)

app = QApplication([])
feed = SyntheticFeed(symbols=["QQQ","SPY","AAPL","NVDA"], start_price=400.0,
                     tick=0.01, trades_per_sec=80, prefill_minutes=6, seed=11)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1600, 950); win.show(); win.start_feed()
t0=time.time()
while time.time()-t0<6: app.processEvents(); time.sleep(0.01)

# ---- 1. CVD hidden by default ---------------------------------------------
check("CVD pane is hidden by default",
      not win._panes[0].cvd_plot.isVisible() and not win.chk_cvd.isChecked())
check("hiding CVD hands the time axis to the price chart",
      win._panes[0].price_plot.getAxis("bottom").isVisible())
win.chk_cvd.setChecked(True); pump(win, app, 10)
check("enabling CVD shows it", win._panes[0].cvd_plot.isVisible())
win.chk_cvd.setChecked(False); pump(win, app, 10)

# ---- 2. four charts, four DIFFERENT configurations ------------------------
win.layout_combo.setCurrentText("4 charts"); pump(win, app)
want = [("NVDA","Footprint"), ("QQQ","Delta"), ("SPY","Profile"), ("NVDA","Heatmap")]
for pane,(sym,mode) in zip(win._panes, want):
    pane.sym_combo.setCurrentText(sym)
    pane.mode_combo.setCurrentText(mode)
pump(win, app, 90)
got = [(p.symbol, p.mode_combo.currentText()) for p in win._panes]
check("each pane holds its own ticker AND chart type", got == want, f"{got}")

fp_modes = [p.fp.mode for p in win._panes]
check("the FootprintItem of each pane really changed mode",
      fp_modes[0] == MODES["Footprint"][0] and fp_modes[1] == MODES["Delta"][0]
      and fp_modes[2] == MODES["Profile"][0], f"{fp_modes}")
check("the heatmap pane shows a heatmap and the others do not",
      win._panes[3].heatmap.isVisible()
      and not any(p.heatmap.isVisible() for p in win._panes[:3]),
      f"{[p.heatmap.isVisible() for p in win._panes]}")
check("two panes on the same ticker are both drawn",
      win._panes[0].symbol == win._panes[3].symbol == "NVDA"
      and len(win._panes[0].time_axis._bars) > 0
      and len(win._panes[3].time_axis._bars) > 0)

# ---- 2b. per-pane TIMEFRAME ----------------------------------------------
# The point of a grid is comparing horizons, so one window-wide timeframe would
# defeat it: the same name at 10s and at 5m side by side has to be possible.
for pane, tf in zip(win._panes, ["10s", "1m", "5m", "15m"]):
    pane.tf_combo.setCurrentText(tf)
pump(win, app, 90)
got_tf = [p.tf_combo.currentText() for p in win._panes]
check("each pane holds its own timeframe", got_tf == ["10s", "1m", "5m", "15m"], f"{got_tf}")
secs = [p.tf_s for p in win._panes]
check("the timeframes really differ in seconds", secs == [10, 60, 300, 900], f"{secs}")
bars = []
for p_ in win._panes:
    ser = win.series.get(p_.symbol)
    bars.append(len(ser.view(p_.tf_s)) if ser else 0)
check("a finer timeframe yields more bars than a coarser one",
      bars[0] >= bars[1] >= bars[2] >= bars[3] and bars[0] > bars[3], f"{bars}")
check("each pane's axis follows its OWN timeframe",
      [len(p_.time_axis._bars) for p_ in win._panes] == bars,
      f"{[len(p_.time_axis._bars) for p_ in win._panes]} vs {bars}")
win._select_pane(win._panes[2])
check("the toolbar shows the selected pane's timeframe",
      win.tf_combo.currentText() == "5m" and win.tf_s == 300,
      f"{win.tf_combo.currentText()} / {win.tf_s}")
for pane in win._panes:
    pane.tf_combo.setCurrentText("1m")
pump(win, app, 40)

# ---- 3. per-pane headers visible only in a grid ---------------------------
check("pane headers appear in a grid", all(p.header.isVisible() for p in win._panes[:4]))
win.layout_combo.setCurrentText("1 chart"); pump(win, app)
check("pane header hides in single-chart mode",
      not win._panes[0].header.isVisible())
win.layout_combo.setCurrentText("4 charts"); pump(win, app)

# ---- 4. the grid is resizable and stays aligned ---------------------------
check("the grid is built from splitters",
      isinstance(win._grid_host, QSplitter)
      and all(isinstance(r, QSplitter) for r in win._rows))
win._rows[0].setSizes([700, 300])
# LET QT SETTLE FIRST, and settle means SETTLE. setSizes() is a request, not an
# assignment - the splitter recomputes against its minimums and stretch factors
# on a later layout pass, and one processEvents() is not reliably enough
# (measured: still 1 run in 6). Reading sizes() early makes _link_rows copy a
# value that is about to change, and the two rows then genuinely differ - a
# test that cries wolf about a product that is fine, because a real drag emits
# splitterMoved repeatedly and the last one is settled.
def settle(w, tries=300, stable=8):
    """Pump until sizes() has been UNCHANGED for `stable` consecutive passes.

    Breaking on the first repeat is not enough: the splitter converges toward
    the requested sizes over several layout passes and can read the same value
    twice on the way (measured 552 -> 564 -> 588 toward a requested 700), so a
    single repeat means nothing.
    """
    prev, same = None, 0
    for _ in range(tries):
        app.processEvents()
        time.sleep(0.002)
        cur = w.sizes()
        same = same + 1 if cur == prev else 0
        prev = cur
        if same >= stable:
            return cur
    return prev


# A real drag emits splitterMoved repeatedly and converges, because setting
# row 1 makes the PARENT splitter relayout, which nudges row 0 again. One
# link against one settled reading therefore races: measured 1 run in 8 even
# after waiting for row 0 to hold still. Iterate the way a drag does, and stop
# as soon as the two agree.
for _ in range(12):
    settle(win._rows[0])
    win._link_rows(win._rows[0])
    settle(win._rows[1])
    if win._rows[0].sizes() == win._rows[1].sizes():
        break
# ALIGNED, not byte-identical. The property that matters is that the two rows
# track each other so the four charts read as a grid rather than two unrelated
# pairs - and Qt settles a splitter against its children's minimums, so the
# rows can land a few pixels apart while being visibly aligned. Demanding
# exact equality made this fail about one run in four for a difference of 12 px
# on a 1064 px row, which is 1% and invisible.
#
# The tolerance is still far tighter than the failure it guards: rows that were
# genuinely not linked read [700, 300] against [532, 532], a gap of 168 px.
_t, _b = win._rows[0].sizes(), win._rows[1].sizes()
_gap = max(abs(x - y) for x, y in zip(_t, _b)) if len(_t) == len(_b) else 9999
check("dragging a column divider moves BOTH rows together", _gap <= 24,
      f"top {_t}  bottom {_b}  (largest gap {_gap} px)")
before = win._grid_host.sizes()
win._grid_host.setSizes([600, 400]); app.processEvents()
check("rows are resizable too", win._grid_host.sizes() != before,
      f"{before} -> {win._grid_host.sizes()}")
check("panes cannot be collapsed to nothing",
      not win._grid_host.childrenCollapsible()
      and not win._rows[0].childrenCollapsible())

# ---- 5. selecting a pane re-reads its settings into the toolbar -----------
win._select_pane(win._panes[3])
check("toolbar shows the selected pane's chart type",
      win.mode_combo.currentText() == "Heatmap", f"{win.mode_combo.currentText()}")
win._select_pane(win._panes[1])
check("and updates again on the next selection",
      win.mode_combo.currentText() == "Delta", f"{win.mode_combo.currentText()}")
check("toolbar symbol follows too", win.active_symbol == "QQQ",
      f"{win.active_symbol}")

# ---- 6. changing one pane must not disturb the others --------------------
snap = [(p.symbol, p.mode_combo.currentText()) for p in win._panes]
win._panes[2].sym_combo.setCurrentText("AAPL"); pump(win, app, 40)
after = [(p.symbol, p.mode_combo.currentText()) for p in win._panes]
check("changing one pane leaves the other three untouched",
      after[0] == snap[0] and after[1] == snap[1] and after[3] == snap[3]
      and after[2] == ("AAPL", "Profile"), f"{after}")

# ---- 7. the live-price tag on every pane ---------------------------------
# The footprint charts had a live price LINE but no value on it, so you could
# see where price was and had to read the number off the axis gradations.
pump(win, app, 60)
tagged = []
for p in win._panes:
    lbl = getattr(p.price_line, "label", None)
    if lbl is None:
        continue
    txt = lbl.textItem.toPlainText().replace(",", "")
    try:
        shown = float(txt)
    except ValueError:
        shown = None
    tagged.append((p.index, shown, round(p.price_line.value(), 4)))
check("every chart pane carries a live-price tag",
      len(tagged) == len(win._panes), f"{len(tagged)} of {len(win._panes)}")
wrong = [t for t in tagged if t[1] is None or abs(t[1] - t[2]) > 0.011]
check("...showing the price the line is actually at", not wrong,
      f"{wrong}" if wrong else f"{[t[1] for t in tagged]}")

# Decimals follow the instrument, not a hardcoded 2. A sub-penny name shown at
# 2 dp prints every level as the same rounded number.
pane0 = win._panes[0]
pane0.instruments.set_tick(pane0.symbol or "QQQ", 0.0001)
pane0._tag_tick = -1.0
pane0.sync_price_tag()
check("the tag's decimals follow the instrument's tick",
      pane0.price_line.label.format == "{value:,.4f}",
      f"{pane0.price_line.label.format}")

# ---- 8. the bookmap background and its LUT zero stop must be equal --------
# The heat field is an image drawn over the background. If its zero-liquidity
# colour differs at all, the field's extent shows as a rectangle sitting on
# the chart - which is what darkening one without the other would have done.
from omnitrix.render.bookmap import BOOKMAP_BG, _build_bookmap_lut
_zero = _build_bookmap_lut()[0]
_bg = QColor(BOOKMAP_BG)
check("the bookmap's empty book matches its background exactly",
      (_zero.red(), _zero.green(), _zero.blue())
      == (_bg.red(), _bg.green(), _bg.blue()),
      f"LUT {_zero.name()} vs BG {_bg.name()}")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS: print("   -", f)
    sys.exit(1)
print("PANE CONTROLS OK")
