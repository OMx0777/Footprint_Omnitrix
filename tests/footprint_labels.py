"""A cell number must be whole, and must never be drawn under the candle.

THE BUG THIS EXISTS FOR. Both cell labels were placed against the centre line
and aligned inward - sell right-aligned at x-0.01, buy left-aligned at x+0.01 -
which is exactly where the candle body sits. The candle is painted after the
cells, so it covered the leading digits and rows on screen read as ".4K" and
".2K" with the tens and hundreds underneath it.

Nothing caught it. _fits was already there and was working: the text genuinely
fitted the rect it was given. The failure was not clipping, it was two objects
drawn in the same place, and no assertion about a string or a width can see
that. The only thing that can is a check on WHERE the text lands relative to
the candle, which is what this does.

It also pins the recovery rule, because the first fix over-corrected: confining
each label to its own bar dropped most of them, leaving rows blank while the
rest of the column sat empty. A label that will not fit inside its bar now
falls outside it, into space nothing else uses.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import QApplication

from omnitrix.render.footprint import FootprintItem, LABEL_PAD_PX
from omnitrix.render.theme import DARK

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])


class Rec:
    """A painter that records what would be drawn instead of drawing it."""

    def __init__(self):
        self.calls = []

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

    def drawText(self, rect, align, text):
        self.calls.append((QRectF(rect), int(align), text))


class FakeBar:
    def has_cells(self):
        return True


item = FootprintItem(tick=0.01, theme=DARK)
PX = 420.0                      # screen px per x-unit; a generously wide column
# Y MUST BE SCALED TOO. _fits checks the rect's HEIGHT against the font, so a
# transform that leaves y alone makes every row 0.05 px tall and every label is
# correctly refused - the harness then measures nothing at all.
PY = 400.0                      # 0.05 x PY = a 20 px row, which is realistic
tr = QTransform().scale(PX, PY)
HALF = item.BOX_W / 2
HW = item._candle_hw(FakeBar())
X = 4.0                         # the bar's centre, in x-units
ROW_H = 0.05


def run(sell_v, buy_v, ws, wb, color=DARK.cell_text):
    r = Rec()
    item._cell_two(r, tr, X, 0.0, ROW_H, HALF, sell_v, buy_v, color,
                   ws, wb, HW)
    return r.calls


# The candle band in SCREEN coordinates - the region the body will cover.
band = tr.mapRect(QRectF(X - HW, 0.0, 2 * HW, ROW_H))

# ---- 1. THE ONE THAT BROKE: no label may land on the candle ---------------
overlaps = []
cases = []
for sw in (1.0, 0.75, 0.5, 0.3, 0.15, 0.06, 0.02):
    for bw in (1.0, 0.6, 0.25, 0.08, 0.02):
        calls = run(31400, 22200, HALF * sw, HALF * bw)
        cases.append((sw, bw, len(calls)))
        for rect, _al, txt in calls:
            if rect.intersects(band):
                overlaps.append((sw, bw, txt, rect.left(), rect.right()))
check("no cell label is ever drawn over the candle body - the failure was two "
      "objects in the same place, which no width check can see",
      not overlaps, f"{len(overlaps)} of {sum(c[2] for c in cases)} labels: "
      f"{overlaps[:2]}")

# ---- 2. and the labels are not silently thrown away ------------------------
both = sum(1 for _s, _b, n in cases if n == 2)
none = [(s, b) for s, b, n in cases if n == 0]
check("a wide bar still labels BOTH sides", both >= len(cases) * 0.8,
      f"{both} of {len(cases)} cases label both sides")
check("...and even the narrowest bars keep their numbers, because a label that "
      "does not fit inside falls outside instead of vanishing",
      not none, f"{len(none)} cases lost their labels: {none[:3]}")

# ---- 3. a label that falls outside sits on the correct side ---------------
calls = run(31400, 22200, HALF * 0.03, HALF * 0.03)   # both far too narrow
check("two labels are drawn even when neither bar can hold one",
      len(calls) == 2, f"{len(calls)}")
if len(calls) == 2:
    lefts = sorted(c[0].center().x() for c in calls)
    cx = tr.mapRect(QRectF(X, 0, 0.001, ROW_H)).center().x()
    check("the sell number stays LEFT of centre and the buy number RIGHT - a "
          "number on the wrong side of the split is worse than none",
          lefts[0] < cx < lefts[1],
          f"{lefts[0]:.0f} | centre {cx:.0f} | {lefts[1]:.0f}")

# ---- 4. padding is real ----------------------------------------------------
calls = run(31400, 22200, HALF, HALF)
gaps = []
for rect, _al, _t in calls:
    gaps.append(min(abs(rect.left() - band.right()),
                    abs(band.left() - rect.right())))
check("labels keep clear of the candle by at least the pad, so they read as "
      "deliberate rather than as touching it",
      all(g >= LABEL_PAD_PX - 0.5 for g in gaps),
      f"gaps {[f'{g:.1f}' for g in gaps]} px, pad {LABEL_PAD_PX}")

# ---- 5. the POC row: an outside label must not use the on-cell colour ------
# On a POC row the cell is near-white and its text is near-black. A label that
# lands OUTSIDE the cell is on the dark chart background, so drawing it in the
# on-cell colour would be black text on a black chart - invisible, and exactly
# the kind of thing that only shows up on one row in one mode.
r = Rec()
seen_pens = []
r.setPen = lambda pen: seen_pens.append(pen)
item._cell_two(r, tr, X, 0.0, ROW_H, HALF, 31400, 22200, DARK.poc_text,
               HALF * 0.02, HALF * 0.02, HW)      # both forced outside
check("a label pushed outside a POC cell is drawn in the CHART text colour, "
      "not the near-black on-cell colour",
      bool(seen_pens) and all(DARK.poc_text not in str(p) for p in seen_pens),
      f"{len(seen_pens)} pen(s) used")

# ---- 6. the candle half-width has ONE definition ---------------------------
# The bug was possible because the candle computed its width and the labels
# assumed it. Both now read the same accessor.
class Plain:
    def has_cells(self):
        return False


item.draw_cells = True
check("the candle width comes from one accessor, and it really does change "
      "with the mode",
      item._candle_hw(FakeBar()) == item.CANDLE_HW
      and item._candle_hw(Plain()) == item.CANDLE_HW_PLAIN,
      f"cells={item._candle_hw(FakeBar())}, plain={item._candle_hw(Plain())}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("FOOTPRINT LABELS OK")
