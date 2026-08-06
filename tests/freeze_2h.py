"""The two-hour freeze: the tape cache rebuilding on every frame.

Reported as "it froze after 2 hours, a restart fixed it, then 2 hours again".
Reproduced here, and the timing is not a coincidence: a 60,000-print ring at
~8 prints/sec fills in 2.08 hours, and a restart empties it.

The mechanism. The cache is dropped when a print it folded has been evicted -
a strict rule, because a bin holding prints the tape no longer has would draw
volume that exists nowhere else. Once the ring is at its cap, EVERY new print
evicts one. Zoom out far enough that the fold reaches the oldest print and the
cache is invalid again one print later, so it rebuilds every single frame:

    tape FULL, zoomed out    120 ms/frame, rebuilt 8 frames of 8
    against the bookmap's     80 ms timer

The fix holds the fold back from the eviction boundary by 2% of the tape, so
eviction only invalidates once that margin is consumed. This asserts the
freeze does not come back.
"""

import os, sys, time, random, math
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.model import Trade, Aggressor
from omnitrix.render.bookmap import BubbleItem
app = QApplication([])
inst = Instruments(default_tick=0.01)
CAP = 60000
buf = BookmapBuffer("QQQ", inst, max_trades=CAP)
rng = random.Random(5)
T0 = 1_700_000_000_000

def add(n):
    global T0
    for _ in range(n):
        T0 += 120                                  # ~8 trades/sec
        buf.add_trade(Trade("QQQ", round(400+rng.randint(-60,60)*0.01,2),
                            rng.randint(1,900),
                            rng.choice([Aggressor.BUY,Aggressor.SELL,Aggressor.UNKNOWN]),
                            T0))

class VB:
    """A real viewport: 1400 px wide, which is what makes the bin-per-pixel
    rule meaningful. A stub without viewPixelSize would silently disable it."""
    def __init__(s,r): s.r=r
    def viewRange(s): return [list(s.r),[0.,1.]]
    def viewPixelSize(s): return ((s.r[1]-s.r[0])/1400.0, 0.01)

item = BubbleItem(0.01, buf)
def frame(view):
    item.cols = buf.view(1)
    item.getViewBox = lambda: VB(view)
    # _binned, not _cells. _binned is what paint() calls, so it is the whole
    # per-frame cost - the incremental fold, the cache decision AND turning
    # the result into bubbles. _cells still exists as the dict oracle the
    # exactness checks below compare against, but nothing paints through it,
    # and timing it would be measuring a path the user never waits on.
    t=time.perf_counter(); item._binned(); return (time.perf_counter()-t)*1000

print(f"tape cap {CAP:,} at ~8 trades/s = {CAP/8/3600:.2f} hours to fill")
for phase, target in (("HALF full", CAP//2), ("FULL", CAP), ("wrapped +2000", CAP+2000)):
    add(target - buf.trade_count)
    hi = buf.trades[-1][0]; oldest = buf.trades[0][0]
    for label, view in (("zoomed IN  (recent 60 cols)", (hi-60, hi+2)),
                        ("zoomed OUT (whole tape)",     (oldest-5, hi+2))):
        frame(view)                                  # warm
        # LIVE CONDITIONS: trades keep arriving between frames, which is the
        # only thing that moves first_abs and can invalidate the cache.
        ts = []
        rebuilds = 0
        for _ in range(8):
            add(80)                                  # ~10 s of trades
            prev = id(item._cache["keys"]) if item._cache else 0
            hi2 = buf.trades[-1][0]
            v = (view[0], hi2 + 2) if view[1] > 1e9 else view
            ts.append(frame(v))
            if item._cache and id(item._cache["keys"]) != prev:
                rebuilds += 1
        print(f"  {phase:14s} {label:28s} {sum(ts)/len(ts):7.2f} ms/frame   "
              f"rebuilt {rebuilds}/8 frames")


# ---- the assertion ---------------------------------------------------------
add(200)
hi = buf.trades[-1][0]
oldest = buf.trades[0][0]
view = (oldest - 5, hi + 2)
frame(view)
worst = 0.0
rebuilds = 0
times = []
for _ in range(24):
    add(80)
    prev = id(item._cache["keys"]) if item._cache else 0
    t = frame((view[0], buf.trades[-1][0] + 2))
    times.append(t)
    worst = max(worst, t)
    if item._cache and id(item._cache["keys"]) != prev:
        rebuilds += 1

times.sort()
median = times[len(times) // 2]
ok = True
print()
# THE property. Before the fix this was 8 of 8 - every frame - which is what
# turned a 120 ms rebuild into a locked-up terminal.
if rebuilds > 3:
    print(f"  FAIL  the cache rebuilds on nearly every frame ({rebuilds}/24) "
          f"- this is the two-hour freeze")
    ok = False
else:
    print(f"  PASS  eviction no longer invalidates every frame   "
          f"rebuilt {rebuilds}/24")

# The steady state is what the user feels; a rebuild is one hitch every
# REBUILD_MARGIN_FRAC of a tape, roughly every 2.5 minutes at 8 prints/sec.
if median > 12.0:
    print(f"  FAIL  median frame {median:.1f} ms - the steady state is slow")
    ok = False
else:
    print(f"  PASS  median frame {median:.1f} ms against an 80 ms timer")

# The worst frame is the rare full rebuild - one per ~1200 prints, about every
# 2.5 minutes at 8 prints/sec. It used to be 54.9 ms, so it dropped a frame
# every time; a bisected left edge and a rebuild that stops at sorted arrays
# instead of building a 46,000-entry dict put it under the timer, which means
# there is no longer a visible hitch at all. Asserted, not merely reported:
# regressing it back over the timer is exactly the stutter that was fixed.
if worst > 80.0:
    print(f"  FAIL  worst frame {worst:.1f} ms exceeds the 80 ms timer - the "
          f"rebuild drops a frame again")
    ok = False
else:
    print(f"  PASS  worst frame {worst:.1f} ms - the periodic rebuild fits "
          f"inside the 80 ms timer, so it costs no dropped frame")
print()
print("FREEZE REGRESSION OK" if ok else "FREEZE REGRESSION FAILED")
sys.exit(0 if ok else 1)
