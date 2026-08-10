"""What broke once four charts and four books were open at the same time.

Every one of these was invisible with a single chart, because a single chart IS
the active pane and the active pane was the only thing these code paths ever
touched.

  * ZOOM. Following means keeping the newest bar in view. It does not mean
    choosing the width - but the redraw snapped the range to a fixed 22 bars on
    every frame, so zooming in near the live edge was undone about thirty times
    a second and the chart appeared to zoom itself out.

  * VWAP. The toggle acted on the active pane's curve. The other three kept
    whatever their items were constructed with, which for a PlotDataItem is
    visible - so VWAP drew on charts whose switch was off, and the startup
    default was stored but never applied at all.

  * THE BOOKMAP TOOLBAR drove one book of four.

  * CLUSTER numbers were centred on the column, which is exactly where the
    candle body is drawn.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import QApplication

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.render.footprint import FootprintItem, CELL_BLOCKS
from omnitrix.render.theme import DARK

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])
SYMS = ["AAA", "BBB", "CCC", "DDD"]
feed = SyntheticFeed(symbols=SYMS, start_price=200.0, tick=0.01,
                     trades_per_sec=150, prefill_minutes=8, seed=21)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1600, 950)
win.show()
win.start_feed()
win.active_symbol = "AAA"
win.layout_combo.setCurrentText("4 charts")
# 10-SECOND BARS. At 1m an eight-minute prefill is four bars, and "scroll back
# into history" is not a meaningful action on four bars - the fixture would be
# asserting against a chart that has no history to scroll into.
win.tf_combo.setCurrentText("10s")
for _ in range(200):
    app.processEvents()
    win._tick()
    time.sleep(0.003)

check("four chart panes are visible", len(win._visible_panes()) == 4,
      f"{len(win._visible_panes())}")

# ---- 1. THE ZOOM ----------------------------------------------------------
pane = win._active_pane
vb = pane.price_plot.getViewBox()
bars = win.series[pane.symbol].view(pane.tf_s)
n = len(bars)
check("the pane is following the live edge to begin with",
      pane.auto_scroll is True)

# zoom in HARD, staying at the live edge - exactly what the report describes
right = n + 3
vb.setXRange(right - 8.0, right, padding=0)
win._on_view(pane)
app.processEvents()
w_after_zoom = vb.viewRect().width()
check("the user's zoom takes effect", w_after_zoom < 12.0,
      f"width {w_after_zoom:.1f} bars")
check("...and the pane is STILL following, because the right edge is still at "
      "the newest bar - that is what made this bug possible",
      pane.auto_scroll is True)

for _ in range(40):
    app.processEvents()
    win._tick()
    time.sleep(0.003)
w_later = vb.viewRect().width()
check("...and 40 frames later the zoom is STILL the user's, not a hard-coded "
      "22 bars - following keeps the newest bar in view, it does not choose "
      "the width",
      abs(w_later - w_after_zoom) < 4.0,
      f"{w_after_zoom:.1f} -> {w_later:.1f} bars")

bars2 = win.series[pane.symbol].view(pane.tf_s)
vr = vb.viewRect()
check("...while still showing the newest bar", vr.right() >= len(bars2) - 1,
      f"right {vr.right():.1f} of {len(bars2)} bars")

# and a deliberate scroll back must still release follow. RELATIVE to the bar
# count: a fixed range can sit to the RIGHT of a short series, which is not
# scrolling back at all - it is still looking at the live edge.
nb = len(win.series[pane.symbol].view(pane.tf_s))
vb.setXRange(0.0, max(3.0, nb - 6.0), padding=0)
win._on_view(pane)
check("scrolling back into history releases follow, so history stays put",
      pane.auto_scroll is False,
      f"viewed 0..{max(3.0, nb - 6.0):.0f} of {nb} bars")

# ---- 2. VWAP ON EVERY PANE ------------------------------------------------
check("VWAP is off in the menu by default", win.chk_vwap.isChecked() is False)
vis = [p.vwap_curve.isVisible() for p in win._panes]
check("...and off on EVERY pane, not just the focused one - the default has "
      "to be APPLIED, not merely stored", not any(vis), f"{vis}")

win.chk_vwap.setChecked(True)
app.processEvents()
vis = [p.vwap_curve.isVisible() for p in win._panes]
band = [all(c.isVisible() for _m, c in p.vwap_bands) for p in win._panes]
check("turning it on reaches all four charts", all(vis) and all(band),
      f"curves={vis} bands={band}")
win.chk_vwap.setChecked(False)
app.processEvents()
vis = [p.vwap_curve.isVisible() for p in win._panes]
check("...and turning it off clears all four", not any(vis), f"{vis}")

for name, chk, attrs in (("CPR", win.chk_cpr, ("cpr_item",)),
                         ("EMAs", win.chk_ema, ("ema9_item", "ema21_item"))):
    chk.setChecked(True)
    app.processEvents()
    on = all(getattr(p, a).isVisible() for p in win._panes for a in attrs)
    chk.setChecked(False)
    app.processEvents()
    off = not any(getattr(p, a).isVisible() for p in win._panes for a in attrs)
    check(f"{name} also applies to every pane", on and off,
          f"on={on} off={off}")

# ---- 3. THE FOOTPRINT DEFAULT ---------------------------------------------
check("classic blocks are the DEFAULT footprint layout",
      FootprintItem(0.01).cell_style == CELL_BLOCKS)
check("...and that is what a fresh window's chart uses",
      win.fp.cell_style == CELL_BLOCKS, f"{win.fp.cell_style!r}")


# ---- 4. CLUSTER / PROFILE / DELTA numbers clear of the candle -------------
class Rec:
    def __init__(self):
        self.texts = []

    def save(self):
        pass

    def restore(self):
        pass

    def resetTransform(self):
        pass

    def setFont(self, _f):
        pass

    def setPen(self, _p):
        pass

    def drawText(self, r, a, t):
        self.texts.append((QRectF(r), t))


class FakeBar:
    is_bull = True

    def has_cells(self):
        return True


it = FootprintItem(tick=0.01, theme=DARK)
HALF = it.BOX_W / 2
HW = it._candle_hw(FakeBar())
PXX, PYY = 460.0, 400.0
tr = QTransform().scale(PXX, PYY)
X, ROW_H = 3.0, 0.05
band_rect = tr.mapRect(QRectF(X - HW, 0.0, 2 * HW, ROW_H))

overlaps = []
drawn = 0
for text in ("12.4K", "845", "1.05M", "+27.3K", "9"):
    r = Rec()
    it._cell_one(r, tr, X, 0.0, ROW_H, HALF, text, DARK.cell_text, hw=HW)
    for rect, txt in r.texts:
        drawn += 1
        if rect.intersects(band_rect):
            overlaps.append(txt)
check("a Cluster / Profile / Delta number is never drawn under the candle - "
      "it used to be centred on the column, which is exactly where the body "
      "is painted",
      not overlaps and drawn > 0,
      f"{len(overlaps)} of {drawn} overlapped: {overlaps[:3]}")

r = Rec()
it._cell_one(r, tr, X, 0.0, ROW_H, HALF, "12.4K", DARK.cell_text,
             align_left=True, hw=HW)
check("...including the left-aligned Profile variant",
      bool(r.texts) and not any(rc.intersects(band_rect) for rc, _ in r.texts))

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("MULTI CHART OK")
