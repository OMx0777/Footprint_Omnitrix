"""Price alerts must fire ONCE, on a crossing, for the right symbol.

The failure that makes an alert feature useless is not missing a level - it is
firing on every print while price sits on it. A level at 400.50 with price
oscillating 400.49/400.50/400.49 would beep dozens of times a second, and you
would learn to ignore it. So the semantics are tested harder than the plumbing.
"""
import os, sys
sys.path.insert(0, __file__.rsplit("tests", 1)[0])
from omnitrix.engine.alerts import AlertBook, ABOVE, BELOW, CROSS
FAILS = []
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

# ---- it must not fire on the print that arms it ---------------------------
b = AlertBook()
b.add("QQQ", 400.50, CROSS)
check("the first print only arms, it does not fire", b.check("QQQ", 400.40) == [])
check("...and a print on the same side stays quiet", b.check("QQQ", 400.45) == [])

# ---- crossing upward -------------------------------------------------------
hit = b.check("QQQ", 400.55)
check("crossing the level fires", len(hit) == 1, f"{[a.describe() for a in hit]}")
check("it reports the price that triggered it", hit[0].fired_price == 400.55)
check("a one-shot alert disarms", not hit[0].armed)
check("...and does not fire again", b.check("QQQ", 400.60) == [])
check("...nor on the way back", b.check("QQQ", 400.30) == [])

# ---- THE annoyance case: price sitting on the level ------------------------
b2 = AlertBook()
b2.add("QQQ", 400.50, CROSS, repeating=True)
b2.check("QQQ", 400.45)
fires = 0
for px in (400.50, 400.50, 400.50, 400.50, 400.50):
    fires += len(b2.check("QQQ", px))
check("a repeating alert fires ONCE while price sits on it", fires == 1,
      f"{fires} times - anything above 1 is a beep you learn to ignore")

# ---- direction ------------------------------------------------------------
b3 = AlertBook()
b3.add("QQQ", 400.00, ABOVE)
b3.check("QQQ", 399.50)
check("an ABOVE alert ignores a fall", b3.check("QQQ", 399.00) == [])
check("an ABOVE alert fires on a rise", len(b3.check("QQQ", 400.10)) == 1)

b4 = AlertBook()
b4.add("QQQ", 400.00, BELOW)
b4.check("QQQ", 400.50)
check("a BELOW alert ignores a rise", b4.check("QQQ", 401.00) == [])
check("a BELOW alert fires on a fall", len(b4.check("QQQ", 399.90)) == 1)

# ---- the point of the feature: symbols you are NOT watching ---------------
b5 = AlertBook()
b5.add("MRVL", 90.00, CROSS)
b5.check("MRVL", 89.50)
check("another symbol's prints never trigger it", b5.check("QQQ", 90.50) == [])
hit = b5.check("MRVL", 90.20)
check("the alert fires for its own symbol", len(hit) == 1 and hit[0].symbol == "MRVL")

# ---- repeating -------------------------------------------------------------
b6 = AlertBook()
b6.add("QQQ", 400.00, CROSS, repeating=True)
b6.check("QQQ", 399.00)
n = len(b6.check("QQQ", 401.00)) + len(b6.check("QQQ", 399.00)) + len(b6.check("QQQ", 401.00))
check("a repeating alert fires on each new crossing", n == 3, f"{n} of 3")

# ---- rubbish prices must not fire anything --------------------------------
b7 = AlertBook()
b7.add("QQQ", 400.00, CROSS)
b7.check("QQQ", 399.00)
bad = (b7.check("QQQ", 0.0) + b7.check("QQQ", -1.0)
       + b7.check("QQQ", float("nan")))
check("a zero, negative or NaN print fires nothing", bad == [],
      "a price-0 record must not set off every alert on the book")
check("...and a real print still works", len(b7.check("QQQ", 400.50)) == 1)

# ---- management + persistence ---------------------------------------------
b8 = AlertBook()
a1 = b8.add("QQQ", 400.0); b8.add("SPY", 500.0); b8.add("QQQ", 401.0)
check("alerts list sorted by symbol then price",
      [(a.symbol, a.price) for a in b8.all()] ==
      [("QQQ", 400.0), ("QQQ", 401.0), ("SPY", 500.0)])
check("removing by id works", b8.remove(a1.id) and len(b8.all()) == 2)
check("active_count counts armed only", b8.active_count() == 2)
rows = b8.to_list()
b9 = AlertBook(); b9.load(rows)
check("alerts survive save/restore",
      [(a.symbol, a.price, a.direction) for a in b9.all()] ==
      [(a.symbol, a.price, a.direction) for a in b8.all()])
b9.load([{"symbol": "X"}, None, {"price": 1.0}])
check("a corrupt saved alert is skipped, not fatal", len(b9.all()) == 0)

# ---- cost: the GATE, which runs on every print before the check does ------
# This is the hole the first version of the feature fell through. The check
# below was measured and cheap; the thing GUARDING it was not, and nothing
# looked at it. active_count() > 0 goes through all() - a list build and a
# sort - so arming a single alert made every print of every symbol slower for
# the rest of the session, and a FIRED alert stayed in the book still being
# sorted. Measured per print: 0.586 us with the book empty, 5.347 us with 21
# alerts, against 0.141 us for the check it was protecting.
import time
b_gate = AlertBook()
for i in range(30):
    b_gate.add(f"G{i:02d}", 100.0 + i)
for a in b_gate.all():
    a.armed = False                       # all spent: the state it ends up in
t = time.perf_counter()
for _ in range(200000):
    bool(b_gate)
gate_ns = (time.perf_counter() - t) / 200000 * 1e9
check("the per-print gate is O(1), not a sort of the whole book",
      gate_ns < 300, f"{gate_ns:.0f} ns per print with 30 spent alerts")

b_empty = AlertBook()
t = time.perf_counter()
for _ in range(200000):
    bool(b_empty)
check("...and costs the same when nothing is armed",
      (time.perf_counter() - t) / 200000 * 1e9 < 300)

b_gate.clear()
check("clearing the book turns the gate off", bool(b_gate) is False)

b10 = AlertBook()
for i in range(50):
    b10.add(f"S{i:02d}", 100.0 + i)
t = time.perf_counter()
for i in range(200000):
    b10.check("NOALERT", 400.0)
per = (time.perf_counter() - t) / 200000 * 1e9
check("the no-alert path is cheap", per < 1500,
      f"{per:.0f} ns per print with 50 alerts on other symbols")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS: print("   -", f)
    sys.exit(1)
print("ALERTS OK")
