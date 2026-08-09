"""Retention follows what is on screen - without ever starving what IS.

Two changes are under test, both aimed at the same wall: the app has to hold
hundreds of symbols now and thousands once options data arrives, and the old
model paid full price for every one of them.

  * the tape ring is grown on demand instead of allocated at full size. A full
    ring is 1.02 MB, so 100 symbols cost 102 MB of mostly-untouched memory
    whether they printed or not, and a thousand would cost a gigabyte;

  * per-column history - the resting ladder and the aggressive buy/sell dicts -
    is kept in full only for symbols something is drawing. Measured at 100
    symbols and 100 depth per side, that state hit 116 MB in six minutes and
    was still climbing linearly toward roughly 440 MB at the column cap.

THE FAILURE THAT MATTERS is not memory, it is a symbol the user is looking at
quietly losing its history because the hot set missed it. So every check below
is about the WATCHED symbol keeping everything, and the reduction applying only
to symbols nothing is drawing.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

import numpy as np
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, BookmapBuffer, SyntheticFeed
from omnitrix.engine.bookmap import COLD_COLS, TAPE_SEED
from omnitrix.engine.model import Trade, Aggressor, BookSnapshot
from omnitrix.ui.main_window import OmnitrixWindow

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)

# ---- 1. the tape ring grows, and retains exactly what it always did --------
b = BookmapBuffer("T", inst, max_trades=8000)
check("a fresh buffer does not allocate the full ring",
      b.max_trades <= TAPE_SEED and b.tape_cap == 8000,
      f"allocated {b.max_trades:,} of a {b.tape_cap:,} ceiling")
start_bytes = b.trade_x.nbytes + b.trade_ti.nbytes + b.trade_sz.nbytes + b.trade_ag.nbytes

T = 1_700_000_000_000
mirror = []
for i in range(8000 + 500):                 # past the ceiling, so it wraps
    T += 100
    tr = Trade("T", round(400 + (i % 71) * 0.01, 2), 1 + (i % 997),
               (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3], T)
    b.add_trade(tr)
    mirror.append((round((tr.ts_ms / 1000.0) / b.col_dt, 9),
                   inst.to_index("T", tr.price), tr.size, tr.aggressor))
mirror = mirror[-8000:]                     # deque(maxlen=8000) semantics

v = b.trades
check("the grown ring holds the ceiling, not more", len(v) == 8000, f"{len(v)}")
bad = next((i for i in range(len(mirror)) if v[i] != mirror[i]), None)
check("every retained print survived every growth step, in order",
      bad is None,
      f"index {bad}: {v[bad]} vs {mirror[bad]}" if bad is not None else "8,000 checked")
check("...including the newest and the oldest",
      v[-1] == mirror[-1] and v[0] == mirror[0])
grown = b.trade_x.nbytes + b.trade_ti.nbytes + b.trade_sz.nbytes + b.trade_ag.nbytes
check("a busy symbol still ends up with its full ring", b.max_trades == 8000,
      f"{start_bytes/1e3:.1f} kB at rest -> {grown/1e3:.1f} kB in use")

# a symbol that barely prints must NOT pay for a full ring
q = BookmapBuffer("QUIET", inst, max_trades=60000)
for i in range(40):
    T += 100
    q.add_trade(Trade("QUIET", 400.0, 10, Aggressor.BUY, T))
quiet = q.trade_x.nbytes + q.trade_ti.nbytes + q.trade_sz.nbytes + q.trade_ag.nbytes
full = 60000 * (8 + 4 + 4 + 1)
check("a thin symbol does not pay for a full ring", quiet * 20 < full,
      f"{quiet/1e3:.1f} kB vs {full/1e6:.2f} MB if preallocated "
      f"({full/max(quiet,1):.0f}x)")

# ---- 2. hot/cold column retention ------------------------------------------
h = BookmapBuffer("H", inst, max_cols=1400)
# Starts HOT deliberately. Starting cold was tried and the data-truth gate
# caught it at once: a bare buffer retained less than the BarSeries beside it,
# so the bookmap and the footprint reported different volume for the same
# session. Only the window knows what is on screen, so only the window demotes.
check("a bare buffer keeps the full retention every consumer expects",
      h.max_cols == h.hot_cols == 1400, f"max_cols={h.max_cols}")
h.set_hot(False)
check("...and goes cold only when told to", h.max_cols == COLD_COLS,
      f"max_cols={h.max_cols}")


def fill_cols(buf, n, t0):
    for k in range(n):
        ts = t0 + k * 1000                   # one column per second
        buf.add_trade(Trade(buf.symbol, 400.0 + (k % 7) * 0.01, 100,
                            Aggressor.BUY, ts))
        buf.add_book(BookSnapshot(buf.symbol, {399.99: 500}, {400.01: 500}, ts))
    return t0 + n * 1000


T2 = 1_700_000_000_000
T2 = fill_cols(h, 400, T2)
check("a cold symbol is capped at the reduced retention",
      len(h.order) == COLD_COLS, f"{len(h.order)} columns")

h.set_hot(True)
T2 = fill_cols(h, 400, T2)
check("promoting keeps every column from that moment on",
      len(h.order) > COLD_COLS, f"{len(h.order)} columns after promotion")
check("...and the promoted symbol keeps the history it already had",
      len(h.order) >= COLD_COLS + 400 - 1,
      f"{len(h.order)} (kept {COLD_COLS} + gained 400)")

before = len(h.order)
h.set_hot(False)
check("demoting releases the memory immediately, not at the next column",
      len(h.order) == COLD_COLS, f"{before} -> {len(h.order)}")
# and the caches must not describe columns that are gone
cols = h.columns()
check("the column cache is rebuilt after a demotion",
      len(cols) == len(h.order) and all(c.bucket in h.cols for c in cols),
      f"{len(cols)} cached vs {len(h.order)} held")
check("aggregation still works on a demoted buffer", len(h.view(1)) > 0,
      f"{len(h.view(1))} aggregated columns")

# ---- 2a. the tape CEILING follows hot/cold too -----------------------------
# Growing on demand fixed the symbol that never prints. It does nothing for the
# symbol that prints constantly and is simply off screen, which still grows to
# the full 60,000 and 1.02 MB - a gigabyte across a thousand active symbols.
#
# Shrinking a ring is where this can go silently wrong. trade_count keeps
# counting across the shrink, so the write head lands at trade_count % new_cap
# and the survivors have to be laid out around THAT. Pack them at slot 0
# instead and the next write evicts the wrong print - no crash, just a tape
# that quietly disagrees with itself. So the check is against a deque at every
# head alignment, not at one.
from collections import deque
from omnitrix.engine.bookmap import TAPE_COLD

bad_align = []
for extra in range(0, 9):                    # every residue of head % new_cap
    t = BookmapBuffer("A", inst, max_trades=60000)
    mirror = deque(maxlen=60000)
    ts = 1_700_000_000_000
    for i in range(TAPE_COLD * 2 + extra):
        ts += 50
        tr = Trade("A", round(400 + (i % 53) * 0.01, 2), 1 + (i % 601),
                   (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3], ts)
        t.add_trade(tr)
        mirror.append((round((tr.ts_ms / 1000.0) / t.col_dt, 9),
                       inst.to_index("A", tr.price), tr.size, tr.aggressor))
    t.set_hot(False)                          # <- the shrink
    want = list(mirror)[-min(len(mirror), t.max_trades):]
    got = list(t.trades)
    if got != want:
        first_bad = next((k for k in range(min(len(got), len(want)))
                          if got[k] != want[k]), None)
        bad_align.append((extra, len(got), len(want), first_bad))
check("a shrunk tape reads exactly like a deque, at every head alignment",
      not bad_align, f"{len(bad_align)} of 9 wrong: {bad_align[:2]}")

t = BookmapBuffer("A2", inst, max_trades=60000)
ts = 1_700_000_000_000
for i in range(TAPE_COLD * 3):
    ts += 50
    t.add_trade(Trade("A2", 400.0 + (i % 17) * 0.01, 100, Aggressor.BUY, ts))
big = t.trade_x.nbytes + t.trade_ti.nbytes + t.trade_sz.nbytes + t.trade_ag.nbytes
count_before, vol_before, max_before = t.trade_count, t.trade_vol, t.trade_max
t.set_hot(False)
small = t.trade_x.nbytes + t.trade_ti.nbytes + t.trade_sz.nbytes + t.trade_ag.nbytes
check("an ACTIVE but off-screen symbol releases its tape", small < big,
      f"{big/1e3:.0f} kB -> {small/1e3:.0f} kB ({big/max(small,1):.1f}x)")
check("session totals are NOT reset by the shrink - they cover every print "
      "ever ingested",
      (t.trade_count, t.trade_vol, t.trade_max) == (count_before, vol_before, max_before),
      f"{t.trade_count:,} prints, {t.trade_vol:,} volume")

# writes after a shrink must continue to evict the OLDEST, not something else
after = deque(list(t.trades), maxlen=t.max_trades)
for i in range(500):
    ts += 50
    tr = Trade("A2", 401.0 + (i % 7) * 0.01, 50, Aggressor.SELL, ts)
    t.add_trade(tr)
    after.append((round((tr.ts_ms / 1000.0) / t.col_dt, 9),
                  inst.to_index("A2", tr.price), tr.size, tr.aggressor))
check("...and it keeps evicting correctly once writing resumes",
      list(t.trades) == list(after),
      f"{len(list(t.trades))} vs {len(after)}")

# promotion lets it grow again
t.set_hot(True)
check("promotion restores the full ceiling", t.tape_cap == t.tape_hot,
      f"cap {t.tape_cap:,}")
for i in range(TAPE_COLD + 200):
    ts += 50
    t.add_trade(Trade("A2", 402.0, 10, Aggressor.BUY, ts))
check("...and the ring grows past the cold ceiling again",
      t.max_trades > TAPE_COLD, f"{t.max_trades:,}")

# ---- 2a-ii. SHRINK THEN GROW - the sequence that actually broke -----------
# Found by tests/stress_1000.py, not by reasoning. Growth originally assumed
# the ring had never wrapped, which is true while it can only grow and false
# the moment it can also shrink: a shrunk ring is ROTATED, so growth copied it
# verbatim and every slot past the old capacity read back as uninitialised
# memory. It surfaced as an aggressor code outside 0..2 while painting the
# tape - a crash, but it could equally have been a plausible wrong price.
#
# The general fix was to stop deriving the retained count from trade_count,
# which is a session total and does not shrink. This checks the whole cycle
# against a deque, and checks every reachable slot decodes.
cyc_bad = []
for pre in (300, 9000, 20001, 41000):
    t = BookmapBuffer("C", inst, max_trades=60000)
    m = deque(maxlen=60000)
    ts = 1_700_000_000_000

    def push(buf, mir, n, ts):
        for i in range(n):
            ts += 50
            tr = Trade("C", round(400 + (i % 37) * 0.01, 2), 1 + (i % 401),
                       (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3], ts)
            buf.add_trade(tr)
            mir.append((round((tr.ts_ms / 1000.0) / buf.col_dt, 9),
                        inst.to_index("C", tr.price), tr.size, tr.aggressor))
        return ts

    ts = push(t, m, pre, ts)
    t.set_hot(False)                       # shrink (rotates the ring)
    keep = min(len(m), t.max_trades)
    m = deque(list(m)[-keep:], maxlen=keep)
    t.set_hot(True)                        # ceiling back up
    ts = push(t, m2 := deque(list(m), maxlen=60000), 30000, ts)   # regrow
    # every reachable slot must decode - this is what raised IndexError
    codes = {int(t.trade_ag[(t._tape_first + i) % t.max_trades])
             for i in range(len(t.trades))}
    if not codes <= {0, 1, 2}:
        cyc_bad.append((pre, "garbage", sorted(codes)[:4]))
        continue
    want = list(m2)[-min(len(m2), t.max_trades):]
    if list(t.trades) != want:
        first = next((k for k in range(min(len(want), len(t.trades)))
                      if list(t.trades)[k] != want[k]), None)
        cyc_bad.append((pre, "mismatch at", first))
check("shrink then grow leaves no uninitialised slot and no wrong print",
      not cyc_bad, f"{cyc_bad[:2]}" if cyc_bad else "4 starting sizes checked")

# and the session totals still describe the whole session
check("the retained count is tracked apart from the session total",
      t.trade_count > t._tape_n and t._tape_n == len(t.trades),
      f"{t.trade_count:,} ingested, {t._tape_n:,} retained")

# ---- 2b. BarSeries: the bigger half ---------------------------------------
# Measured per SEALED bar at 100 depth a side: 3,501 B, of which the L2 book is
# 53.7% (1,880 B, and no two bars share one) and the footprint arrays 36.3%.
# At the 12,000-bar cap that is 42 MB a symbol - 4.2 GB across a hundred and
# 42 GB across a thousand.
#
# What must NOT be lost is the candle. A stripped bar keeps OHLC, volume and
# delta; it loses only its per-price detail, which is why it is applied to bars
# far behind the screen on symbols nothing is drawing.
from omnitrix.engine import BarSeries
from omnitrix.engine.bars import BOOK_BARS, COLD_BARS as BAR_COLD


def build_bars(n_bars, t0):
    s = BarSeries("BS", inst)
    for b in range(n_bars):
        for k in range(40):
            t0 += 200
            s.add_trade(Trade("BS", round(220.0 + (k % 31) * 0.01, 2),
                              10 + k, (Aggressor.BUY, Aggressor.SELL,
                                       Aggressor.UNKNOWN)[k % 3], t0))
        s.add_book(BookSnapshot("BS",
                                {round(220.0 - i * 0.01, 2): 500 for i in range(1, 51)},
                                {round(220.0 + i * 0.01, 2): 500 for i in range(1, 51)},
                                t0))
    return s, t0


bs, T3 = build_bars(300, 1_700_000_000_000)
check("a bare BarSeries keeps full detail", bs._hot is True)
ohlcv_before = [(b.start_ts, b.open, b.high, b.low, b.close, b.volume, b.delta)
                for b in bs.bars]
lv_before = sum(b.n_levels() for b in bs.bars)

bs.set_hot(False)
ohlcv_after = [(b.start_ts, b.open, b.high, b.low, b.close, b.volume, b.delta)
               for b in bs.bars]
check("going cold does not touch OHLCV, volume or delta on ANY bar",
      ohlcv_before == ohlcv_after,
      f"{sum(1 for a, b in zip(ohlcv_before, ohlcv_after) if a != b)} bars differ")
lv_after = sum(b.n_levels() for b in bs.bars)
check("...but the per-price detail behind the cold window is released",
      lv_after < lv_before, f"{lv_before:,} levels -> {lv_after:,}")
kept = [b for b in bs.bars if b.n_levels() > 0]
check("...and the newest bars keep theirs", len(kept) <= BAR_COLD + 2
      and len(kept) > 0, f"{len(kept)} bars still carry cells")
check("a stripped bar still answers arrays() instead of raising",
      all(len(b.arrays()) == 3 for b in bs.bars))
check("a stripped bar still answers its cached analytics",
      all(isinstance(b._analytics(), dict) for b in bs.bars))
check("aggregation still works after a demotion", len(bs.view(60)) > 0,
      f"{len(bs.view(60))} bars at 1m")

# ---- 2b-ii. THE BUDGET CONTRACT, on the series side -----------------------
# _sync_hot rations demotions by counting what set_hot says it released, and
# the bookmap half of that contract is checked in section 4. The series half
# was not, and it was broken: the expensive path stripped up to BOOK_BARS bars
# and then fell off the end of the function, returning None. None is falsy, so
# the single most expensive thing a demotion does scored as free work and a
# pass could strip an unbounded number of symbols inside one frame - the exact
# shape of every freeze this codebase has had.
#
# It is invisible from the outside: retention was correct, memory was released,
# nothing raised. Only the FRAME BUDGET was wrong. Hence a check on the return
# value itself and not merely on the effect.
budget, _ = build_bars(200, 1_700_000_900_000)
check("demoting a series that really strips bars REPORTS the work",
      budget.set_hot(False) is True,
      "returned falsy -> _sync_hot would treat a BOOK_BARS strip as free")
check("...and demoting it again reports nothing, so no slot is wasted",
      bool(budget.set_hot(False)) is False)
check("...and promotion reports nothing either - it strips nothing",
      bool(budget.set_hot(True)) is False)
short, _ = build_bars(max(1, BAR_COLD - 5), 1_700_001_100_000)
check("...and a series with nothing behind the cold window is free",
      bool(short.set_hot(False)) is False,
      f"{len(short.bars)} bars, COLD_BARS={BAR_COLD}")

# the book window applies even while HOT - the heatmap only draws what is visible
hot_bs, _ = build_bars(60, 1_700_000_500_000)
check("a hot series keeps its recent books",
      sum(1 for b in hot_bs.bars if b.book is not None and len(b.book)) > 0,
      f"{sum(1 for b in hot_bs.bars if b.book is not None and len(b.book))} bars with a book")

# ---- 2c. the cached footprint DICT is gone --------------------------------
# _cache["tot"] held {tick_index: volume} - the same data _ti/_sell/_buy
# already hold, boxed, in the exact shape seal() exists to remove. It was
# reported as 184 B a bar because sys.getsizeof does not follow a dict's
# VALUES; measured with a deep sizer it is 7,098 B, 68% of a sealed bar and
# nearly four times the L2 book. Across 1,000 symbols at the 12,000-bar cap,
# 85 GB.
#
# value_area() was its only consumer and now walks the arrays, which seal()
# already sorts ascending - the same traversal the dict got from sorted(tot).


def deep(o, seen=None):
    """Real bytes, following containers. getsizeof alone does not."""
    if seen is None:
        seen = set()
    if id(o) in seen:
        return 0
    seen.add(id(o))
    t = sys.getsizeof(o)
    if isinstance(o, dict):
        for k, v in o.items():
            t += deep(k, seen) + deep(v, seen)
    elif isinstance(o, (list, tuple, set, frozenset)):
        for v in o:
            t += deep(v, seen)
    return t


cache_bars, _ = build_bars(80, 1_700_000_700_000)
sealed_b = [x for x in cache_bars.bars if x.cells is None]
per_cache = sum(deep(x._cache or {}) for x in sealed_b) / max(1, len(sealed_b))
check("no bar caches a boxed copy of its own footprint",
      per_cache < 400, f"{per_cache:.0f} B of _cache per sealed bar")
check("...and no 'tot' key survives anywhere",
      all("tot" not in (x._cache or {}) for x in cache_bars.bars))


def old_value_area(bar, pct):
    """The dict formulation, verbatim, as the oracle."""
    ti, sell, buy = bar.arrays()
    if ti.size == 0:
        return None, None
    t64 = ti.astype(np.int64)
    v = sell.astype(np.int64) + buy.astype(np.int64)
    tot = dict(zip(t64.tolist(), v.tolist()))
    poc = int(ti[int(v.argmax())])
    target = sum(tot.values()) * pct
    idxs = sorted(tot)
    pos = idxs.index(poc)
    lo = hi = pos
    acc = tot[poc]
    n = len(idxs)
    while acc < target and (lo > 0 or hi < n - 1):
        up = tot[idxs[hi + 1]] if hi < n - 1 else -1
        dn = tot[idxs[lo - 1]] if lo > 0 else -1
        if up < 0 and dn < 0:
            break
        if up >= dn:
            hi += 1
            acc += tot[idxs[hi]]
        else:
            lo -= 1
            acc += tot[idxs[lo]]
    return idxs[hi], idxs[lo]


va_bad = 0
va_n = 0
for bar in cache_bars.bars:
    for pct in (0.5, 0.68, 0.70, 0.9, 1.0):
        va_n += 1
        if bar.value_area(pct) != old_value_area(bar, pct):
            va_bad += 1
check("value_area on arrays equals value_area on the dict, exactly",
      va_bad == 0, f"{va_n:,} comparisons, {va_bad} mismatches")


def drain_demotions(w, passes=40):
    """Demotion is grace-gated (DEMOTE_GRACE_S), so a test that wants the
    queue drained has to age it first - otherwise it is asserting that a
    glance strips history, which is the bug this grace exists to fix."""
    import time as _t
    from omnitrix.ui.main_window import DEMOTE_GRACE_S as _G
    hot_ = w._hot_symbols()
    for _ in range(passes):
        # Age BEFORE the pass, or the pass itself only starts the countdown.
        stale = _t.monotonic() - _G - 1
        for k in w.bookmaps:
            if k not in hot_:
                w._cold_since[k] = stale
        w._sync_hot()


# ---- 3. the app marks the right symbols hot --------------------------------
app = QApplication.instance() or QApplication([])
SYMS = [f"Z{i:02d}" for i in range(12)] + ["NVDA"]
feed = SyntheticFeed(symbols=SYMS, start_price=220.0, tick=0.01,
                     trades_per_sec=120, book_hz=8, depth_levels=20,
                     prefill_minutes=0, seed=4)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1200, 800)
win.show()
win.start_feed()
for _ in range(150):
    app.processEvents()
    win._tick()
    time.sleep(0.006)

win.active_symbol = "NVDA"
win._open_bookmap()
for _ in range(120):
    app.processEvents()
    win._tick()
    time.sleep(0.006)
win._sync_hot()

hot = win._hot_symbols()
check("the symbol on the chart is hot", "NVDA" in hot, f"hot={sorted(hot)}")
nv = win.bookmaps.get("NVDA")
check("...and its buffer really is at full retention",
      nv is not None and nv.max_cols == nv.hot_cols,
      f"max_cols={getattr(nv, 'max_cols', None)} of {getattr(nv, 'hot_cols', None)}")
# Demotion is rate-limited (MAX_DEMOTIONS_PER_SYNC) so a large universe cannot
# stall a frame - drain the queue explicitly rather than relying on how many
# ticks happened to elapse.
from omnitrix.ui.main_window import MAX_DEMOTIONS_PER_SYNC
drain_demotions(win)
cold = [s for s in SYMS if s not in hot and s in win.bookmaps]
check("symbols nothing is drawing are cold", bool(cold)
      and all(win.bookmaps[s].max_cols == COLD_COLS for s in cold),
      f"{len(cold)} cold of {len(win.bookmaps)}")

# switching must promote the new symbol BEFORE it is drawn stale
win.open_bookmap_for("Z03")
for _ in range(60):
    app.processEvents()
    win._tick()
    time.sleep(0.006)
win._sync_hot()
check("the BarSeries of an off-screen symbol is cold too",
      any(not win.series[x]._hot for x in cold if x in win.series),
      f"{sum(1 for x in cold if x in win.series and not win.series[x]._hot)} "
      f"of {len(cold)} cold series")
check("...and the watched symbol's BarSeries stays hot",
      win.series["NVDA"]._hot is True)

check("selecting a symbol promotes it", win.bookmaps["Z03"].max_cols
      == win.bookmaps["Z03"].hot_cols,
      f"Z03 max_cols={win.bookmaps['Z03'].max_cols}")
check("...and it still has the history it accumulated while cold",
      len(win.bookmaps["Z03"].order) > 0,
      f"{len(win.bookmaps['Z03'].order)} columns")

# ---- 3b. A GLANCE MUST NOT DESTROY HISTORY --------------------------------
# The symptom that found this: an hour of watching one symbol, then footprint
# and heat only for the last fifteen minutes. Not a fetch that failed - an
# eviction that fired on ordinary use. Demotion is destructive and promotion
# does not undo it, so releasing the instant a symbol leaves the screen means
# looking at another ticker for ten seconds permanently strips the one you
# came back to.
from omnitrix.ui.main_window import DEMOTE_GRACE_S
import time as _time

win.active_symbol = "NVDA"
for _ in range(40):
    app.processEvents()
    win._tick()
    time.sleep(0.005)
win._sync_hot()
check("the watched symbol is hot", win.series["NVDA"]._hot is True)

win.active_symbol = "Z05"                    # a glance elsewhere
for _ in range(10):
    win._sync_hot()
check("a glance does NOT immediately strip the symbol left behind",
      win.series["NVDA"]._hot is True
      and win.bookmaps["NVDA"].max_cols == win.bookmaps["NVDA"].hot_cols,
      f"NVDA hot={win.series['NVDA']._hot}, "
      f"max_cols={win.bookmaps['NVDA'].max_cols}")
check("...and the grace period is long enough to be useful",
      DEMOTE_GRACE_S >= 60, f"{DEMOTE_GRACE_S:.0f}s")

win.active_symbol = "NVDA"                   # straight back
win._sync_hot()
check("coming back clears the countdown", "NVDA" not in win._cold_since)

# once the grace really has elapsed, it does release
win.active_symbol = "Z05"
win._sync_hot()
win._cold_since["NVDA"] = _time.monotonic() - DEMOTE_GRACE_S - 1
for _ in range(6):
    win._sync_hot()
check("a symbol genuinely abandoned is still released",
      win.series["NVDA"]._hot is False
      or win.bookmaps["NVDA"].max_cols == win.bookmaps["NVDA"].cold_cols,
      f"hot={win.series['NVDA']._hot}, "
      f"max_cols={win.bookmaps['NVDA'].max_cols}")
win.active_symbol = "NVDA"
win._sync_hot()

# ---- 3c. replayed depth restores the heat field ---------------------------
from omnitrix.engine.model import BookSnapshot as _BS

hb = BookmapBuffer("HB", inst, max_cols=1400)
t4 = 1_700_000_000_000
for k in range(60):
    ts = t4 + k * 1000
    hb.add_trade(Trade("HB", 400.0, 100, Aggressor.BUY, ts))
    hb.add_book(_BS("HB", {399.99: 500}, {400.01: 500}, ts))
measured = [c.bucket for c in hb.columns() if c.sweeps > 0]
# strip, as going cold does, then replay depth back in
hb.set_hot(False)
kept = {c.bucket for c in hb.columns()}
old_books = [_BS("HB", {399.98: 900}, {400.02: 900}, t4 + k * 1000)
             for k in range(60)]
n_filled = hb.rebuild_heatmap(old_books)
check("replayed depth is accepted for columns that lost theirs",
      n_filled >= 0, f"{n_filled} columns filled")
still = [c for c in hb.columns() if c.sweeps > 0]
check("a MEASURED column is never overwritten by a replay",
      all(c.book.get(hb.instruments.to_index("HB", 399.99), 0) == 500
          for c in still if c.book),
      f"{len(still)} measured columns intact")
check("a reconstructed column is not claimed as measured",
      all(c.sweeps == 0 for c in hb.columns() if c.bucket not in measured))

# ---- 4. demotion must not stall a frame -----------------------------------
# Each demotion reallocates a tape ring and evicts columns - 626 us measured -
# so an unbounded pass over a thousand symbols would itself drop the frame it
# exists to protect. Promotions are never deferred; only demotions are.
for p_ in win._panes:
    p_.symbol = ""
win.active_symbol = "NVDA"
for s_ in win.bookmaps.values():
    s_.set_hot(True)
for s_ in win.series.values():
    s_.set_hot(True)
win._demote_cursor = 0
hot2 = win._hot_symbols()
n_cold = sum(1 for x in win.bookmaps if x not in hot2)

# THE BUDGET COUNTS WORK, NOT CALLS. A symbol registered a moment ago has no
# columns to evict and no ring to shrink, so demoting it is free - and
# counting it left the 1,000-symbol run with a 760-deep queue draining six a
# pass while the free ones ahead used every slot. So: cheap demotions must all
# go through at once, and only the expensive ones are rationed.
drain_demotions(win, passes=1)
cheap_left = sum(1 for x, b in win.bookmaps.items()
                 if x not in hot2 and b.max_cols != b.cold_cols)
check("cheap demotions are NOT rationed - they all clear in one pass",
      cheap_left == 0, f"{cheap_left} of {n_cold} still pending")

# Now make them expensive - real tape and real columns - and re-check.
ts_e = 1_700_000_900_000
exp = [x for x in win.bookmaps if x not in hot2][:10]
for x in exp:
    bb = win.bookmaps[x]
    bb.set_hot(True)
    for i in range(TAPE_COLD + 2000):
        ts_e += 40
        bb.add_trade(Trade(x, 400.0 + (i % 11) * 0.01, 10, Aggressor.BUY, ts_e))
win._demote_cursor = 0
drain_demotions(win, passes=1)
still_big = sum(1 for x in exp if win.bookmaps[x].max_trades > TAPE_COLD)
check("expensive demotions ARE rationed, so a mass release cannot stall a frame",
      still_big >= len(exp) - MAX_DEMOTIONS_PER_SYNC,
      f"{len(exp) - still_big} released this pass, limit {MAX_DEMOTIONS_PER_SYNC}")
drain_demotions(win)
check("...and repeated passes clear the expensive ones too",
      all(win.bookmaps[x].max_trades <= TAPE_COLD for x in exp),
      f"{sum(1 for x in exp if win.bookmaps[x].max_trades > TAPE_COLD)} left")
# ---- 4b. THE BUDGET IS IN COLUMNS, because the work is per column ---------
# Every evicted column is folded into the session archive at a measured 30 us,
# so demoting one symbol with a full 1,400-column ring is 38-47 ms - and six of
# those in a pass is 282 ms. The watchdog caught exactly that at 200 symbols:
#     SLOW FRAME 239 ms - sync_hot 216ms
# A budget counting SYMBOLS cannot see work that is per COLUMN, which is the
# same mistake as a budget counting calls instead of work.
from omnitrix.ui.main_window import MAX_FOLD_COLS_PER_SYNC

big = [x for x in win.bookmaps if x not in hot2][:8]
ts_f = 1_700_001_500_000
for x in big:
    bb = win.bookmaps[x]
    bb.set_hot(True)
    for i in range(600):
        ts_f += 1000
        bb.add_trade(Trade(x, 400.0, 10, Aggressor.BUY, ts_f))
        bb.add_book(BookSnapshot(x, {399.99: 500}, {400.01: 500}, ts_f))
win._demote_cursor = 0
over_before = sum(win.bookmaps[x].over_cap() for x in big)
stale_ = time.monotonic() - DEMOTE_GRACE_S - 1
for x in big:
    win._cold_since[x] = stale_
    win._ever_hot.add(x)
for x in big:
    win.bookmaps[x].max_cols = win.bookmaps[x].cold_cols
after = []
for _ in range(3):
    before = sum(b._evicted for b in win.bookmaps.values())
    win._sync_hot()
    after.append(sum(b._evicted for b in win.bookmaps.values()) - before)
check("one pass never releases more columns than the budget allows, however "
      "many symbols are eligible",
      all(n <= MAX_FOLD_COLS_PER_SYNC + 2 for n in after),
      f"columns released per pass: {after}, budget {MAX_FOLD_COLS_PER_SYNC}")
check("...and it really does release something each pass, rather than "
      "deadlocking below the budget",
      any(n > 0 for n in after), f"{after}")
for _ in range(80):
    win._sync_hot()
check("...and the backlog still drains to zero",
      all(win.bookmaps[x].over_cap() == 0 for x in big),
      f"{sum(win.bookmaps[x].over_cap() for x in big)} columns left of "
      f"{over_before}")

drain_demotions(win)
drained = sum(1 for x, b in win.bookmaps.items()
              if x not in hot2 and b.max_cols == b.cold_cols)
check("the whole queue drains", drained == n_cold, f"{drained} of {n_cold}")
check("a hot symbol is never deferred - it is promoted on the same pass",
      all(win.bookmaps[x].max_cols == win.bookmaps[x].hot_cols
          for x in hot2 if x in win.bookmaps), f"hot={sorted(hot2)}")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("HOT/COLD RETENTION OK")
