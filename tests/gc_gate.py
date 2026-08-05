"""The cycle collector must not stall the UI for work it never finds.

A gen-2 sweep walks every tracked object. A busy desk holds ~877,000 of them
and the sweep takes 139 ms - four frames - and at default settings it fires
about nineteen times a minute. That is a large part of "it goes sluggish after
a while": the longer the session runs, the more objects exist, and the longer
each sweep takes.

Raising the threshold is only safe if the collector is not actually reclaiming
anything, so that is what this asserts. If our structures ever start forming
reference cycles, this test fails and the tuning must be revisited - it would
mean real memory is being held.
"""
import gc
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import Instruments, SyntheticFeed, BookmapBuffer, BarSeries

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# ---- the app must actually apply the tuning --------------------------------
from omnitrix.app import _tune_gc
_tune_gc()
t = gc.get_threshold()
check("the app raises the gen-2 threshold", t[2] >= 200,
      f"thresholds {t} - default is (700, 10, 10)")
check("...but does NOT disable collection", gc.isenabled() and t[2] < 10 ** 9,
      "Qt can create cycles; they must still be reclaimed, just not mid-frame")

# ---- the premise: our data creates no cycles -------------------------------
inst = Instruments(default_tick=0.01)
SYMS = [f"S{i:02d}" for i in range(30)]
bufs = {s: BookmapBuffer(s, inst) for s in SYMS}
ser = {s: BarSeries(s, inst) for s in SYMS}
for b in bufs.values():
    b.max_cols = 400                      # force eviction during the run
feed = SyntheticFeed(symbols=SYMS, start_price=400.0, tick=0.01,
                     trades_per_sec=800, prefill_minutes=1, seed=3)
feed.on_trade(lambda tr: (bufs[tr.symbol].add_trade(tr),
                          ser[tr.symbol].add_trade(tr)))
feed.on_book(lambda b: bufs[b.symbol].add_book(b))
gc.collect()
feed.start()
time.sleep(8)
feed.stop()

cyclic = gc.collect()
check("the engine creates NO reference cycles", cyclic == 0,
      f"{cyclic} cyclic objects - if this is non-zero the threshold tuning is "
      f"holding real memory and must be reconsidered")

# ---- and the sweep is expensive enough to be worth avoiding ---------------
best = 1e9
for _ in range(4):
    t0 = time.perf_counter()
    gc.collect(2)
    best = min(best, time.perf_counter() - t0)
ms = best * 1000
n = len(gc.get_objects())
# Reported, not asserted. How long a sweep takes depends on how much data this
# test happened to build, so gating on it makes the gate flaky rather than
# meaningful - the assertion that matters is the zero-cycles one above, because
# that is what makes the tuning safe.
print(f"  INFO   gen-2 sweep {ms:.1f} ms over {n:,} objects "
      f"({ms/33*100:.0f}% of a 33 ms frame)")

del bufs, ser, feed
gc.collect()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("GC GATE OK")
