"""Bookmap Auto price grid: follows zoom, and all five consumers agree."""
import sys, os, random, tempfile, time
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
app = QApplication([])

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.model import Trade, BookSnapshot, Aggressor
from omnitrix.ui.bookmap_window import BookmapWindow, PRICE_STEP
from omnitrix.render.pricegrid import AUTO_STEPS, TARGET_PX_BAND, auto_step_ticks

FAIL = []
def check(ok, msg):
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    if not ok: FAIL.append(msg)

TICK = 0.01
inst = Instruments()
buf = BookmapBuffer("QQQ", inst, col_dt=1.0)
r = random.Random(3)
t0 = int(time.time() * 1000) - 400_000
for i in range(400):
    ts = t0 + i * 1000
    mid = 40000 + int(r.gauss(0, 30))
    bids = {(mid - k) * TICK: r.randint(100, 9000) for k in range(1, 60)}
    asks = {(mid + k) * TICK: r.randint(100, 9000) for k in range(1, 60)}
    buf.add_book(BookSnapshot("QQQ", bids, asks, ts))
    for _ in range(6):
        ti = mid + int(r.gauss(0, 8))
        buf.add_trade(Trade("QQQ", ti * TICK, r.randint(1, 900),
                            r.choice([Aggressor.BUY, Aggressor.SELL]), ts))

win = BookmapWindow(buf, TICK); win.show()
for _ in range(3): app.processEvents()

check("Auto" in PRICE_STEP and list(PRICE_STEP)[0] == "Auto",
      "Auto is offered and is the default item")
check(win.step_combo.currentText() == "Auto", "combo shows Auto")
check(win.auto_step is True, "...and auto_step state AGREES with the combo")

def consumers():
    return (win.row_ticks, win.heat.row_ticks, win.dom_item.row_ticks,
            win.bubbles.row_ticks, win.pie.row_ticks, win.bars.row_ticks)

# zoom out progressively; the grid must widen and everyone must stay in step
seen, bad_sync = [], 0
for span in (0.2, 1.0, 4.0, 20.0, 100.0):
    win.main.setYRange(400.0 - span/2, 400.0 + span/2, padding=0)
    for _ in range(3): app.processEvents()
    win.refresh()
    c = consumers()
    if len(set(c)) != 1: bad_sync += 1
    seen.append(c[0])
check(bad_sync == 0, f"all 6 consumers share one grid at every zoom")
check(seen == sorted(seen) and len(set(seen)) > 2,
      f"grid widens as you zoom out: {seen}")
check(all(s in AUTO_STEPS for s in seen), "only round steps chosen")

# zooming back IN must go fine-grained again (not a one-way ratchet)
win.main.setYRange(399.9, 400.1, padding=0)
for _ in range(3): app.processEvents()
win.refresh()
check(win.row_ticks < seen[-1],
      f"zooming back in refines the grid again ({seen[-1]} -> {win.row_ticks})")

# readout
check(win.lbl_step.text().startswith("("),
      f"toolbar reports the auto grid: {win.lbl_step.text()!r}")

# explicit selections still win and pin
for label, dollars in PRICE_STEP.items():
    if dollars < 0: continue
    win.step_combo.setCurrentText(label)
    for _ in range(2): app.processEvents()
    want = 1 if dollars <= 0 else max(1, round(dollars / TICK))
    if win.row_ticks != want or len(set(consumers())) != 1:
        FAIL.append(f"{label}: got {win.row_ticks} want {want}")
check(not [f for f in FAIL if f.startswith(tuple(PRICE_STEP))],
      "every explicit step pins all consumers correctly")
win.main.setYRange(350.0, 450.0, padding=0)
for _ in range(2): app.processEvents()
win.refresh()
check(win.row_ticks == 100, "an explicit $1 does NOT drift when you zoom")
check(win.lbl_step.text() == "", "readout blank when the step is explicit")

# back to Auto, then a real paint at every zoom
win.step_combo.setCurrentText("Auto")
errs = []
for span in (0.1, 1.0, 10.0, 60.0):
    win.main.setYRange(400 - span/2, 400 + span/2, padding=0)
    for _ in range(2): app.processEvents()
    try:
        win.refresh(); win.grab()
    except Exception as e:
        errs.append(f"span {span}: {e}")
check(not errs, f"renders clean across 4 zoom levels on Auto {errs[:1]}")

# cost of resolving every refresh
win.step_combo.setCurrentText("Auto"); win.refresh()
t = time.perf_counter()
for _ in range(2000): win._active_pane.resolve_auto_step()
print(f"\n  _resolve_auto_step: {(time.perf_counter()-t)*1e6/2000:.2f} us "
      f"per refresh (80 ms timer)")

win.close()
print("\n" + ("ALL BOOKMAP-AUTO CHECKS PASSED" if not FAIL else f"FAILED: {FAIL}"))
