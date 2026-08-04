# Gates

Two executable invariants. Exit code is the contract: **0 = safe to ship**.

```
py tests/run_gates.py          # both
py tests/run_gates.py truth    # data only, ~1 s - run this on every commit
```

No pytest, no new dependencies: the gates run anywhere the app runs.

---

## `gate_truth.py` — the app must never show volume that did not trade

Every check here exists because it was violated in production and **the chart
lied without saying so**. If one fails, read the message before "fixing" it —
each maps to a specific way the terminal reported false flow.

The invariant: **no consumer decides the buy/sell split for itself.**
`model.split_size()` is the only definition. Four places used to decide
independently and three were wrong, all leaning the same way:

| what was wrong | what it did |
|---|---|
| bubbles/pies/bars counted UNKNOWN as 100% buy | reported **69.1% buy** on a tape that was **48.6% buy** |
| the tape reader did the same | CVD read **+21,323** when the truth was **−6,581** — wrong sign |
| footprint gave the odd share to buy, bookmap to sell | the two panes disagreed about the same trade |
| classifier had only the quote rule | ~70% of prints were UNKNOWN, then drawn green |

Also covered: the feed boundary (a disconnect must not contaminate the next
book, an unpriceable record must not become a price-0 trade, a volume gap must
not become a fabricated block), and that compacted bars are indistinguishable
from the dict version they replaced — including that POC is **order
independent**, which it was not before.

## `gate_perf.py` — the app must not get slower the longer it runs

One invariant, and every lag report in this project has been a violation of it:

> **A repeating timer must do work proportional to what is VISIBLE,
> not to total session history.**

`BarSeries.view`, `BookmapBuffer.view`, `signals.detect_all` and the stats dock
all broke it and together burnt **75% of a core** after eight hours —
`detect_all` alone froze the GUI thread for a quarter second every 0.9 s.

A consumer whose cost rises with session length fails here **even if it is still
fast enough today**, because it will not be tomorrow. Measurements below
`NOISE_FLOOR_MS` are exempt: a ratio on timer noise means nothing.

### Two thresholds are regression guards, not endorsements

Both are set at today's measured baseline and are labelled as such in the file.
They stop things getting worse; they do not claim the architecture is finished:

- **Footprint paint, ~50 ms at 150 bars.** Dominated by per-bar Qt primitives,
  not cells — 5 → 147 levels per bar only moves it ~44 → ~67 ms. The real fix is
  what the liquidity heatmap already does: composite into one image and blit
  once, which took that renderer from 170 ms to 4 ms.
- **~4.6 MB/h per symbol.** Roughly 65% is `Bar.book`: every historical bar pins
  a 256-level ladder for the chart's heatmap overlay, which only ever draws the
  visible window. Bounding it is an open decision — it costs historical
  liquidity on scroll-back.

## Adding a check

Put it in the gate it belongs to and make the message say **what breaks for the
user**, not what the assertion compares. "reads balanced on balanced flow" beats
"assertEqual(a, b)": the next person to see it red needs to know whether they
broke the product or the test.
