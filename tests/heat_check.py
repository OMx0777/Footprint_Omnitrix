"""Does the bookmap heat field still render? The user's screenshot shows
bubbles and the DOM ladder drawing while the liquidity field behind them is
black. The bisected _visible() I added to BookHeatmapItem is the prime suspect."""
import os, sys, time, logging
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed, BookmapBuffer
from omnitrix.ui.bookmap_window import BookmapWindow
logging.basicConfig(level=logging.CRITICAL)
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

app=QApplication([])
inst=Instruments(default_tick=0.01)
buf=BookmapBuffer("QQQ", inst)
feed=SyntheticFeed(symbols=["QQQ"], start_price=400.0, tick=0.01,
                   trades_per_sec=80, prefill_minutes=3, seed=9)
feed.on_trade(buf.add_trade); feed.on_book(buf.add_book)
feed.start(); time.sleep(2.5); feed.stop()

cols = buf.view(1)
withbook = [c for c in cols if c.book]
check("the buffer has columns", len(cols) > 10, f"{len(cols)} columns")
check("columns carry book data", len(withbook) > 5,
      f"{len(withbook)} of {len(cols)} have a book")

win = BookmapWindow(buf, 0.01); win.resize(1200, 760); win.show()
for _ in range(20): app.processEvents(); win.refresh(); time.sleep(0.02)

pane = win._panes[0]
heat = pane.heat
# what does the item itself think is visible?
span, x_lo, x_hi = heat._visible()
check("_visible() returns the on-screen columns", len(span) > 0,
      f"{len(span)} of {len(heat.cols)} cols, x range {x_lo:.1f}..{x_hi:.1f}")
check("...and they carry books", sum(1 for c in span if c.book) > 0,
      f"{sum(1 for c in span if c.book)} with book")

img=QImage(pane.glw.size(), QImage.Format.Format_ARGB32_Premultiplied)
p=QPainter(img); pane.glw.render(p); p.end()
# the heat field is the only thing that paints large filled areas of colour
from collections import Counter
cnt=Counter()
for y in range(60, img.height()-80, 2):
    for x in range(20, 900, 2):
        c=img.pixelColor(x,y)
        if c.alpha() and (c.red()+c.green()+c.blue()) > 40:
            cnt[c.name()]+=1
tot=sum(cnt.values())
check("the heat field paints pixels", tot > 500,
      f"{tot} non-background pixels in the field area; top {cnt.most_common(3)}")
print()
if FAILS: print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("HEAT FIELD OK")
