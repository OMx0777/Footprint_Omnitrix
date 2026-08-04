"""Tiling the open Bookmap windows into a 1 / 1x2 / 2x2 grid."""
import os, sys, time, logging
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.pop("QT_QPA_PLATFORM", None)      # needs a real screen geometry
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.ui.bookmap_window import BookmapWindow
logging.basicConfig(level=logging.CRITICAL)
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

app = QApplication([])
feed = SyntheticFeed(symbols=["QQQ","SPY","AAPL","NVDA"], start_price=400.0,
                     tick=0.01, trades_per_sec=60, prefill_minutes=4, seed=5)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1200, 800); win.show(); win.start_feed()
t0=time.time()
while time.time()-t0<4: app.processEvents(); time.sleep(0.01)
for s in ["QQQ","SPY","AAPL","NVDA"]:
    win.open_bookmap_for(s)
app.processEvents()
bms = [w for w in app.topLevelWidgets() if isinstance(w, BookmapWindow)]
check("four bookmaps open", len(bms) == 4, f"{len(bms)}")

area = QApplication.primaryScreen().availableGeometry()
win.tile_bookmaps(4)
app.processEvents()
g = sorted((w.geometry().x(), w.geometry().y(), w.geometry().width(),
            w.geometry().height()) for w in bms)
check("2x2 fills the screen without overlap",
      len({(x, y) for x, y, _, _ in g}) == 4
      and all(abs(w_ - area.width()//2) <= 2 for _, _, w_, _ in g)
      and all(abs(h - area.height()//2) <= 2 for *_, h in g),
      f"cells {[(x,y,w_,h) for x,y,w_,h in g]}")

win.tile_bookmaps(2)
app.processEvents()
tiled = sorted(w.geometry().width() for w in bms)[-2:]
check("1x2 gives each half the screen width",
      all(abs(t - area.width()//2) <= 2 for t in tiled), f"widths {tiled}")

win.tile_bookmaps(1)
app.processEvents()
big = max(w.geometry().width() for w in bms)
check("single fills the width", abs(big - area.width()) <= 2, f"{big} vs {area.width()}")

# extras must not be disturbed
before = bms[3].geometry()
win.tile_bookmaps(1)
app.processEvents()
check("windows beyond the grid are left alone",
      bms[3].geometry() == before or bms[3] is bms[0],
      "untouched")

for w in bms: w.close()
win.close(); feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("BOOKMAP TILING OK")
