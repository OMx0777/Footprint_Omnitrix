"""Where the memory ACTUALLY goes, measured rather than assumed.

Written to settle a proposal to compress Bar.book with delta-encoding + zlib.
The compression works exactly as claimed - 81% off - but Bar.book turned out to
be 3.2% of the footprint, so the whole exercise would have saved 2.6%.

    Bar.book ladders          4.87 MB    3.2%   <- the proposed target
    Column.book ladders      24.96 MB   16.5%
    Bar footprint cells       3.07 MB    2.0%
    Column buy/sell dicts    17.79 MB   11.7%
    tape deques              96.32 MB   63.6%   <- the actual target
    Bar objects               1.10 MB
    Column objects            3.36 MB

The tape is a deque of (float, int, int, Aggressor) TUPLES: 80 bytes for the
tuple plus boxed members is 261 bytes to carry 17 bytes of data. The same
60,000 entries as four parallel numpy arrays is 1.02 MB against 15.65 MB.

Keep this runnable. Every future "let us optimise X" should start by seeing
whether X is on this list.
"""

import sys, time, gc
import numpy as np
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from omnitrix.engine import Instruments, SyntheticFeed, BarSeries, BookmapBuffer
import psutil
proc = psutil.Process()
inst = Instruments(default_tick=0.01)

SYMS = [f"S{i:02d}" for i in range(20)]
ser = {s: BarSeries(s, inst) for s in SYMS}
bufs = {s: BookmapBuffer(s, inst) for s in SYMS}
feed = SyntheticFeed(symbols=SYMS, start_price=400.0, tick=0.01,
                     trades_per_sec=900, prefill_minutes=45, seed=7)
feed.on_trade(lambda t: (ser[t.symbol].add_trade(t), bufs[t.symbol].add_trade(t)))
feed.on_book(lambda b: (ser[b.symbol].add_book(b), bufs[b.symbol].add_book(b)))
gc.collect(); r0 = proc.memory_info().rss
feed.start(); time.sleep(20); feed.stop(); gc.collect()
r1 = proc.memory_info().rss

def ladder_bytes(objs):
    seen = {}
    for o in objs:
        bk = getattr(o, "book", None)
        if bk is None: continue
        seen[id(bk)] = bk
    tot = 0
    for bk in seen.values():
        try:
            ti, sz = bk.arrays(); tot += ti.nbytes + sz.nbytes + 220   # 2 array hdrs
        except Exception: pass
    return tot, len(seen)

allbars = [b for s in SYMS for b in ser[s].bars]
allcols = [c for s in SYMS for c in bufs[s].cols.values()]
bar_book, n_bar_lad = ladder_bytes(allbars)
col_book, n_col_lad = ladder_bytes(allcols)

# footprint cells on bars
fp = 0
for b in allbars:
    try:
        ti, se, bu = b.arrays(); fp += ti.nbytes + se.nbytes + bu.nbytes + 330
    except Exception:
        fp += sys.getsizeof(getattr(b, "cells", {}) or {})
# column trade dicts
coldicts = sum(sys.getsizeof(c.buy) + sys.getsizeof(c.sell) for c in allcols)
# The tape is four ring arrays now, so measure them - the old estimate
# (getsizeof(deque) + 72 B/entry) describes a structure that no longer exists.
tapes = sum(b.trade_x.nbytes + b.trade_ti.nbytes + b.trade_sz.nbytes
            + b.trade_ag.nbytes for b in bufs.values())
bar_obj = len(allbars) * 200
col_obj = len(allcols) * 120

print(f"{len(SYMS)} symbols, 45 min prefill + 20 s live")
print(f"  RSS grew            {(r1-r0)/1e6:8.1f} MB")
print(f"  bars                {len(allbars):,}   columns {len(allcols):,}")
print()
for name, val in (("Bar.book ladders", bar_book), ("Column.book ladders", col_book),
                  ("Bar footprint cells", fp), ("Column buy/sell dicts", coldicts),
                  ("tape ring arrays", tapes), ("Bar objects", bar_obj),
                  ("Column objects", col_obj)):
    print(f"    {name:24s} {val/1e6:8.2f} MB")
shared = n_bar_lad + n_col_lad
print(f"\n  distinct ladders: {n_bar_lad:,} on bars, {n_col_lad:,} on columns")
tot = bar_book + col_book + fp + coldicts + tapes + bar_obj + col_obj
print(f"    accounted        {tot/1e6:8.2f} MB of {(r1-r0)/1e6:.1f} MB RSS growth")
for name, val in (("Bar.book", bar_book), ("tape", tapes)):
    print(f"    {name:16s} share {val/max(tot,1)*100:5.1f}%")
