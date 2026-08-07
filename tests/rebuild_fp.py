"""Replaying history into stripped bars must not count anything twice.

A cold symbol loses its per-price footprint behind COLD_BARS while keeping
OHLC, volume and delta. Selecting it should be able to fetch the trades back
and refill the cells - and the way that goes wrong is silent.

`add_trade()` maintains the bar's volume and delta AND feeds _stat_trade, and
all of those already counted these trades when they arrived live. Replaying
through the normal path therefore doubles the session volume, the session
delta and the bar's own volume, permanently, with nothing raised and no way
for a user to tell: the chart simply says the day traded twice what it did.

So the test does not check that the footprint came back. It checks that every
figure which was already correct is STILL correct afterwards, byte for byte.
"""

import os
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


def build(n_bars=140, seed=5):
    """One bar per iteration, explicitly - the base timeframe is 10 s, so
    letting trade spacing decide meant only 32 bars for 80 iterations and
    barely any fell behind the cold window."""
    rng = np.random.default_rng(seed)
    s = BarSeries("RB", inst)
    step = s.base_tf_s * 1000
    t = 1_700_000_000_000
    hist = []
    for b in range(n_bars):
        base = t + b * step
        for k in range(int(rng.integers(5, 60))):
            tr = Trade("RB", round(220.0 + rng.integers(-40, 40) * 0.01, 2),
                       int(rng.integers(1, 800)), AGGR[k % 3],
                       base + k * 100)
            s.add_trade(tr)
            hist.append(tr)
    return s, hist


s, hist = build()
# the figures that must survive untouched
before = {
    "sess_volume": s.sess_volume, "sess_trades": s.sess_trades,
    "sess_high": s.sess_high, "sess_low": s.sess_low, "sess_last": s.sess_last,
    "sess_delta": getattr(s, "sess_delta", None),
    "bars": [(b.start_ts, b.open, b.high, b.low, b.close, b.volume, b.delta)
             for b in s.bars],
}
poc_before = {b.start_ts: b.poc for b in s.bars}

s.set_hot(False)                                   # strip the footprint
stripped = [b for b in s.bars if b.n_levels() == 0]
check("stripping actually removed cells from the old bars", len(stripped) > 5,
      f"{len(stripped)} of {len(s.bars)} bars stripped")

rep = s.rebuild_footprint(hist)
print(f"  rebuild: {rep}")

# ---- THE POINT ------------------------------------------------------------
check("session volume is unchanged - the replay did NOT double-count",
      s.sess_volume == before["sess_volume"],
      f"{before['sess_volume']:,} -> {s.sess_volume:,}")
check("session trade count is unchanged",
      s.sess_trades == before["sess_trades"],
      f"{before['sess_trades']:,} -> {s.sess_trades:,}")
check("session high/low/last are unchanged",
      (s.sess_high, s.sess_low, s.sess_last)
      == (before["sess_high"], before["sess_low"], before["sess_last"]))
check("session delta is unchanged",
      getattr(s, "sess_delta", None) == before["sess_delta"],
      f"{before['sess_delta']} -> {getattr(s, 'sess_delta', None)}")
after_bars = [(b.start_ts, b.open, b.high, b.low, b.close, b.volume, b.delta)
              for b in s.bars]
diff = [i for i, (a, b) in enumerate(zip(before["bars"], after_bars)) if a != b]
check("every bar's OHLC, volume and delta are unchanged", not diff,
      f"{len(diff)} bars differ, e.g. {diff[:2]}")

# ---- and the footprint really did come back --------------------------------
refilled = [b for b in s.bars if b.n_levels() > 0]
check("the footprint is back on the refilled bars", rep["bars"] > 0
      and len(refilled) > len(s.bars) - len(stripped),
      f"{rep['bars']} bars refilled, {len(refilled)} now carry cells")
check("a refilled bar's cells sum to the volume it always had",
      rep["mismatched"] == 0, f"{rep['mismatched']} bars disagree")
poc_after = {b.start_ts: b.poc for b in s.bars}
same_poc = [k for k in poc_before
            if poc_before[k] is not None and poc_after.get(k) == poc_before[k]]
check("the rebuilt POC matches what the bar reported before stripping",
      len(same_poc) >= rep["bars"],
      f"{len(same_poc)} bars agree of {rep['bars']} refilled")

# ---- replaying TWICE must still not double-count ---------------------------
rep2 = s.rebuild_footprint(hist)
check("a second replay is a no-op, not a doubling",
      s.sess_volume == before["sess_volume"]
      and [(b.start_ts, b.volume, b.delta) for b in s.bars]
      == [(x[0], x[5], x[6]) for x in before["bars"]],
      f"second pass: {rep2}")

# ---- a trade for a bar that no longer exists must be refused ---------------
ghost = Trade("RB", 220.0, 999, Aggressor.BUY, 1_600_000_000_000)
n_before = len(s.bars)
rep3 = s.rebuild_footprint([ghost])
check("a trade whose bar was evicted is skipped, not invented",
      len(s.bars) == n_before and rep3["applied"] == 0,
      f"{rep3}")
check("...and it did not touch the session either",
      s.sess_volume == before["sess_volume"])

# ---- the LIVE bar must not be written behind add_trade's back --------------
live = s.bars[-1]
live_before = (live.volume, live.delta, live.n_levels())
s.rebuild_footprint([Trade("RB", float(live.close), 500, Aggressor.BUY,
                           live.start_ts * 1000 + 10)])
check("the live bar is left alone - it owns its own cells",
      (live.volume, live.delta, live.n_levels()) == live_before,
      f"{live_before} -> {(live.volume, live.delta, live.n_levels())}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("FOOTPRINT REBUILD OK")
