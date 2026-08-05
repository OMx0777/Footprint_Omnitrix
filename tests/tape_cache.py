"""The incremental tape binning must be INDISTINGUISHABLE from the naive pass.

_TapeItem._cells caches bins across frames so a dense tape is not re-folded
from scratch 9 times a second. That is only acceptable if it is exact: a bubble
whose volume disagrees with the tape is false data, and the whole point of this
overlay is telling the user what actually traded.

So this compares the cached implementation against a from-scratch reference on
every frame, through the situations that could break it:

  * live appending (the normal case),
  * scrolling left, right, and back to the live edge,
  * zooming, which changes the bin size and the visible span,
  * changing the price grid,
  * TAPE EVICTION - the deque is bounded, so on a busy feed prints fall off the
    front while the cache still holds bins built from them,
  * switching the buffer to another symbol.
"""

import os
import sys
import math
import time
import logging
import random

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.model import split_size, Trade, Aggressor
from omnitrix.render.bookmap import BubbleItem, _TRADE_SCAN_SLACK

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def reference(item, x_lo, x_hi):
    """Oracle: fold EVERY retained print, then keep the bins in view.

    Note the difference from the pass this replaced. The old code filtered
    per TRADE against the viewport, so a bin straddling the edge of the screen
    was counted only in part - and the same bubble reported a different volume
    depending on where you had scrolled. Bins are drawn at their centre, so
    that clipping never made a bubble appear or disappear; it only understated
    one. Folding whole bins is both cheaper and the honest number, and it is
    what this oracle asserts.
    """
    buf = item.buffer
    if buf is None or not buf.trades:
        return {}
    xs = item.xscale
    inv = 1.0 / max(1e-9, item.bin_cols)
    rt = max(1, int(item.row_ticks))
    cells = {}
    for x, ti, size, aggr in list(buf.trades):
        key = (int(math.floor(x * inv)), ti // rt)
        e = cells.get(key)
        if e is None:
            e = cells[key] = [0, 0]
        b, s = split_size(size, aggr, ti)
        e[0] += b
        e[1] += s
    lo_bin = math.floor((x_lo / xs) * inv) - 1
    hi_bin = math.floor((x_hi / xs) * inv) + 1
    return {k: v for k, v in cells.items() if lo_bin <= k[0] <= hi_bin}


class FakeVB:
    def __init__(self):
        self.rng = (0.0, 100.0)

    def viewRange(self):
        return [list(self.rng), [0.0, 1.0]]


app = QApplication.instance() or QApplication([])
inst = Instruments(default_tick=0.01)

# Trades are generated SYNCHRONOUSLY here rather than by a feed thread. In the
# real app every print is applied on the GUI thread (the window queues feed
# events and drains them in its own timer), so a background thread appending
# while the oracle iterates would be a race this test invented - and it would
# make the comparison meaningless, because the two sides would not be looking
# at the same tape.
rng = random.Random(23)
CLOCK = {"ms": 1_700_000_000_000}


def feed_trades(buf, n, sym="QQQ", px=400.0):
    for _ in range(n):
        CLOCK["ms"] += 12
        price = round(px + rng.randint(-40, 40) * 0.01, 2)
        aggr = rng.choice([Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN])
        buf.add_trade(Trade(symbol=sym, price=price,
                            size=rng.randint(1, 900), aggressor=aggr,
                            ts_ms=CLOCK["ms"]))


# A tape small enough that eviction is reached quickly and can be exercised.
buf = BookmapBuffer("QQQ", inst, max_trades=4000)
item = BubbleItem(0.01, buf)
vb = FakeVB()
item.getViewBox = lambda: vb
feed_trades(buf, 1500)


def compare(tag, x_lo, x_hi):
    """One frame: cached vs reference, restricted to what the caller sees."""
    # _xrange() reports (0, 0) until the item has columns, so the item has to
    # be fed the same way the real refresh feeds it.
    item.cols = item.buffer.view(1)
    vb.rng = (x_lo, x_hi)
    got = item._cells()
    want = reference(item, x_lo - 1, x_hi + 1)
    # Bins the cache folded start at its fold boundary; anything older than
    # that is legitimately absent, so only compare from there.
    lo_x = item._cache["lo_x"] if getattr(item, "_cache", None) else -1e18
    inv = 1.0 / max(1e-9, item.bin_cols)
    first = math.floor((lo_x / item.xscale) * inv)
    keys = {k for k in want if k[0] >= first}
    diff = {k: (got.get(k), want[k]) for k in keys if got.get(k) != want[k]}
    extra = {k: v for k, v in got.items() if k not in want and (v[0] + v[1]) > 0}
    return diff, extra, len(keys)


# ---- 1. live appending, following the edge --------------------------------
worst = 0
for i in range(60):
    feed_trades(buf, 25)
    hi = (buf.trades[-1][0] if buf.trades else 100.0) + 2
    d, x, n = compare("live", hi - 60, hi)
    worst = max(worst, len(d) + len(x))
    if d or x:
        check("live appending stays exact", False, f"frame {i}: {list(d.items())[:2]} extra={list(x)[:2]}")
        break
else:
    check("live appending stays exact", True, f"60 frames, {n} bins in view")

# ---- 2. the cache is actually being reused ---------------------------------
hi = buf.trades[-1][0] + 2
item.cols = buf.view(1)
vb.rng = (hi - 60, hi)
item._cells()
c1 = item._cache
item._cells()
check("the cache is reused between frames", item._cache is c1 and c1 is not None)

# ---- 3. eviction: the deque is bounded and prints fall off the front -------
before_first = buf.trade_count - len(buf.trades)
feed_trades(buf, 4000)          # guarantees the bounded deque wraps
evicted = buf.trade_count - len(buf.trades)
check("eviction actually happened in this run", evicted > 0,
      f"{evicted:,} prints dropped from the tape")

bad = 0
for i in range(40):
    feed_trades(buf, 40)
    hi = buf.trades[-1][0] + 2
    d, x, n = compare("evict", hi - 60, hi)
    if d or x:
        bad += 1
check("exact while the tape is evicting", bad == 0, f"{bad} bad frames of 40")

# ---- 4. scrolling back into evicted territory -----------------------------
oldest = buf.trades[0][0]
d, x, n = compare("back", oldest - 40, oldest + 20)
check("exact when the view reaches past the oldest retained print",
      not d and not x, f"{list(d.items())[:2]} extra={list(x)[:2]}")

# ---- 5. scroll left, right, and back to live ------------------------------
hi = buf.trades[-1][0] + 2
seq = [(hi - 60, hi), (hi - 300, hi - 120), (hi - 90, hi - 30),
       (hi - 600, hi), (hi - 60, hi), (oldest, oldest + 50), (hi - 60, hi)]
bad = []
for a, b in seq:
    d, x, n = compare("scroll", a, b)
    if d or x:
        bad.append((a, b, list(d.items())[:1], list(x)[:1]))
check("exact across scrolling in both directions", not bad, str(bad[:2]))

# ---- 6. zoom changes the bin size -----------------------------------------
bad = []
for bc in (1.0, 2.0, 5.0, 10.0, 1.0, 30.0):
    item.bin_cols = bc
    hi = buf.trades[-1][0] + 2
    d, x, n = compare("zoom", hi - 120, hi)
    if d or x:
        bad.append((bc, list(d.items())[:1]))
item.bin_cols = 1.0
check("exact across bin-size changes", not bad, str(bad[:2]))

# ---- 7. price grid changes -------------------------------------------------
bad = []
for rt in (1, 5, 10, 25, 1):
    item.row_ticks = rt
    hi = buf.trades[-1][0] + 2
    d, x, n = compare("grid", hi - 60, hi)
    if d or x:
        bad.append((rt, list(d.items())[:1]))
item.row_ticks = 1
check("exact across price-grid changes", not bad, str(bad[:2]))

# ---- 8. xscale (timeframe aggregation) -------------------------------------
bad = []
for xsv in (1.0, 0.2, 0.1, 1.0):
    item.xscale = xsv
    hi = buf.trades[-1][0] * xsv + 2
    d, x, n = compare("xscale", hi - 60, hi)
    if d or x:
        bad.append((xsv, list(d.items())[:1]))
item.xscale = 1.0
check("exact across timeframe changes", not bad, str(bad[:2]))

# ---- 9. switching the pane to another symbol ------------------------------
buf2 = BookmapBuffer("SPY", inst, max_trades=4000)
feed_trades(buf2, 900, sym="SPY", px=88.0)
item.buffer = buf2
hi = buf2.trades[-1][0] + 2
d, x, n = compare("swap", hi - 60, hi)
check("exact after the pane is repointed at another symbol",
      not d and not x, f"{list(d.items())[:2]}")

# ---- 10. a bubble's volume must not depend on where you scrolled ----------
# This is the behaviour the old per-trade viewport filter got wrong: it clipped
# bins at the edge of the screen, so the SAME print cluster reported different
# volume at different scroll positions. Nothing on a chart should change value
# because the viewport moved.
item.buffer = buf
item.bin_cols = 5.0
hi = buf.trades[-1][0] + 2
mid_key = None
readings = {}
for a, b in ((hi - 60, hi), (hi - 300, hi), (hi - 120, hi - 20), (hi - 61, hi - 1)):
    item.cols = buf.view(1)
    vb.rng = (a, b)
    cells = item._cells()
    if mid_key is None and cells:
        mid_key = max(cells, key=lambda k: sum(cells[k]))
    if mid_key in cells:
        readings.setdefault(tuple(cells[mid_key]), []).append((a, b))
item.bin_cols = 1.0
check("a bin reports the same volume from every scroll position",
      len(readings) == 1, f"{len(readings)} different readings: {list(readings)[:3]}")

# ---- 11. the cache cannot grow with uptime --------------------------------
item.buffer = buf
sizes = []
for i in range(120):
    feed_trades(buf, 30)
    hi = buf.trades[-1][0] + 2
    vb.rng = (hi - 60, hi)
    item.cols = buf.view(1)
    item._cells()
    sizes.append(len(item._cache["cells"]) if item._cache else 0)
check("the cache stays bounded as the session runs",
      max(sizes) < 20000 and sizes[-1] <= max(sizes),
      f"peak {max(sizes)} bins, final {sizes[-1]}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("TAPE CACHE EXACTNESS OK")
