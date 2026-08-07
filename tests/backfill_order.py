"""Bulk-replaying a session must build the SAME series live arrival built.

Startup backfill routes replayed trades through add_trade() on purpose - that
is what makes sess_volume, sess_delta and the volume profile come out right,
derived from the same trades and the same split_size rather than from a second
implementation that would eventually disagree.

Which puts the whole weight on add_trade's ordering rules, and one of them
loses data on purpose:

    late = self._bar_by_ts.get(bucket)
    ...
    return              # too late to place: not charted, not counted

A trade whose bar has already been evicted is DROPPED, silently, with no
counter. Live that is right - it is one stale tick out of a session. In a bulk
replay of six hours it could be thousands, and the result would be a chart
whose session volume is quietly short with nothing to say so.

So the oracle here is not "did it load" but "is it identical to what live
produced", and the exposure of the silent drop is measured rather than assumed.
"""

import os
import random
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from omnitrix.engine import Instruments, BarSeries
from omnitrix.engine.model import Trade, Aggressor

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)
AGGR = (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)


def session(n_bars=200, per_bar=40, seed=11, jitter_ms=0):
    """Trades as a live feed would deliver them, optionally with real jitter."""
    rng = random.Random(seed)
    base_tf = 10
    out = []
    t0 = 1_700_000_000_000
    for b in range(n_bars):
        for k in range(per_bar):
            ts = t0 + b * base_tf * 1000 + k * (base_tf * 1000 // per_bar)
            if jitter_ms:
                ts += rng.randint(-jitter_ms, jitter_ms)
            out.append(Trade("BF", round(220.0 + rng.randint(-40, 40) * 0.01, 2),
                             rng.randint(1, 900), AGGR[k % 3], ts))
    return out


def load(trades, **kw):
    s = BarSeries("BF", inst, **kw)
    for t in trades:
        s.add_trade(t)
    return s


def fingerprint(s):
    """Everything a user could read off the chart."""
    return {
        "sess": (s.sess_volume, s.sess_trades, s.sess_high, s.sess_low,
                 s.sess_last, getattr(s, "sess_delta", None)),
        "bars": [(b.start_ts, b.open, b.high, b.low, b.close, b.volume, b.delta,
                  b.n_levels(), b.poc) for b in s.bars],
    }


# ---- 1. an ordered bulk load is indistinguishable from live arrival --------
tr = session()
live = load(tr)
bulk = load(list(tr))          # same order, one pass - the backfill case
check("an ordered bulk load reproduces live arrival exactly",
      fingerprint(live) == fingerprint(bulk),
      f"{len(live.bars)} bars, {live.sess_volume:,} volume")

# and the profile a user would see
vp_live = {}
for b in live.bars:
    t_, sell, buy = b.arrays()
    for i, ti in enumerate(t_.tolist()):
        vp_live[ti] = vp_live.get(ti, 0) + int(sell[i]) + int(buy[i])
vp_bulk = {}
for b in bulk.bars:
    t_, sell, buy = b.arrays()
    for i, ti in enumerate(t_.tolist()):
        vp_bulk[ti] = vp_bulk.get(ti, 0) + int(sell[i]) + int(buy[i])
check("...including the volume profile, price level by price level",
      vp_live == vp_bulk, f"{len(vp_bulk)} levels")
check("...and the profile sums to the session volume",
      sum(vp_bulk.values()) == bulk.sess_volume,
      f"{sum(vp_bulk.values()):,} vs {bulk.sess_volume:,}")

# ---- 2. real feeds interleave: jitter must not lose volume -----------------
trj = session(jitter_ms=4000, seed=7)        # ±4 s across 10 s bars
jit = load(trj)
total = sum(t.size for t in trj)
check("out-of-order arrivals inside the window are still counted",
      jit.sess_volume == total,
      f"{jit.sess_volume:,} of {total:,} "
      f"({(total - jit.sess_volume) / total * 100:.3f}% lost)")

# ---- 3. THE EXPOSURE: when does the silent drop actually bite? ------------
# A trade is dropped only when its bar is GONE, so the question is how far
# late a record has to be. Jitter of a few seconds never reaches past the bar
# cap - the first attempt at this test looked reassuring while proving
# nothing, because a late trade referenced a bar 1-2 back and 40 were kept.
#
# Forced properly here: a trade older than the retained window at all.
ex = load(session(n_bars=60, per_bar=20, seed=3), max_bars=20)
before = (ex.sess_volume, ex.sess_trades)
ancient = Trade("BF", 220.0, 999_999, Aggressor.BUY,
                ex.bars[0].start_ts * 1000 - 10 * 60 * 1000)
ex.add_trade(ancient)
check("a trade older than the retained bars IS dropped, and not counted",
      (ex.sess_volume, ex.sess_trades) == before,
      f"{before[0]:,} unchanged - the 999,999 was discarded silently")

# and it must not be silent: a bulk loader has to be able to check
check("...and the drop is RECORDED, so a bulk load can verify itself",
      ex.dropped_late == 1 and ex.dropped_late_vol == 999_999,
      f"dropped_late={ex.dropped_late}, vol={ex.dropped_late_vol:,}")

# THE REALISTIC CASE. At the shipped 12,000-bar cap (33 h) a same-day replay
# cannot reach past the window at all, so the drop is not an exposure for
# startup backfill - it is an exposure for a cap set below the session.
# THE ONE THAT MATTERS FOR BACKFILL. Feeding a day's replay in arrival order
# loses volume even with nothing evicted: a late trade whose BUCKET WAS NEVER
# CREATED finds no bar and is discarded. Not eviction - a hole in the sequence
# of buckets, which jitter produces routinely.
day = session(n_bars=2340, per_bar=10, jitter_ms=8000, seed=9)   # 6.5 h at 10 s
want_day = sum(t.size for t in day)
arrival = load(day)                                 # default max_bars=12000
lost = want_day - arrival.sess_volume
check("replaying in ARRIVAL order silently loses volume - this is the reason "
      "a bulk payload must not be fed in raw",
      lost > 0 and arrival.dropped_late_vol == lost,
      f"{lost:,} of {want_day:,} ({lost / want_day * 100:.3f}%), "
      f"and dropped_late_vol reports exactly {arrival.dropped_late_vol:,}")

# THE REMEDY. A bulk payload is held whole in memory, so unlike a live stream
# it can be sorted first - and then no trade is ever late and nothing is lost.
srt = load(sorted(day, key=lambda t: t.ts_ms))
check("replaying SORTED BY TIMESTAMP loses nothing at all",
      srt.sess_volume == want_day and srt.dropped_late == 0,
      f"{srt.sess_volume:,} of {want_day:,}, {srt.dropped_late} dropped")
check("...and the sorted load's profile still sums to its session volume",
      sum(int(b.arrays()[1].sum()) + int(b.arrays()[2].sum())
          for b in srt.bars) == srt.sess_volume,
      f"{srt.sess_volume:,}")

# ---- 4. the seam: replay then live must not double count ------------------
first_half = tr[:len(tr) // 2]
second_half = tr[len(tr) // 2:]
seam = load(first_half)
for t in second_half:
    seam.add_trade(t)
check("replay then live meets exactly - no gap, no overlap",
      fingerprint(seam) == fingerprint(live),
      f"{seam.sess_volume:,} vs {live.sess_volume:,}")

# a trade delivered TWICE across the seam must show up as the double it is,
# so the seam has to be exact rather than approximately right
dup = load(first_half)
for t in second_half:
    dup.add_trade(t)
for t in second_half[:50]:                   # 50 records of overlap
    dup.add_trade(t)
check("an overlapping seam DOES double count - which is why the boundary is "
      "a sequence, not a timestamp",
      dup.sess_volume > live.sess_volume,
      f"{dup.sess_volume:,} vs {live.sess_volume:,} "
      f"(+{dup.sess_volume - live.sess_volume:,})")

# ---- 5. bars stay monotonic however jumbled the input ---------------------
for s_, name in ((live, "ordered"), (jit, "jittered"), (srt, "sorted bulk")):
    ts = [b.start_ts for b in s_.bars]
    check(f"{name}: bar timestamps stay strictly increasing",
          all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)),
          f"{len(ts)} bars")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("BACKFILL ORDERING OK")
