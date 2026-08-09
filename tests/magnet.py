"""Magnet mode: a drawing lands on the level it was aimed at.

TradingView's most-used drawing behaviour, and the reason a hand-drawn trend
line never quite touches the high it was drawn to touch. A line anchored three
cents under the wick is not the level the trader meant, it is the level their
mouse managed.

The two ways a magnet goes wrong, and both are worse than not having one:

  * TOO WEAK and it does nothing, so the feature is decoration;

  * TOO STRONG and it fights the user. A point placed deliberately in open
    space - mid-range, nowhere near a bar extreme - must come back EXACTLY as
    given. A magnet that drags every click to the nearest wick makes it
    impossible to draw a level that is not already a wick, which is most of the
    levels worth drawing.

So the checks are: it snaps a near miss, it leaves a deliberate placement
alone, and it never invents a price that is not an actual OHLC of the bar
under the cursor.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow, MAGNET_PX_DEFAULT

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])
feed = SyntheticFeed(symbols=["NVDA"], start_price=220.0, tick=0.01,
                     trades_per_sec=200, prefill_minutes=6, seed=13)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1500, 900)
win.show()
win.start_feed()
win.active_symbol = "NVDA"
import time as _t
for _ in range(160):
    app.processEvents()
    win._tick()
    _t.sleep(0.004)

bars = win.series["NVDA"].view(win.tf_s)
check("the fixture really has bars to snap to", len(bars) > 5, f"{len(bars)}")

i = len(bars) // 2
bar = bars[i]
_px_w, px_h = win.price_plot.vb.viewPixelSize()
check("the view has a usable pixel scale", px_h > 0, f"{px_h:.6f} price/px")

check("magnet is ON by default, as in every charting package that has it",
      win.magnet_on is True and win.chk_magnet.isChecked() is True)

# ---- 1. a NEAR MISS snaps to the extreme it nearly hit ---------------------
near = bar.high - px_h * (MAGNET_PX_DEFAULT * 0.4)      # a few px under the wick
out = win._magnet(QPointF(float(i), near))
check("a click a few pixels under the high snaps ONTO the high",
      abs(out.y() - bar.high) < 1e-9,
      f"{near:.4f} -> {out.y():.4f}, high {bar.high:.4f}")
check("...and the x lands on the bar centre, so the drawing is unambiguous "
      "about which bar it refers to", abs(out.x() - i) < 1e-9, f"{out.x()}")

for name, target in (("low", bar.low), ("open", bar.open), ("close", bar.close)):
    got = win._magnet(QPointF(float(i), target + px_h * 2)).y()
    check(f"...and to the {name} as well", abs(got - target) < 1e-9,
          f"{got:.4f} vs {target:.4f}")

# ---- 2. A DELIBERATE PLACEMENT IS LEFT ALONE ------------------------------
# The one that decides whether this is a magnet or a straitjacket.
lo, hi = min(bar.open, bar.close), max(bar.open, bar.close)
mid = (bar.high + bar.low) / 2.0
far = None
for cand in (mid, (bar.high + hi) / 2.0, (bar.low + lo) / 2.0):
    if all(abs(cand - v) / px_h > MAGNET_PX_DEFAULT * 1.5
           for v in (bar.open, bar.high, bar.low, bar.close)):
        far = cand
        break
if far is None:
    print("  SKIP  bar too small to host an unambiguous open-space point")
else:
    out = win._magnet(QPointF(float(i), far))
    check("a point placed in OPEN SPACE comes back untouched - a magnet that "
          "drags every click to a wick makes it impossible to draw any level "
          "that is not already a wick",
          abs(out.y() - far) < 1e-9, f"{far:.4f} -> {out.y():.4f}")

# ---- 3. it never invents a price ------------------------------------------
import random
rng = random.Random(4)
invented = []
for _ in range(300):
    j = rng.randrange(len(bars))
    b = bars[j]
    y = b.low + (b.high - b.low) * rng.random()
    o = win._magnet(QPointF(float(j), y))
    if abs(o.y() - y) > 1e-12:                 # it moved -> must be an OHLC
        if not any(abs(o.y() - v) < 1e-9
                   for v in (b.open, b.high, b.low, b.close)):
            invented.append((j, y, o.y()))
check("every snapped point is an ACTUAL open/high/low/close of the bar under "
      "the cursor - never an interpolated or rounded price",
      not invented, f"{len(invented)} invented: {invented[:2]}")

# ---- 4. off, and out of range ---------------------------------------------
win.chk_magnet.setChecked(False)
out = win._magnet(QPointF(float(i), bar.high - px_h * 2))
check("with magnet off the point is returned exactly as given",
      abs(out.y() - (bar.high - px_h * 2)) < 1e-12)
win.chk_magnet.setChecked(True)

out = win._magnet(QPointF(float(len(bars) + 50), bar.high))
check("a point beyond the last bar does not raise and is not moved",
      abs(out.y() - bar.high) < 1e-12)
win.active_symbol = "GHOST"
out = win._magnet(QPointF(float(i), bar.high - px_h * 2))
check("a symbol with no series does not raise", out is not None)
win.active_symbol = "NVDA"

# ---- 5. the shortcut -------------------------------------------------------
before = win.chk_magnet.isChecked()
win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_N,
                            Qt.KeyboardModifier.AltModifier, ""))
app.processEvents()
check("Alt+N toggles magnet", win.chk_magnet.isChecked() != before)
check("...and the flag the placement path reads follows the menu",
      win.magnet_on == win.chk_magnet.isChecked())
win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_N,
                            Qt.KeyboardModifier.AltModifier, ""))
app.processEvents()

# a BARE n must still reach the ticker search, not the magnet
win.sym_search.hide()
app.processEvents()
state = win.chk_magnet.isChecked()
win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_N,
                            Qt.KeyboardModifier.NoModifier, "n"))
app.processEvents()
check("a BARE 'n' still opens the ticker search and does NOT toggle magnet - "
      "every tool key is on Alt so typing a symbol is never captured",
      win.sym_search.isVisible() and win.chk_magnet.isChecked() == state,
      f"search={win.sym_search.isVisible()}, magnet={win.chk_magnet.isChecked()}")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("MAGNET OK")
