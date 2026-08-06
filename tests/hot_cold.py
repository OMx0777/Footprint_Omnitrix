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

# the book window applies even while HOT - the heatmap only draws what is visible
hot_bs, _ = build_bars(60, 1_700_000_500_000)
check("a hot series keeps its recent books",
      sum(1 for b in hot_bs.bars if b.book is not None and len(b.book)) > 0,
      f"{sum(1 for b in hot_bs.bars if b.book is not None and len(b.book))} bars with a book")

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

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("HOT/COLD RETENTION OK")
