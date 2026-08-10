"""The volume profile must agree with the footprint, and must get the history.

Two defects, found together while wiring backfilled history into the profile.

  1. THE SPLIT. SessionProfile split an UNKNOWN print as size//2 to buy and the
     remainder to sell - a FIXED side for the odd share. That is exactly the
     bias model.split_size exists to remove: a 1-lot unclassified print counted
     as a whole SELL in the profile and as an alternating share everywhere
     else, so the profile's delta drifted from the footprint's over the same
     session with nothing on screen to explain the difference.

  2. THE HISTORY. The profile was fed only from the live drain loop, so a
     symbol whose session had been backfilled showed a chart going back hours
     next to a profile that began when the application did.

The check that matters is agreement: the profile and the bars are two views of
one session, and a user reads them side by side. If they disagree, at least one
is lying and neither says which.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import Instruments, BarSeries
from omnitrix.engine.profile import SessionProfile
from omnitrix.engine.model import Trade, Aggressor, split_size

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)
T0 = 1_700_000_000_000

# ---- 1. THE SPLIT AGREES WITH THE ONE SHARED DEFINITION -------------------
prof = SessionProfile("SP", inst)
ser = BarSeries("SP", inst)
odd = []
for i in range(600):
    # ODD sizes and UNKNOWN aggressors on purpose - that is the only case where
    # a local split can differ, and it is the case that was wrong.
    size = 1 + (i % 7) * 2
    ag = (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3]
    px = round(100.0 + (i % 23) * 0.01, 2)
    tr = Trade("SP", px, size, ag, T0 + i * 200)
    prof.add_trade(tr)
    ser.add_trade(tr)
    if ag is Aggressor.UNKNOWN:
        odd.append(tr)

p_buy = sum(prof.buy.values())
p_sell = sum(prof.sell.values())
b_buy = b_sell = 0
for bar in ser.bars:
    _ti, sv, bv = bar.arrays()
    b_buy += int(bv.sum())
    b_sell += int(sv.sum())

check("the fixture really exercises the case - odd sizes, UNKNOWN aggressor",
      len(odd) > 100, f"{len(odd)} unknown prints")
check("profile buy volume equals the footprint's, exactly",
      p_buy == b_buy, f"profile {p_buy:,} vs bars {b_buy:,}")
check("profile sell volume equals the footprint's, exactly",
      p_sell == b_sell, f"profile {p_sell:,} vs bars {b_sell:,}")
check("...so their DELTAS agree - two views of one session that a trader "
      "reads side by side",
      (p_buy - p_sell) == (b_buy - b_sell),
      f"{p_buy - p_sell:+,} vs {b_buy - b_sell:+,}")
check("total volume matches the series", prof.total == ser.sess_volume,
      f"{prof.total:,} vs {ser.sess_volume:,}")

# and per PRICE, not just in aggregate
bars_by_ti = {}
for bar in ser.bars:
    ti_a, sv, bv = bar.arrays()
    for t, s_, b_ in zip(ti_a.tolist(), sv.tolist(), bv.tolist()):
        cur = bars_by_ti.setdefault(t, [0, 0])
        cur[0] += s_
        cur[1] += b_
mismatch = [t for t, (s_, b_) in bars_by_ti.items()
            if prof.sell.get(t, 0) != s_ or prof.buy.get(t, 0) != b_]
check("...and they agree at EVERY price level, not merely in total",
      not mismatch, f"{len(mismatch)} levels differ of {len(bars_by_ti)}")

# ---- 2. HISTORY ARRIVING AS BARS ------------------------------------------
live_prof = SessionProfile("H", inst)
live_ser = BarSeries("H", inst)
step = live_ser.base_tf_s * 1000
for i in range(120):                      # the live part
    tr = Trade("H", round(50.0 + (i % 11) * 0.01, 2), 100 + i % 50,
               (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3],
               T0 + 400 * step + i * 300)
    live_ser.add_trade(tr)
    live_prof.add_trade(tr)

hist = BarSeries("H", inst)
for i in range(1200):                     # the replayed session in front of it
    hist.add_trade(Trade("H", round(50.0 + (i % 29) * 0.01, 2), 100 + i % 70,
                         (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)[i % 3],
                         T0 + i * 300))

before_total = live_prof.total
before_brackets = len(live_prof.brackets)
rep = live_ser.prepend_history(hist)
added = live_prof.add_bars(live_ser.bars[:rep["added"]], live_ser.base_tf_s)

check("the merge added bars to work with", rep["added"] > 0, f"{rep}")
check("the profile gained volume from the history",
      added > 0 and live_prof.total == before_total + added,
      f"+{added:,} -> {live_prof.total:,}")
check("...and gained TPO brackets, so the profile spans the session rather "
      "than one bracket",
      len(live_prof.brackets) > before_brackets,
      f"{before_brackets} -> {len(live_prof.brackets)} brackets")

# the merged profile must STILL agree with the merged series
mb = ms = 0
for bar in live_ser.bars:
    _t, sv, bv = bar.arrays()
    mb += int(bv.sum())
    ms += int(sv.sum())
pb = sum(live_prof.buy.values())
ps = sum(live_prof.sell.values())
check("after the merge the profile still equals the footprint, buy and sell",
      (pb, ps) == (mb, ms), f"profile ({pb:,},{ps:,}) vs bars ({mb:,},{ms:,})")

# ---- 3. it must not double count ------------------------------------------
snap = (live_prof.total, sum(live_prof.buy.values()))
again = live_prof.add_bars([], live_ser.base_tf_s)
check("adding no bars changes nothing", again == 0
      and (live_prof.total, sum(live_prof.buy.values())) == snap)

# a bar with no footprint (stripped by cold retention) contributes nothing
stripped = BarSeries("K", inst)
_st = stripped.base_tf_s * 1000
# ONE TRADE PER BAR. 300 ms apart put all forty in a single 10-second bucket,
# so set_hot stripped nothing and the check asserted against an empty list -
# a vacuous pass, which is worse than no check.
for i in range(80):
    stripped.add_trade(Trade("K", 10.0, 100, Aggressor.BUY, T0 + i * _st))
stripped.set_hot(False)
blank = [b for b in stripped.bars if b.n_levels() == 0]
pk = SessionProfile("K", inst)
got = pk.add_bars(blank, stripped.base_tf_s)
check("the fixture really produced stripped bars", len(blank) > 5,
      f"{len(blank)} of {len(stripped.bars)} bars lost their footprint")
check("bars whose footprint was stripped add nothing - the profile must not "
      "invent volume for detail that is gone",
      got == 0 and pk.total == 0, f"{got} added from {len(blank)} blank bars")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("PROFILE HISTORY OK")
