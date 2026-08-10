"""A symbol selected AFTER startup must still get its session.

THE REPORT: "still no historical data ... it dont show all historical footprint
and profile charts", with a server log proving the replay had served megabytes
per symbol. The data arrived and the client threw it away.

Two causes, both structural:

  * the startup backfill only ever covered `_hot_symbols()` - the one or two
    symbols on screen when the app opened;

  * and it REFUSED to install into a slot that had already counted live bars.
    On a multicast feed carrying the whole basket that is every symbol within
    seconds of launch, so every symbol selected later was fetched and
    discarded, and its chart began when the application did.

Refusing to REPLACE was right: the live series holds volume and delta the
replay knows nothing about. Merging is a different operation, and the seam is
what makes it safe - only bars strictly OLDER than the oldest live bar are
taken, so the two never describe the same bucket.

The failure this guards is DOUBLE COUNTING, which would be silent: the chart
would look full and every figure on it would be wrong.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import Instruments, BarSeries
from omnitrix.engine.model import Trade, Aggressor

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)
T0 = 1_700_000_000_000


def build(name, first_bucket, n_bars, px0=100.0, per_bar=6):
    """`n_bars` bars starting at `first_bucket` base buckets past T0."""
    s = BarSeries(name, inst)
    step = s.base_tf_s * 1000
    tot = 0
    for b in range(n_bars):
        for k in range(per_bar):
            px = round(px0 + ((b + k) % 17) * 0.01, 2)
            ag = Aggressor.BUY if k % 2 else Aggressor.SELL
            s.add_trade(Trade(name, px, 100, ag,
                              T0 + (first_bucket + b) * step + k * 100))
            tot += 100
    return s, tot


# live = the last 20 bars; replay = the whole 120 bars ending at the same place
live, live_vol = build("X", 100, 20)
replay, replay_vol = build("X", 0, 120)

live_first = live.bars[0].start_ts
live_bars_before = len(live.bars)
live_delta_before = live.sess_delta
live_vol_before = live.sess_volume

rep = live.prepend_history(replay)
check("the merge reports what it added", rep.get("added", 0) > 0, f"{rep}")
check("...and the series really grew",
      len(live.bars) == live_bars_before + rep["added"],
      f"{live_bars_before} -> {len(live.bars)}")

# ---- THE ONE THAT MATTERS: no bucket appears twice -------------------------
ts = [b.start_ts for b in live.bars]
check("no bar bucket appears twice - a duplicate would double count volume "
      "and delta with nothing to show for it",
      len(ts) == len(set(ts)), f"{len(ts) - len(set(ts))} duplicates")
check("...and the series is still ascending in time",
      all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)))
check("the seam is exact - every merged bar is strictly older than the oldest "
      "live bar",
      all(b.start_ts < live_first for b in live.bars[:rep["added"]]),
      f"seam at {live_first}")
check("...and the live bars are untouched, same objects in the same order",
      [b.start_ts for b in live.bars[rep["added"]:]]
      == [b.start_ts for b in build("X", 100, 20)[0].bars])

# ---- session figures gain the prepended flow, and only that ---------------
added_vol = sum(b.volume for b in live.bars[:rep["added"]])
check("session volume grew by exactly the merged bars' volume",
      live.sess_volume == live_vol_before + added_vol,
      f"{live_vol_before:,} + {added_vol:,} = {live.sess_volume:,}")
check("...and session delta likewise",
      live.sess_delta == live_delta_before
      + sum(b.delta for b in live.bars[:rep["added"]]))
check("the session OPEN is now the oldest bar's open, not whatever printed "
      "when the client attached",
      live.sess_open == live.bars[0].open, f"{live.sess_open}")

# ---- a bar the live stream is still filling is NOT merged -----------------
live2, _ = build("Y", 100, 20)
replay2, _ = build("Y", 0, 121)          # replay ALSO covers live's first bar
seam = live2.bars[0].start_ts
r2 = live2.prepend_history(replay2)
merged_ts = {b.start_ts for b in live2.bars[:r2["added"]]}
check("the boundary bar is left to the live stream - the replay holds only "
      "the part that arrived before the client connected, and adding it would "
      "produce a bar that is neither",
      seam not in merged_ts, f"seam {seam} in merged set")

# ---- idempotence: merging twice must not add anything -------------------
before = len(live2.bars)
vol_before = live2.sess_volume
r3 = live2.prepend_history(replay2)
check("merging the same replay again adds nothing - a retry or a duplicate "
      "signal must not silently double the session",
      r3.get("added", 0) == 0 and len(live2.bars) == before
      and live2.sess_volume == vol_before, f"{r3}")

# ---- edges ----------------------------------------------------------------
empty = BarSeries("Z", inst)
r4 = empty.prepend_history(replay)
check("merging into an EMPTY series takes the lot", r4["added"] == len(replay.bars),
      f"{r4}")
check("...and merging an empty replay is a no-op, not a crash",
      BarSeries("W", inst).prepend_history(BarSeries("W", inst))["added"] == 0)

# the cap is honoured from the FRONT, where the oldest are
small, _ = build("C", 200, 5)
small.max_bars = 30
big, _ = build("C", 0, 200)
r5 = small.prepend_history(big)
check("the bar cap is honoured, and it drops the OLDEST",
      len(small.bars) <= 30 and small.bars[-1].start_ts
      == max(b.start_ts for b in small.bars),
      f"{len(small.bars)} bars, cap 30")

# aggregation must not serve a stale fold after a merge
live3, _ = build("V", 100, 20)
_ = live3.view(60)                      # populate the cache
rep3, _ = build("V", 0, 120)
live3.prepend_history(rep3)
agg = live3.view(60)
check("the aggregation cache is invalidated - a stale fold would draw the "
      "chart as it was before the history arrived",
      agg and agg[0].start_ts <= live3.bars[0].start_ts + 60,
      f"{len(agg)} bars at 1m, first {agg[0].start_ts if agg else None}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("SESSION MERGE OK")
