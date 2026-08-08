"""Delta divergence must fire on the setup and stay quiet otherwise.

The trade: price grinds to a higher high while the aggressive buying that
should be driving it is smaller than it was at the previous high. Fewer buyers
are making the move, so the next seller of size has less to absorb.

An indicator that fires often is not an indicator. Two ways this one could be
useless and neither raises an error:

  * firing on noise - one quiet bar inside an advance is a quiet bar, not a
    divergence, so the comparison is between SWING points a trader would also
    have marked;

  * firing on a healthy trend - if price and delta both make higher highs,
    nothing is diverging and a signal there is worse than no signal.

So the test builds both shapes deliberately and checks it can tell them apart.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import Instruments, BarSeries
from omnitrix.engine.model import Trade, Aggressor
from omnitrix.engine.signals import detect_delta_divergence

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)


def build(legs):
    """legs = [(peak_price, buy_share)] - one advance-and-pull-back per leg."""
    s = BarSeries("DV", inst)
    t = 1_700_000_000_000
    step = s.base_tf_s * 1000
    b = 0
    base = 100.0
    for peak, share in legs:
        up = [base + (peak - base) * (k + 1) / 6 for k in range(6)]
        down = [peak - (peak - base) * (k + 1) / 8 for k in range(4)]
        for px in up + down:
            for k in range(20):
                aggr = Aggressor.BUY if (k / 20.0) < share else Aggressor.SELL
                s.add_trade(Trade("DV", round(px, 2), 100, aggr,
                                  t + b * step + k * 100))
            b += 1
        base = down[-1]
    return s


# ---- 1. THE SETUP: higher high in price, weaker delta ----------------------
# Second leg reaches a higher price on notably less aggressive buying.
# The share is CALCULATED, not guessed. Each bar carries 2,000 shares, so a
# buy share p gives a bar delta of (2p-1)*2000. Leg one (6 up + 4 back, all at
# 0.80) leaves cumulative delta at +12,000 with its swing high at +7,200; for
# the second swing high to sit BELOW that, leg two's six advancing bars must
# shed more than 4,800 - so p must be under 0.30. Two earlier guesses (0.52,
# 0.40) both left the running total still climbing, which is a slower advance,
# not a divergence.
s = build([(101.0, 0.80), (102.0, 0.20)])
div = detect_delta_divergence(s.bars, lookback=len(s.bars))
bear = [d for d in div if d["kind"] == "bear_div"]
check("a higher high on weaker delta IS reported", bool(bear),
      f"{len(bear)} bear divergences of {len(div)} total")
if bear:
    d = bear[-1]
    check("...and it compares two swings, not two bars",
          d["i"] - d["prev_i"] >= 3, f"bars {d['prev_i']} -> {d['i']}")
    check("...with price higher and cumulative delta lower",
          s.bars[d["i"]].high > s.bars[d["prev_i"]].high
          and d["delta"] < d["prev_delta"],
          f"price {s.bars[d['prev_i']].high:.2f}->{s.bars[d['i']].high:.2f}, "
          f"delta {d['prev_delta']:,}->{d['delta']:,}")

# ---- 2. THE HEALTHY TREND: it must stay quiet ------------------------------
# Higher high AND stronger delta - nothing is diverging.
s2 = build([(101.0, 0.55), (102.0, 0.90)])   # both legs bought - healthy
div2 = detect_delta_divergence(s2.bars, lookback=len(s2.bars))
bear2 = [d for d in div2 if d["kind"] == "bear_div"]
check("a higher high on STRONGER delta is NOT reported - a signal there is "
      "worse than no signal", not bear2, f"{len(bear2)} false positives")

# ---- 3. the bullish mirror -------------------------------------------------
s3 = build([(99.0, 0.20), (98.0, 0.80)])   # lower low, net BUYING into it
div3 = detect_delta_divergence(s3.bars, lookback=len(s3.bars))
bull = [d for d in div3 if d["kind"] == "bull_div"]
check("a lower low on improving delta is reported as bullish", bool(bull),
      f"{len(bull)} bull divergences")

# ---- 4. it must not fire on noise ------------------------------------------
import random
rng = random.Random(7)
s4 = BarSeries("DV", inst)
t = 1_700_000_000_000
for b in range(120):
    px = 100.0 + rng.gauss(0, 0.05)
    for k in range(20):
        s4.add_trade(Trade("DV", round(px, 2), 100,
                           Aggressor.BUY if rng.random() < 0.5 else Aggressor.SELL,
                           t + b * s4.base_tf_s * 1000 + k * 100))
noise = detect_delta_divergence(s4.bars, lookback=len(s4.bars))
check("flat noise does not produce a wall of signals", len(noise) <= 6,
      f"{len(noise)} on 120 random bars")

# ---- 5. bounds -------------------------------------------------------------
check("too few bars returns nothing rather than raising",
      detect_delta_divergence(s.bars[:4]) == [])
check("an empty series returns nothing", detect_delta_divergence([]) == [])
big = detect_delta_divergence(s.bars, lookback=len(s.bars), top_n=3)
check("the result is capped by top_n", len(big) <= 3, f"{len(big)}")
check("...and is ordered oldest first",
      all(big[i]["i"] <= big[i + 1]["i"] for i in range(len(big) - 1)))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("DELTA DIVERGENCE OK")
