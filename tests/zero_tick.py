"""The zero-tick rule: does it reclaim attribution WITHOUT inventing it?

A live post-market feed showed UNKNOWN climbing 3% -> 19% across a session.
Every one of those prints was being split 50/50 by split_size, so a fifth of
the delta, CVD and imbalance figures on screen rested on a coin toss that
looked like data.

The cause was not the feed. UNKNOWN requires BOTH no usable direction from the
quote AND a price equal to the previous print - and both get commoner as a book
thins, which is exactly what happens after the close.

Lee-Ready's answer is the zero-tick rule: a print at an unchanged price
inherits the direction of the last price CHANGE. That is what this checks, and
the thing it must NOT do is manufacture a direction where none was ever
observed.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from omnitrix.engine.takion_decode import TakionDecoder
from omnitrix.engine.model import Aggressor, split_size

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# ---- the rule itself -------------------------------------------------------
d = TakionDecoder()
# Establish an uptick, then repeat the price with no usable quote.
d.classify("QQQ", 100.00, 0.0, 0.0)          # seeds the price
d.classify("QQQ", 100.02, 0.0, 0.0)          # uptick -> BUY
r = d.classify("QQQ", 100.02, 0.0, 0.0)      # same price
check("a zero tick after an UPTICK is a buy", r is Aggressor.BUY, r.name)
check("...and is counted as ztick, not as a quote",
      d.cls["ztick"] == 1 and d.cls["quote"] == 0, str(d.cls))

d2 = TakionDecoder()
d2.classify("QQQ", 100.00, 0.0, 0.0)
d2.classify("QQQ", 99.98, 0.0, 0.0)           # downtick -> SELL
r = d2.classify("QQQ", 99.98, 0.0, 0.0)
check("a zero tick after a DOWNTICK is a sell", r is Aggressor.SELL, r.name)

# ---- it must not invent a direction ---------------------------------------
d3 = TakionDecoder()
r = d3.classify("NEW", 50.00, 0.0, 0.0)       # first ever print, no quote
check("the FIRST print with no quote is still UNKNOWN",
      r is Aggressor.UNKNOWN,
      "nothing has been observed yet - inventing a side here would be fabrication")
r = d3.classify("NEW", 50.00, 0.0, 0.0)       # repeat, still no direction
check("a repeat with no prior MOVE is still UNKNOWN", r is Aggressor.UNKNOWN)
check("and it is counted as unknown", d3.cls["unknown"] == 2, str(d3.cls))

# ---- direct evidence must still win ---------------------------------------
d4 = TakionDecoder()
d4.classify("QQQ", 100.00, 0.0, 0.0)
d4.classify("QQQ", 100.05, 0.0, 0.0)          # uptick: last direction is UP
r = d4.classify("QQQ", 100.05, 100.04, 100.10)   # ...but it prints AT the bid
check("a quote overrides the inherited direction",
      r is Aggressor.SELL,
      "at the bid is direct evidence of a seller, whatever the last tick did")

# ---- per symbol, not global ------------------------------------------------
d5 = TakionDecoder()
d5.classify("AAA", 10.00, 0.0, 0.0)
d5.classify("AAA", 10.02, 0.0, 0.0)           # AAA ticked up
r = d5.classify("BBB", 20.00, 0.0, 0.0)
check("one symbol's direction never leaks into another",
      r is Aggressor.UNKNOWN, f"BBB got {r.name} from AAA's uptick")

# ---- what it is worth on a realistic thin tape -----------------------------
import random

rng = random.Random(11)


def run_tape(use_quotes_pct: float, repeat_pct: float, n=20000):
    dec = TakionDecoder()
    px = 723.70
    for _ in range(n):
        if rng.random() > repeat_pct:
            px = round(px + rng.choice([-0.01, 0.01]), 2)
        if rng.random() < use_quotes_pct:
            # a 2-cent spread puts the mid on a whole cent, so prints land on
            # it often - which is precisely the case that used to go UNKNOWN
            bid, ask = round(px - 0.01, 2), round(px + 0.01, 2)
        else:
            bid = ask = 0.0
        dec.classify("QQQ", px, bid, ask)
    tot = sum(dec.cls.values())
    return {k: v / tot for k, v in dec.cls.items()}


print()
print("  thin post-market tape (60% of prints repeat a price, 50% quoteless):")
m = run_tape(0.50, 0.60)
attributed = 1.0 - m["unknown"]
print(f"    quote {m['quote']:.0%}  mid {m['mid']:.0%}  tick {m['tick']:.0%}  "
      f"ztick {m['ztick']:.0%}  UNKNOWN {m['unknown']:.0%}")
check("the unknown share is now small on a thin tape", m["unknown"] < 0.05,
      f"{m['unknown']:.1%} still split 50/50")
check("almost everything is attributed", attributed > 0.95,
      f"{attributed:.1%}")
check("the zero-tick tier is visible, not folded into the strong ones",
      m["ztick"] > 0.0, "it must be auditable separately")

# ---- and the split is unchanged for everything already classified ---------
check("split_size still halves a genuine UNKNOWN",
      split_size(100, Aggressor.UNKNOWN, 0) == (50, 50))
check("...and never halves an attributed print",
      split_size(100, Aggressor.BUY, 0) == (100, 0)
      and split_size(100, Aggressor.SELL, 0) == (0, 100))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("ZERO-TICK OK")
