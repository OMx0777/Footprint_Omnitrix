"""Both footprint layouts must work, and VWAP must stay off until asked for.

Two user-facing choices, and the ways each could be broken while still looking
fine:

  * a style setting that is stored but never reaches the painter. The renderer
    would keep drawing the default and nothing would raise - so the check is
    that the DRAWN GEOMETRY differs, not that an attribute was set;

  * the classic layout regressing on the bug the histogram layout had, where
    labels were drawn under the candle body. Both layouts draw the candle after
    the cells, so both need the same clearance;

  * VWAP coming back on through the workspace. The default moved to off because
    a 2x2 grid drew four of them unrequested; a restore that ignores the saved
    value would quietly undo that.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import QApplication

from omnitrix.render.footprint import (FootprintItem, CELL_HISTOGRAM,
                                       CELL_BLOCKS, CELL_STYLES,
                                       CELL_STYLE_LABELS)
from omnitrix.render.theme import DARK

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])

# ---- 1. the styles are declared coherently ---------------------------------
check("the histogram is the DEFAULT - the new layout is what a new user gets",
      FootprintItem(0.01).cell_style == CELL_HISTOGRAM)
check("both styles are offered, and each has a label for the dialog",
      set(CELL_STYLES) == {CELL_HISTOGRAM, CELL_BLOCKS}
      and all(k in CELL_STYLE_LABELS for k in CELL_STYLES),
      f"{CELL_STYLE_LABELS}")


# ---- 2. THE STYLE MUST REACH THE PAINTER -----------------------------------
class Rec:
    """Records fills and texts instead of drawing them."""

    def __init__(self):
        self.fills = []
        self.texts = []

    def fillRect(self, r, c):
        # MAPPED TO PIXELS. Storing scene units made every width ~0.33, so an
        # "are these equal within 1" check passed for any pair of values and
        # asserted nothing at all.
        self.fills.append(tr.mapRect(QRectF(r)))

    def drawText(self, r, a, t):
        self.texts.append((QRectF(r), t))

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


class FakeBar:
    is_bull = True

    def has_cells(self):
        return True


PX, PY = 420.0, 400.0
tr = QTransform().scale(PX, PY)
X, ROW_H = 4.0, 0.05


def draw(style, sell_v, buy_v, ws_frac, wb_frac):
    it = FootprintItem(tick=0.01, theme=DARK)
    it.cell_style = style
    half = it.BOX_W / 2
    hw = it._candle_hw(FakeBar())
    r = Rec()
    if style == CELL_HISTOGRAM:
        ws, wb = half * ws_frac, half * wb_frac
        if ws > 0:
            r.fillRect(QRectF(X - ws, 0, ws, ROW_H), None)
        if wb > 0:
            r.fillRect(QRectF(X, 0, wb, ROW_H), None)
        it._cell_two(r, tr, X, 0.0, ROW_H, half, sell_v, buy_v,
                     DARK.cell_text, ws, wb, hw)
    else:
        r.fillRect(QRectF(X - half, 0, half, ROW_H), None)
        r.fillRect(QRectF(X, 0, half, ROW_H), None)
        it._cell_two(r, tr, X, 0.0, ROW_H, half, sell_v, buy_v,
                     DARK.cell_text, half, half, hw)
    return r, half, hw


# A row where the two sides are very different in size. The histogram must show
# that difference in WIDTH; the classic layout deliberately does not.
h, half, hw = draw(CELL_HISTOGRAM, 40000, 4000, 1.0, 0.1)
b, _, _ = draw(CELL_BLOCKS, 40000, 4000, 1.0, 0.1)

hw_widths = sorted(r.width() for r in h.fills)
bw_widths = sorted(r.width() for r in b.fills)
check("the histogram makes the two sides DIFFERENT widths - that is what "
      "carries the shape of the auction",
      hw_widths[-1] > 50 and hw_widths[0] < hw_widths[-1] * 0.5,
      f"{hw_widths[0]:.1f} px vs {hw_widths[-1]:.1f} px")
check("the classic layout makes them EQUAL - denser, and read by number",
      bw_widths[-1] > 50 and abs(bw_widths[0] - bw_widths[-1]) < 1.0,
      f"{bw_widths[0]:.1f} px vs {bw_widths[-1]:.1f} px")
check("...so the setting really does change what is drawn, rather than only "
      "being stored", hw_widths != bw_widths)

# ---- 3. NEITHER layout may put a label under the candle --------------------
band = tr.mapRect(QRectF(X - hw, 0.0, 2 * hw, ROW_H))
bad = []
for style in CELL_STYLES:
    for sf, bf in ((1.0, 1.0), (0.6, 0.3), (0.2, 0.9), (0.05, 0.05)):
        r, _, _ = draw(style, 31400, 22200, sf, bf)
        for rect, txt in r.texts:
            if rect.intersects(band):
                bad.append((style, sf, bf, txt))
check("neither layout draws a cell number under the candle body - the candle "
      "is painted after the cells in both, so both need the clearance",
      not bad, f"{len(bad)} overlaps: {bad[:3]}")

nolabel = []
for style in CELL_STYLES:
    r, _, _ = draw(style, 31400, 22200, 1.0, 1.0)
    if len(r.texts) != 2:
        nolabel.append((style, len(r.texts)))
check("...and a full-width row still labels both sides in both layouts",
      not nolabel, f"{nolabel}")

# ---- 4. the setting survives a workspace round-trip ------------------------
from omnitrix.ui import workspace

# NEUTER THE MODULE FUNCTIONS, then keep the originals to call with an explicit
# path. `def save(win, path=PATH)` binds the default at import time, so
# reassigning workspace.PATH does nothing - and a window that closes or a
# settings apply anywhere in this file would otherwise write the operator's
# real ~/.omnitrix_workspace.json. tests/clock_guard.py scans for exactly this.
_real_save, _real_restore = workspace.save, workspace.restore
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow

feed = SyntheticFeed(symbols=["NVDA"], start_price=220.0, tick=0.01,
                     trades_per_sec=60, prefill_minutes=1, seed=2)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1200, 800)

check("VWAP is OFF by default - a 2x2 grid must not draw four of them before "
      "anyone asks", win.chk_vwap.isChecked() is False)

tmp = os.path.join(tempfile.mkdtemp(), "ws.json")
win.fp.cell_style = CELL_BLOCKS
win.chk_vwap.setChecked(True)
_real_save(win, tmp)
raw = json.load(open(tmp, encoding="utf-8"))
check("the layout choice is written to the workspace",
      raw.get("cell_style") == CELL_BLOCKS, f"{raw.get('cell_style')!r}")

win.fp.cell_style = CELL_HISTOGRAM
win.chk_vwap.setChecked(False)
_real_restore(win, tmp)
check("...and comes back on restore", win.fp.cell_style == CELL_BLOCKS,
      f"{win.fp.cell_style!r}")
check("...and so does an explicitly enabled VWAP - the new default must not "
      "override a choice the user already made",
      win.chk_vwap.isChecked() is True)

# a workspace naming a style that does not exist must not break the renderer
raw["cell_style"] = "spiral"
json.dump(raw, open(tmp, "w", encoding="utf-8"))
win.fp.cell_style = CELL_HISTOGRAM
_real_restore(win, tmp)
check("an unknown style in a hand-edited or older workspace falls back rather "
      "than putting the painter in a branch that does not exist",
      win.fp.cell_style in CELL_STYLES, f"{win.fp.cell_style!r}")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("CELL STYLE OK")
