"""_binned and _by_bin, vectorised, must draw exactly what the dict drew.

WHY THIS EXISTS. The tape's hot path used to end in a Python loop over a
{(time_bin, price_bucket): [buy, sell]} dict. Measured on a full 60,000-print
tape zoomed out, 45,315 bins:

    building the dict                        29.1 ms   (per rebuild)
    _binned walking it                       54.2 ms   (per FRAME)

so it was replaced by sorted numpy arrays plus a small dict of the prints
folded since the last rebuild. Two sources now have to be merged on every
frame, and the way that goes wrong is not a crash:

  * merge the pending dict into the cache's arrays IN PLACE and the same
    volume is added again next frame. That is the doubling bug this file has
    already produced once, and it reads as a bubble twice its true size;
  * miss the pending dict entirely and the newest prints - the ones being
    traded off - silently do not appear;
  * mis-sign the price bucket when unpacking the low 32 bits of the key and a
    bubble lands billions of ticks away.

None of those raise. So the test is an oracle: the dict formulation, written
out longhand, against what the arrays produce.

tests/tape_cache.py already proves _cells (the grouping) is exact. This proves
the two functions BETWEEN that grouping and the screen are.
"""

import os
import sys
import random

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.model import Trade, Aggressor, split_size
from omnitrix.render.bookmap import BubbleItem, PieItem

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


class VB:
    """1400 px wide, so the bin-per-pixel rule is actually exercised."""

    def __init__(self, r):
        self.r = r

    def viewRange(self):
        return [list(self.r), [0.0, 1.0]]

    def viewPixelSize(self):
        return ((self.r[1] - self.r[0]) / 1400.0, 0.01)


inst = Instruments(default_tick=0.01)
rng = random.Random(1071)
T0 = 1_700_000_000_000


def feed(buf, n):
    global T0
    for _ in range(n):
        T0 += 120
        buf.add_trade(Trade("QQQ", round(400 + rng.randint(-70, 70) * 0.01, 2),
                            rng.randint(1, 4000),
                            rng.choice([Aggressor.BUY, Aggressor.SELL,
                                        Aggressor.UNKNOWN]), T0))


# ---- the oracles: the dict formulation, longhand ---------------------------


def oracle_candidates(item):
    """Every cell _binned could draw, before the max_cells cut."""
    cells = item._cells()
    rt = max(1, int(item.row_ticks))
    bc, xs, tick = item._eff_bin, item.xscale, item.tick
    out = []
    for (xb, tb), (b, s) in cells.items():
        tot = b + s
        if tot < item.min_size or tot <= 0:
            continue
        out.append(((xb + 0.5) * bc * xs, (tb + 0.5) * rt * tick, b, s, tot))
    return out


def oracle_pie(item):
    cells = item._cells()
    rt = max(1, int(item.row_ticks))
    bins = {}
    for (xb, tb), (b, s) in cells.items():
        price = (tb + 0.5) * rt * item.tick
        e = bins.get(xb)
        if e is None:
            e = bins[xb] = [0, 0, 0.0]
        e[0] += b
        e[1] += s
        e[2] += price * (b + s)
    out = []
    for xb, (b, s, pv) in bins.items():
        tot = b + s
        if tot < item.min_size or tot <= 0:
            continue
        out.append(((xb + 0.5) * item.bin_cols * item.xscale, pv / tot, b, s, tot))
    return out


def compare(label, got, cand, cap):
    """got must be the `cap` largest of cand, ascending, with nothing invented.

    Tie-tolerant at the cut only: where several cells have identical totals,
    which survives is arbitrary. It was arbitrary before too - decided by dict
    insertion order - so pinning it would be testing an accident. Everything
    else is exact.
    """
    want_n = min(len(cand), cap)
    if len(got) != want_n:
        check(f"{label}: draws the right number", False, f"{len(got)} vs {want_n}")
        return
    # nothing invented: every drawn cell is a real cell, to the bit
    pool = {}
    for c in cand:
        pool[c] = pool.get(c, 0) + 1
    invented = [g for g in got if pool.get(g, 0) == 0]
    check(f"{label}: every drawn cell exists in the dict formulation",
          not invented, f"{len(invented)} invented, e.g. {invented[:1]}")
    # the same volumes, in the same order
    tots = sorted(c[4] for c in cand)[-want_n:]
    check(f"{label}: the same totals survive the cut",
          sorted(g[4] for g in got) == tots)
    check(f"{label}: largest last, so big bubbles draw on top",
          all(got[i][4] <= got[i + 1][4] for i in range(len(got) - 1)))


# ---- exercise it across the states that matter ------------------------------
CAP = 6000
buf = BookmapBuffer("QQQ", inst, max_trades=CAP)
item = BubbleItem(0.01, buf)
pie = PieItem(0.01, buf)

STAGES = (("cold, partly filled", CAP // 3),
          ("at the cap", CAP),
          ("wrapped, evicting", CAP + 2500))

for label, target in STAGES:
    feed(buf, max(0, target - buf.trade_count))
    for zoom, mk in (("zoomed OUT", lambda: (buf.trades[0][0] - 5, buf.trades[-1][0] + 2)),
                     ("zoomed IN", lambda: (buf.trades[-1][0] - 40, buf.trades[-1][0] + 2))):
        for min_size in (0, 400):
            item.cols = pie.cols = buf.view(1)
            item.min_size = pie.min_size = min_size
            v = mk()
            item.getViewBox = pie.getViewBox = lambda v=v: VB(v)
            # A few live prints between the fold and the draw, so the pending
            # dict is non-empty - the merge is the whole point of the refactor
            # and an empty one would not exercise it.
            feed(buf, 40)
            item.cols = pie.cols = buf.view(1)
            got = item._binned()
            compare(f"{label} / {zoom} / min_size={min_size}",
                    got, oracle_candidates(item), item.max_cells)
            gp = pie._by_bin()
            compare(f"{label} / {zoom} / min_size={min_size} / pie",
                    gp, oracle_pie(pie), pie.max_cells)
    item.min_size = pie.min_size = 0

# ---- the pending merge must not accumulate ---------------------------------
# Reading the same unchanged tape twice must give the same answer. If the merge
# added pending volume into the cache arrays in place, the second read would be
# larger - which is exactly how the earlier doubling bug presented.
item.min_size = 0
v = (buf.trades[0][0] - 5, buf.trades[-1][0] + 2)
item.getViewBox = lambda: VB(v)
item.cols = buf.view(1)
a = item._binned()
b = item._binned()
c = item._binned()
check("re-reading an unchanged tape gives an identical answer",
      a == b == c, f"{len(a)}/{len(b)}/{len(c)} cells")

# ---- and the volume is the tape's volume, not a multiple of it -------------
# The one thing an oracle built from _cells cannot catch, because it shares
# _cells: does the cache hold the volume the TAPE holds?
cells = item._cells()
cached = sum(sum(v) for v in cells.values())
cch = item._cache
first_abs = buf.trade_count - len(buf.trades)
tape = 0
for i in range(max(0, cch["fold_start"] - first_abs), len(buf.trades)):
    _, ti, size, aggr = buf.trades[i]
    bb, ss = split_size(size, aggr, ti)
    tape += bb + ss
check("the cache holds exactly the tape's volume over the folded span",
      cached == tape, f"{cached:,} cached vs {tape:,} on the tape")

# ---- a negative price bucket must survive the round trip -------------------
# The bucket lives in the low 32 bits as two's complement. Masking without the
# int32 cast reads -3 as 4,294,967,293 and puts the bubble off the planet.
low = BookmapBuffer("LOW", Instruments(default_tick=1.0), max_trades=500)
li = BubbleItem(1.0, low)
li.row_ticks = 4
for k in range(60):
    T0 += 120
    low.add_trade(Trade("LOW", -3.0 - (k % 5), 100 + k, Aggressor.BUY, T0))
li.cols = low.view(1)
lv = (low.trades[0][0] - 2, low.trades[-1][0] + 2)
li.getViewBox = lambda: VB(lv)
neg = [k for k in li._cells() if k[1] < 0]
check("a negative price bucket survives the key packing",
      bool(neg) and all(-100 < k[1] < 0 for k in neg),
      f"buckets {sorted({k[1] for k in neg})}")
check("...and _binned puts it at a negative price",
      bool(li._binned()) and all(p < 0 for _, p, _, _, _ in li._binned()))

# ---- the left edge, bisected, must land where the scan landed --------------
# _left_edge replaced a newest-first Python scan (13.3 ms of a 24.2 ms rebuild
# at the 60,000 cap). Off by one and the rebuild folds a print the incremental
# loop folds again, or drops one nothing else will ever fold.
from omnitrix.render.bookmap import _left_edge

bad = []
for cap_n in (64, 500, 1000):
    rb = BookmapBuffer("RB", inst, max_trades=cap_n)
    for total in (cap_n // 3, cap_n, cap_n + 1, cap_n * 2 + 7):
        feed(rb, max(0, total - rb.trade_count))
        tx = rb.trade_x
        n = min(rb.trade_count, cap_n)
        base = rb._tape_first
        # every retained x, plus the gaps between them and both open ends
        targets = [tx[(base + k) % cap_n] for k in range(n)]
        targets += [t + 0.5 for t in targets] + [targets[0] - 99, targets[-1] + 99]
        for target in targets:
            want = n
            for k in range(n - 1, -1, -1):        # the scan, verbatim
                if tx[(base + k) % cap_n] < target:
                    break
                want = k
            got = _left_edge(tx, base, cap_n, n, target)
            if got != want:
                bad.append((cap_n, total, target, got, want))
check("the bisected left edge is where the linear scan stopped",
      not bad, f"{len(bad)} mismatches, e.g. {bad[:2]}" if bad
      else "across unwrapped, exactly-full and twice-wrapped rings")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("BINNED EXACTNESS OK")
