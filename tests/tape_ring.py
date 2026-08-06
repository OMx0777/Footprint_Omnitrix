"""The tape as a numpy ring must be INDISTINGUISHABLE from the deque it replaced.

Why it was replaced: the deque held (float, int, int, Aggressor) TUPLES, and
measured, that is 261 bytes to carry 17 bytes of data. A full 60,000-entry tape
cost 15.65 MB per symbol - 63.6% of the process footprint, about 1 GB across
100 symbols.

Why this test is long: the ring wraps. deque(maxlen=N) drops from the left and
keeps logical index 0 meaning "oldest retained"; a ring has to reproduce that
with modular arithmetic, and getting it wrong is not a crash - it is bubbles
drawn at the wrong prices, which is the failure mode this codebase cares about
most. So the comparison is against a real deque fed the identical trades,
before the wrap, exactly at it, and long after.
"""

import os
import sys
import random
import time
from collections import deque

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.model import Trade, Aggressor

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


inst = Instruments(default_tick=0.01)
CAP = 500
rng = random.Random(19)


def feed(buf, mirror, n, t0=1_700_000_000_000):
    for i in range(n):
        aggr = rng.choice([Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN])
        tr = Trade("QQQ", round(400 + rng.randint(-50, 50) * 0.01, 2),
                   rng.randint(1, 5000), aggr, t0 + i * 13)
        buf.add_trade(tr)
        ti = inst.to_index("QQQ", tr.price)
        x = (tr.ts_ms / 1000.0) / buf.col_dt
        mirror.append((x, ti, tr.size, tr.aggressor))


def compare(buf, mirror, label):
    v = buf.trades
    if len(v) != len(mirror):
        check(f"{label}: length", False, f"{len(v)} vs {len(mirror)}")
        return
    bad = None
    for i in range(len(mirror)):
        if v[i] != mirror[i]:
            bad = (i, v[i], mirror[i])
            break
    check(f"{label}: every entry matches the deque", bad is None,
          f"index {bad[0]}: {bad[1]} vs {bad[2]}" if bad else f"{len(v)} entries")


# ---- before the wrap -------------------------------------------------------
buf = BookmapBuffer("QQQ", inst, max_trades=CAP)
mirror = deque(maxlen=CAP)
feed(buf, mirror, CAP // 2)
compare(buf, mirror, "partly filled")

# ---- exactly at the wrap ---------------------------------------------------
feed(buf, mirror, CAP // 2)
compare(buf, mirror, "exactly full")
check("the ring has not wrapped yet", buf._tape_first == 0,
      f"_tape_first={buf._tape_first}")

# ---- one past ---------------------------------------------------------------
feed(buf, mirror, 1)
compare(buf, mirror, "one past the cap")
check("the oldest entry moved", buf._tape_first == 1,
      f"_tape_first={buf._tape_first}")

# ---- long after ------------------------------------------------------------
feed(buf, mirror, CAP * 3 + 137)
compare(buf, mirror, "after 4 wraps")

# ---- the access patterns the seven call sites actually use ------------------
v = buf.trades
check("bool() works", bool(v) is True)
check("[-1] is the newest", v[-1] == mirror[-1], f"{v[-1]} vs {mirror[-1]}")
check("[0] is the oldest retained", v[0] == mirror[0])
check("iteration matches", list(v) == list(mirror))
check("reversed() matches", list(reversed(v)) == list(reversed(mirror)))
check("a tail slice matches", v[-40:] == list(mirror)[-40:])
from itertools import islice
check("islice (the signals scanner) matches",
      list(islice(v, len(v) - 30, None)) == list(mirror)[-30:])
try:
    v[len(v)]
    check("an out-of-range index raises", False)
except IndexError:
    check("an out-of-range index raises", True)

# ---- an empty tape ---------------------------------------------------------
e = BookmapBuffer("EMPTY", inst, max_trades=CAP)
check("an empty tape is falsy and has length 0",
      not e.trades and len(e.trades) == 0)

# ---- the renderer must bin identically --------------------------------------
from omnitrix.render.bookmap import BubbleItem


class FakeVB:
    def __init__(self, r):
        self.r = r

    def viewRange(self):
        return [list(self.r), [0.0, 1.0]]


big = BookmapBuffer("QQQ", inst, max_trades=4000)
mirror2 = deque(maxlen=4000)
feed(big, mirror2, 9000)                      # wrapped twice

item = BubbleItem(0.01, big)
item.cols = big.view(1)
lo = big.trades[0][0]
hi = big.trades[-1][0]
vb = FakeVB((lo - 5, hi + 5))
item.getViewBox = lambda: vb
got = item._cells()

# the oracle, straight off the mirror deque
import math
from omnitrix.engine.model import split_size
want = {}
# The renderer holds its fold back from the eviction boundary (see
# REBUILD_MARGIN_FRAC) so that eviction does not invalidate the cache on every
# print - that was the two-hour freeze. It also bins at the effective width it
# chose. The oracle has to do BOTH, or it is describing a renderer that does
# not exist.
inv = 1.0 / max(1e-9, item._eff_bin)
rt = max(1, int(item.row_ticks))
_c = item._cache
_entries = list(mirror2)
_first_abs = big.trade_count - len(_entries)
if _c is not None:
    _entries = _entries[max(0, _c["fold_start"] - _first_abs):]
for x, ti, size, aggr in _entries:
    key = (int(math.floor(x * inv)), ti // rt)
    c = want.setdefault(key, [0, 0])
    b, sl = split_size(size, aggr, ti)
    c[0] += b
    c[1] += sl
diff = {k: (got.get(k), want[k]) for k in want if got.get(k) != want[k]}
check("the renderer bins the ring exactly as it binned the deque",
      not diff, f"{len(diff)} differing bins: {list(diff.items())[:2]}")
check("...over a tape that wrapped twice",
      big.trade_count == 9000 and len(big.trades) == 4000,
      f"{big.trade_count} written, {len(big.trades)} retained")

# ---- and the point of the exercise -----------------------------------------
arrays = (big.trade_x.nbytes + big.trade_ti.nbytes
          + big.trade_sz.nbytes + big.trade_ag.nbytes)
tuples = sum(sys.getsizeof(e) + sum(sys.getsizeof(v) for v in e)
             for e in mirror2)
check("the ring is far smaller than the deque it replaced",
      arrays * 4 < tuples,
      f"{arrays/1e3:.1f} kB of arrays vs {tuples/1e3:.1f} kB of tuples "
      f"({tuples/max(arrays,1):.1f}x)")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("TAPE RING OK")
