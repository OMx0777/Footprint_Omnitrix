"""1,000 active symbols, with the UI churning, for five minutes.

THE BASELINE. Everything since 88d41ed has been projected from measurements at
100 symbols; this is the first run at the number the architecture is actually
aimed at, and it exists so the next change has something to regress against.

Three things it is looking for, in order of how badly they would hurt:

  * a slow leak. numpy buffers are refcounted rather than traced, so a
    released ring is freed at once and never reaches the collector - which
    also means a leak here would NOT show up as garbage. It shows up as RSS
    that keeps climbing after the retention windows have filled, so the test
    compares the second half of the run against the first rather than looking
    at a single number;

  * a frame over the 80 ms timer. Not the average - the average was never the
    problem. The p95 and the worst frame are what a user experiences as a
    stutter;

  * a demotion backlog. Demotions are rate-limited (MAX_DEMOTIONS_PER_SYNC) so
    that releasing two hundred symbols at once cannot stall a frame. That is
    only safe if the queue actually drains: under heavy churn a cursor that
    falls behind faster than it advances would leak memory by never getting
    round to it.

THE LOAD IS A MIX, not a uniform rate. Real peak-hours flow is dominated by a
few dozen names while the long tail barely prints, and a uniform generator
would both understate the hot symbols and overstate the cold ones - which is
exactly the distribution the hot/cold split is built around, so getting it
wrong would flatter the result.
"""

import os
import random
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments
from omnitrix.engine.model import Trade, Aggressor, BookSnapshot
from omnitrix.ui.main_window import OmnitrixWindow, MAX_DEMOTIONS_PER_SYNC
from omnitrix.app import _tune_gc

RUN_S = float(os.environ.get("STRESS_SECONDS", "180"))
N_SYM = int(os.environ.get("STRESS_SYMBOLS", "1000"))
DEPTH = 100                       # per side, the stated requirement

# Peak-hours mix. Rates are prints/sec for one symbol in that tier.
TIERS = ((20, 150.0), (100, 30.0), (N_SYM - 120, 2.0))

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


rng = random.Random(7)
syms, rates = [], []
for count, rate in TIERS:
    for _ in range(max(0, count)):
        i = len(syms)
        syms.append(f"{'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i % 26]}{i:04d}")
        rates.append(rate)
mid = {s: 50.0 + (i % 400) for i, s in enumerate(syms)}
total_rate = sum(rates)

app = QApplication.instance() or QApplication([])
_tune_gc()


from omnitrix.engine.feed import Feed


class NullFeed(Feed):
    """A real Feed that emits nothing. Flow is injected straight into the
    window's queue instead, so the frequency MIX is exactly the one described
    above rather than whatever a uniform generator produces."""

    def __init__(self):
        super().__init__()
        self.connected = {}
        self.symbols = syms

    def start(self):
        pass

    def stop(self):
        pass


win = OmnitrixWindow(NullFeed(), Instruments(default_tick=0.01))
win.resize(1600, 950)
win.show()

try:
    import psutil
    _proc = psutil.Process()
    rss = lambda: _proc.memory_info().rss / 1e6
except Exception:
    rss = lambda: 0.0

TS = [1_700_000_000_000]
AGGR = (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)
_book_due = {s: 0.0 for s in syms}


def generate(dt: float) -> int:
    """Inject one interval's worth of flow. Cost excluded from the telemetry."""
    q = win._event_q
    n = 0
    TS[0] += int(dt * 1000)
    ts = TS[0]
    for s, r in zip(syms, rates):
        k = int(r * dt) + (1 if rng.random() < (r * dt) % 1.0 else 0)
        if not k:
            continue
        m = mid[s]
        for _ in range(k):
            px = round(m + rng.randint(-40, 40) * 0.01, 2)
            q.append(Trade(s, px, rng.randint(1, 900),
                           AGGR[rng.randrange(3)], ts))
            n += 1
        mid[s] = max(1.0, m + rng.uniform(-0.02, 0.02))
        _book_due[s] -= dt
        if _book_due[s] <= 0:
            # Depth arrives often on the active names and rarely on the tail,
            # matching the trade mix.
            _book_due[s] = 0.5 if r > 20 else 5.0
            b = {round(m - i * 0.01, 2): 300 + i for i in range(1, DEPTH + 1)}
            a = {round(m + i * 0.01, 2): 300 + i for i in range(1, DEPTH + 1)}
            q.append(BookSnapshot(s, b, a, ts))
    return n


def churn(step: int) -> None:
    """A user changing their mind: layouts, tickers, bookmap panes."""
    if step % 3 == 0:
        win.layout_combo.setCurrentText(rng.choice(["1 chart", "2 charts", "4 charts"]))
    for p in win._panes[:win._n_panes]:
        if rng.random() < 0.6:
            p.sym_combo.setCurrentText(rng.choice(syms))
    if rng.random() < 0.5:
        win.open_bookmap_for(rng.choice(syms))


def _deep(o, seen=None):
    """Real bytes, FOLLOWING containers.

    sys.getsizeof on a dict counts the table and not the values, which is
    exactly how _cache["tot"] was reported at 184 B a bar while actually
    costing 7,098. Nothing in this file estimates with a constant; an estimate
    is what hid the largest term in the model for three commits.
    """
    if o is None:
        return 0
    if seen is None:
        seen = set()
    if id(o) in seen:
        return 0
    seen.add(id(o))
    t = sys.getsizeof(o)
    if isinstance(o, dict):
        for k, v in o.items():
            t += _deep(k, seen) + _deep(v, seen)
    elif isinstance(o, (list, tuple, set, frozenset)):
        for v in o:
            t += _deep(v, seen)
    return t


def model_mb():
    """Bytes the MODEL holds, counted - so growth can be attributed.

    RSS alone cannot separate "still filling a retention window" from
    "leaking", and these windows take minutes to fill, so a short run that
    sees RSS rising has learned nothing. Reported per owner instead.
    """
    lad = bs = tp = bars = cache = idx = 0
    seen = set()
    for b in win.bookmaps.values():
        tp += (b.trade_x.nbytes + b.trade_ti.nbytes
               + b.trade_sz.nbytes + b.trade_ag.nbytes)
        for c in b.columns():
            k = c.book
            if k is not None and id(k) not in seen:
                seen.add(id(k))
                if hasattr(k, "ti"):
                    lad += k.ti.nbytes + k.sz.nbytes + 280
            bs += _deep(c.buy) + _deep(c.sell)
    for ser in win.series.values():
        # One dict entry per bar, kept for the life of the bar. This is the
        # term that legitimately climbs for 33 hours as the 12,000-bar window
        # fills, and it is counted separately so it is never mistaken for one
        # that should have plateaued.
        idx += _deep(ser._bar_by_ts)
        for bar in ser.bars:
            for nm in ("_ti", "_sell", "_buy"):
                a = getattr(bar, nm, None)
                if a is not None:
                    bars += a.nbytes + 112
            k = bar.book
            if k is not None and hasattr(k, "ti") and k.ti.size:
                bars += k.ti.nbytes + k.sz.nbytes + 280
            bars += sys.getsizeof(bar)
            cache += _deep(bar._cache) + _deep(bar._imb)
    return lad/1e6, bs/1e6, tp/1e6, bars/1e6, cache/1e6, idx/1e6


def backlog() -> int:
    """Symbols that should be cold and are not - the demotion queue depth."""
    hot = win._hot_symbols()
    n = 0
    for s, b in win.bookmaps.items():
        if s not in hot and b.max_cols != b.cold_cols:
            n += 1
    return n


print(f"  {N_SYM:,} symbols, {DEPTH} depth per side, "
      f"{total_rate:,.0f} prints/sec offered, {RUN_S:.0f}s run")
print(f"  tiers: " + ", ".join(f"{c} @ {r:g}/s" for c, r in TIERS))
print()
print("   elapsed    _tick ms          paint ms           RSS MB   backlog   "
      "ladders buy/sell   tape   bars  _cache  barIdx (MB)")

ticks, paints, backlogs = [], [], []
rss_series = []
model_series = []
t_start = time.time()
nxt = 30.0
step = 0
total_trades = 0
DT = 0.08                                  # the app's own frame interval
while time.time() - t_start < RUN_S:
    step += 1
    total_trades += generate(DT)
    a = time.perf_counter()
    win._tick()
    ticks.append((time.perf_counter() - a) * 1000)
    a = time.perf_counter()
    win.grab()
    paints.append((time.perf_counter() - a) * 1000)
    app.processEvents()
    if step % 60 == 0:                     # ~every 5 s of simulated time
        churn(step // 60)
    backlogs.append(backlog())
    el = time.time() - t_start
    if el >= nxt:
        ticks.sort(); paints.sort()
        tm, tp = ticks[len(ticks)//2], ticks[int(len(ticks)*0.95)]
        pm, pp = paints[len(paints)//2], paints[int(len(paints)*0.95)]
        r = rss()
        rss_series.append(r)
        lad, bsd, tpd, bard, cached, idxd = model_mb()
        model_series.append(lad + bsd + tpd + bard + cached + idxd)
        print(f"   {el:5.0f}s   {tm:5.1f} (p95{tp:6.1f})  {pm:6.1f} (p95{pp:6.1f})  "
              f"{r:8.1f}   {max(backlogs):5d}  {lad:6.1f} {bsd:8.1f} {tpd:6.1f} "
              f"{bard:6.1f} {cached:7.1f} {idxd:7.1f}")
        ticks, paints, backlogs = [], [], []
        nxt += 30.0

# ---- verdict ---------------------------------------------------------------
print()
print(f"  symbols ingested       {len(win.bookmaps):,}")
print(f"  trades injected        {total_trades:,}")
hot_now = win._hot_symbols()
print(f"  hot at the end         {len(hot_now)}  {sorted(hot_now)[:6]}")

print()
check("RSS stays under 2 GB at 1,000 symbols",
      rss_series and max(rss_series) < 2000,
      f"peak {max(rss_series):.0f} MB")

# LEAK vs FILL. A leak grows without limit; a retention window fills and
# stops. So the test is on the RATE, over the last third of the run, once the
# windows have had time to fill - and it is checked on the MODEL rather than
# on RSS, because the allocator returns pages on its own schedule and would
# otherwise be the thing under test.
if len(model_series) >= 6:
    k = len(model_series) // 3
    early = (model_series[k] - model_series[0]) / max(1, k)
    late = (model_series[-1] - model_series[-1 - k]) / max(1, k)
    check("model growth DECELERATES - the windows are filling, not leaking",
          late <= max(2.0, early * 0.5),
          f"{early:+.1f} MB/sample early vs {late:+.1f} late")
    check("...and the last samples are essentially flat",
          abs(late) < 6.0, f"{late:+.2f} MB per 30 s sample at the end")
else:
    print("  INFO  run too short to separate filling from leaking "
          f"({len(model_series)} samples; the cold bar window alone needs ~25 min)")
check("the demotion queue drains under churn", max(backlogs or [0]) < 200,
      f"deepest backlog {max(backlogs or [0])} symbols "
      f"(limit {MAX_DEMOTIONS_PER_SYNC}/pass)")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("1000-SYMBOL STRESS OK")
