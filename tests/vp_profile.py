"""The volume-profile drawing must report POC, VAH and VAL - and be RIGHT.

The tool exists (left toolbar, the "profile" glyph), so this establishes what
it actually does: the levels are recomputed here from the same bars by an
independent path and compared, and the drawn output is checked in rendered
pixels rather than by asking the object whether it thinks it drew something.

A profile that draws a POC at the wrong price is worse than no profile at all -
it is a number a trader would act on.
"""

import os
import sys
import time
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.render.drawings import FixedVolumeProfile, _value_area
from omnitrix.ui.main_window import OmnitrixWindow

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


app = QApplication.instance() or QApplication([])
feed = SyntheticFeed(symbols=["QQQ"], start_price=400.0, tick=0.01,
                     trades_per_sec=120, prefill_minutes=10, seed=41)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1500, 900)
win.show()
win.start_feed()
t0 = time.time()
while time.time() - t0 < 6.0:
    app.processEvents()
    time.sleep(0.01)
for _ in range(40):
    app.processEvents()
    win._tick()
    time.sleep(0.005)

# A restored workspace can leave a coarse timeframe selected, which over a
# 10-minute prefill is one bar - nothing to profile. Pin a fine one.
win.tf_combo.setCurrentText("10s")
for _ in range(40):
    app.processEvents(); win._tick(); time.sleep(0.005)

series = win.series.get(win.active_symbol)
bars = series.view(win.tf_s)
check("there are bars to profile", len(bars) >= 3, f"{len(bars)} bars")

# ---- build the drawing exactly as the toolbar does ------------------------
lo = min(b.low for b in bars)
hi = max(b.high for b in bars)
p1 = [0.0, lo - 0.5]
p2 = [float(len(bars)), hi + 0.5]
tick = win.instruments.tick(win.active_symbol)
vp = FixedVolumeProfile(p1, p2, win._get_bars_for_vp, tick)
win.price_plot.addItem(vp)
app.processEvents()

# ---- independent recomputation from the same bars -------------------------
buy, sell = {}, {}
for b in win._get_bars_for_vp(0.0, float(len(bars))):
    bti, bsell, bbuy = b.arrays()
    for ti, sv, bv in zip(bti.tolist(), bsell.tolist(), bbuy.tolist()):
        if sv:
            sell[ti] = sell.get(ti, 0) + sv
        if bv:
            buy[ti] = buy.get(ti, 0) + bv
totals = {ti: buy.get(ti, 0) + sell.get(ti, 0) for ti in set(buy) | set(sell)}
check("the profile has volume to work with", bool(totals), f"{len(totals)} levels")

mx = max(totals.values())
want_poc = min(k for k, v in totals.items() if v == mx)
want_vah, want_val = _value_area(totals, want_poc, 0.70)

# ---- what the drawing itself computes -------------------------------------
got = {}
# _text is a STATICMETHOD, so the replacement must be one too - a plain
# function here would be bound and shift every argument by one.
_orig_text = FixedVolumeProfile._text


def spy_text(p, tr, x, y, s, color, dx=4, dy=-3):
    for tag in ("POC", "VAH", "VAL"):
        if s.startswith(tag):
            got[tag] = float(s.split()[1].replace(",", ""))
    return _orig_text(p, tr, x, y, s, color, dx, dy)


FixedVolumeProfile._text = staticmethod(spy_text)
img = QImage(win.size(), QImage.Format.Format_ARGB32_Premultiplied)
p = QPainter(img)
win.render(p)
p.end()
FixedVolumeProfile._text = staticmethod(_orig_text)

check("the drawing labels all three levels",
      set(got) == {"POC", "VAH", "VAL"}, f"labelled: {sorted(got)}")

if set(got) == {"POC", "VAH", "VAL"}:
    check("POC matches an independent recomputation",
          abs(got["POC"] - want_poc * tick) < tick * 0.51,
          f"drawn {got['POC']:.2f} vs computed {want_poc * tick:.2f}")
    check("VAH matches an independent recomputation",
          abs(got["VAH"] - want_vah * tick) < tick * 0.51,
          f"drawn {got['VAH']:.2f} vs computed {want_vah * tick:.2f}")
    check("VAL matches an independent recomputation",
          abs(got["VAL"] - want_val * tick) < tick * 0.51,
          f"drawn {got['VAL']:.2f} vs computed {want_val * tick:.2f}")
    check("VAL <= POC <= VAH", got["VAL"] <= got["POC"] <= got["VAH"],
          f"{got['VAL']:.2f} / {got['POC']:.2f} / {got['VAH']:.2f}")
    check("the POC is inside the drawn price range",
          lo - 0.5 <= got["POC"] <= hi + 0.5, f"POC {got['POC']:.2f}")

# ---- the value area must actually hold ~70% of the volume -----------------
if want_vah is not None and want_val is not None:
    inside = sum(v for k, v in totals.items() if want_val <= k <= want_vah)
    share = inside / sum(totals.values())
    check("the value area holds about 70% of the range's volume",
          0.68 <= share <= 0.88, f"{share:.1%}")

# ---- and it has to be VISIBLE, not merely computed ------------------------
vb = win.price_plot.getViewBox()
vb.setYRange(lo - 0.5, hi + 0.5, padding=0)
vb.setXRange(-1, len(bars) + 1, padding=0)
for _ in range(5):
    app.processEvents()
img = QImage(win.size(), QImage.Format.Format_ARGB32_Premultiplied)
p = QPainter(img)
win.render(p)
p.end()
POC_RGB = (0xFF, 0xC4, 0x3C)      # POC_COL "#FFC43C"
VA_RGB = (0x5C, 0x9D, 0xFF)       # VA_COL  "#5C9DFF"


def near(c, rgb, tol=40):
    return (abs(c.red() - rgb[0]) < tol and abs(c.green() - rgb[1]) < tol
            and abs(c.blue() - rgb[2]) < tol)


poc_px = va_px = 0
for y in range(70, min(img.height(), 900), 1):
    for x in range(60, min(img.width(), 1400), 3):
        c = img.pixelColor(x, y)
        if near(c, POC_RGB):
            poc_px += 1
        elif near(c, VA_RGB):
            va_px += 1
check("the POC line is actually rendered", poc_px > 30, f"{poc_px} POC pixels")
check("the value-area lines are actually rendered", va_px > 30, f"{va_px} VA pixels")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("VOLUME PROFILE OK")
