"""DATA-TRUTH GATE - the app must never show volume that did not trade.

Every invariant here exists because it was violated in production and the chart
lied without saying so. Read the failure message before "fixing" a gate: these
are not style rules, each one maps to a specific way the terminal reported
false flow.

Run:  py tests/gate_truth.py     (exit 0 = safe to ship)
"""

from __future__ import annotations

import random
import time

from _harness import Gate

from PyQt6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.bars import Bar, BarSeries
from omnitrix.engine.model import (Trade, BookSnapshot, Aggressor, split_size)
from omnitrix.engine.takion_decode import TakionDecoder, L1, L2
from omnitrix.engine.pipe_feed import PipeFeed
from omnitrix.engine.network_feed import NetworkFeed
from omnitrix.render.bookmap import BubbleItem
from omnitrix.ui.tape_window import TapeWindow

TICK = 0.01
B, S, U = Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN
g = Gate("data-truth")


def l1(sym, last, bid, ask, cum, tms=1):
    return L1.pack(sym.encode().ljust(32, b"\0"), 0., 0., 0.,
                   last, bid, ask, cum, tms, 0, 0, 0)


def l2(sym, mmid, price, size, side):
    return L2.pack(sym.encode().ljust(8, b"\0"), mmid.encode().ljust(8, b"\0"),
                   price, size, side)


# =====================================================================
g.section("the split (model.split_size is the ONLY definition)")
# An UNKNOWN print carries no direction. Attributing all of it to one side is
# fabricating data - four call sites used to do exactly that, all leaning the
# same way, which is why the tape read green.
g.check(split_size(100, B, 7) == (100, 0), "BUY -> all buy")
g.check(split_size(100, S, 7) == (0, 100), "SELL -> all sell")
g.check(all(sum(split_size(n, U, t)) == n
            for n in range(1, 400) for t in range(4)),
        "buy + sell == size always (session figures derive from this)")
g.check(max(abs(b - s) for n in range(1, 400) for t in range(4)
            for b, s in [split_size(n, U, t)]) <= 1,
        "an UNKNOWN print never leans more than 1 share")

r = random.Random(11)
ti, net, total = 40000, 0, 0
for _ in range(200_000):
    ti += r.choice((-1, 0, 1))
    size = r.choice((1, 1, 1, 3, 5, 7, 99, 101))       # odd-heavy on purpose
    b, s = split_size(size, U, ti)
    net += b - s
    total += size
g.check(abs(net) < total * 0.001,
        f"no accumulating bias over a price walk: {net:+,} on {total:,} "
        f"unclassified shares ({net / total * 100:+.4f}%)")

# =====================================================================
g.section("every consumer reports the SAME split")
for pct in (0.0, 0.15, 0.5, 1.0):
    inst = Instruments()
    buf = BookmapBuffer("QQQ", inst, col_dt=1.0)
    ser = BarSeries("QQQ", inst)
    rr = random.Random(int(pct * 100) + 3)
    exp_b = exp_s = 0
    t0 = 1_700_000_000_000
    for i in range(4000):
        p = rr.random()
        aggr = U if p < pct else (B if p < pct + (1 - pct) / 2 else S)
        t = 40000 + int(rr.gauss(0, 30))
        size = rr.choice((1, 3, 100, 250, 999))
        tr = Trade("QQQ", t * TICK, size, aggr, t0 + i * 100)
        buf.add_trade(tr)
        ser.add_trade(tr)
        x, y = split_size(size, aggr, t)
        exp_b += x
        exp_s += y
    bm_b = sum(sum(c.buy.values()) for c in buf.columns())
    bm_s = sum(sum(c.sell.values()) for c in buf.columns())
    fp_b = sum(int(bar.arrays()[2].sum()) for bar in ser.bars)
    fp_s = sum(int(bar.arrays()[1].sum()) for bar in ser.bars)
    g.check(bm_b == fp_b == exp_b and bm_s == fp_s == exp_s
            and sum(bar.delta for bar in ser.bars) == exp_b - exp_s
            and ser.sess_delta == exp_b - exp_s,
            f"{int(pct*100):>3}% UNKNOWN: bookmap, footprint, bar delta and "
            f"session delta agree (buy {exp_b:,} / sell {exp_s:,})")

# =====================================================================
g.section("the renderers do not invent buying")
inst = Instruments()
buf = BookmapBuffer("QQQ", inst, col_dt=1.0)
r = random.Random(99)
t0 = 1_700_000_000_000
exp_b = exp_s = 0
for i in range(3000):
    aggr = U if r.random() < 0.4 else (B if r.random() < 0.5 else S)
    t = 40000 + int(r.gauss(0, 20))
    size = r.choice((1, 7, 100, 500))
    buf.add_trade(Trade("QQQ", t * TICK, size, aggr, t0 + i * 100))
    x, y = split_size(size, aggr, t)
    exp_b += x
    exp_s += y


class _VB:
    def viewRange(self):
        return [(-1e12, 1e12), (0, 1e6)]

    def viewPixelSize(self):
        return (0.01, 0.01)


bub = BubbleItem(TICK, buf)
bub.getViewBox = lambda: _VB()
bub.set_cols(buf.columns())          # _cells() filters on _xrange()
cells = bub._cells()
got_b = sum(v[0] for v in cells.values())
got_s = sum(v[1] for v in cells.values())
g.check(got_b == exp_b and got_s == exp_s,
        f"bubble/pie/bars overlay matches the engine exactly "
        f"(buy {got_b:,}, sell {got_s:,})")
share = got_b / max(got_b + got_s, 1) * 100
g.check(45 <= share <= 55,
        f"...and reads balanced on balanced flow: {share:.1f}% buy")
old_b = sum(sz for _x, _t, sz, a in buf.trades if a.value != "sell")
old_s = sum(sz for _x, _t, sz, a in buf.trades if a.value == "sell")
g.note(f"the pre-fix renderer reported "
       f"{old_b / max(old_b + old_s, 1) * 100:.1f}% buy on this same tape")

tw = TapeWindow(buf, TICK)
vis, _lo, _hi, (_n, vol, delta) = tw._window()
g.check(all(0 <= q[3] <= q[2] for q in vis),
        "tape: every print's buy share is within [0, size]")
g.check(delta == 2 * sum(q[3] for q in vis) - vol,
        "tape: window delta is exactly buy - sell")
g.check(sum(2 * q[3] - q[2] for q in vis) == delta,
        "tape: the CVD curve ends at that delta (no drift)")
tw.close()

# =====================================================================
g.section("classification: right where there is evidence, honest where not")
d = TakionDecoder()
g.check(all(d.classify("X", last, bid, ask) is want for last, bid, ask, want in (
            (400.05, 400.00, 400.05, B), (400.07, 400.00, 400.05, B),
            (400.00, 400.00, 400.05, S), (399.98, 400.00, 400.05, S))),
        "quote rule: at/through a side is exact")
d = TakionDecoder()
g.check(d.classify("A", 400.04, 400.00, 400.06) is B
        and TakionDecoder().classify("A", 400.02, 400.00, 400.06) is S,
        "inside the spread: the side of the midpoint")
d = TakionDecoder()
d.classify("A", 400.00, 0.0, 0.0)
g.check(d.classify("A", 400.03, 400.00, 400.06) is B, "at the mid: uptick -> BUY")
g.check(TakionDecoder().classify("NEW", 400.00, 0.0, 0.0) is U,
        "no quote and no history -> UNKNOWN, NOT a fabricated buy")
d = TakionDecoder()
g.check(d.classify("A", 400.03, 400.06, 400.00) is U and d.cls["quote"] == 0,
        "a crossed/garbage quote is not trusted and not counted as evidence")

# accuracy against KNOWN ground truth, on a quote-refreshing feed
def _old_rule(last, bid, ask):
    if last >= ask > 0:
        return B
    if 0 < last <= bid:
        return S
    return U


r = random.Random(4)
d = TakionDecoder()
old_score = new_score = 0.0
new_c = {B: 0, S: 0, U: 0}
N = 20000
mid = 40000
for _ in range(N):
    bid, ask = (mid - 1) * TICK, (mid + 1) * TICK
    took = r.random() < 0.5
    truth = B if took else S
    last = ask if took else bid
    if r.random() < 0.7:
        mid += 1 if took else -1
        bid, ask = (mid - 1) * TICK, (mid + 1) * TICK
    o = _old_rule(last, bid, ask)
    n = d.classify("Q", last, bid, ask)
    new_c[n] += 1
    old_score += 1.0 if o is truth else (0.5 if o is U else 0.0)
    new_score += 1.0 if n is truth else (0.5 if n is U else 0.0)
g.check(new_score / N >= 0.85,
        f"accuracy vs ground truth is {new_score / N:.1%} "
        f"(the single-tier rule scored {old_score / N:.1%})")
g.check(abs(new_c[B] - new_c[S]) / N < 0.06,
        f"no directional skew on balanced flow "
        f"({abs(new_c[B] - new_c[S]) / N:.1%})")

# =====================================================================
g.section("both transports decode identically")
recs1 = [l1("QQQ", 400.05, 400.00, 400.05, 1000),
         l1("QQQ", 400.05, 400.00, 400.05, 1500),
         l1("QQQ", 400.00, 400.00, 400.05, 1900),
         l1("QQQ", 400.03, 400.00, 400.06, 2400)]
now_ms = float(int(time.time() * 1000))
recs2 = [l2("QQQ", "ARCA", 399.99, 500, b"B"),
         l2("QQQ", "NSDQ", 400.06, 700, b"A"),
         l2("QQQ", "", now_ms, 0, b"C")]


def _run(feed):
    # Lock the clock offset, or _on_l1 withholds every trade while it measures
    # and this compares one book instead of the whole record mix.
    feed._ts_offset = 0
    out = []
    feed.on_trade(lambda t: out.append(("T", t.symbol, round(t.price, 4),
                                        t.size, t.aggressor.value)))
    feed.on_book(lambda b: out.append(("B", b.symbol,
                                       tuple(sorted(b.bids.items())),
                                       tuple(sorted(b.asks.items())), b.ts_ms)))
    for rec in recs1:
        feed._on_l1(rec, 0)
    for rec in recs2:
        feed._on_l2(rec, 0)
    return out, dict(feed.cls)


a, ca = _run(PipeFeed())
b_, cb = _run(NetworkFeed("127.0.0.1"))
g.check(a == b_ and len(a) > 0,
        f"pipe and network emit identical events ({len(a)})")
g.check(ca == cb, "and identical classification counters")

# =====================================================================
g.section("the feed boundary cannot corrupt the book")
d = TakionDecoder()
books = []
d.on_book(books.append)
d._on_l2(l2("QQQ", "ARCA", 400.00, 5000, b"B"), 0)
d._on_l2(l2("QQQ", "NSDQ", 399.99, 3000, b"B"), 0)      # will be PULLED
d._on_l2(l2("QQQ", "BATS", 400.05, 4000, b"A"), 0)
d.on_disconnect()                                        # link drops mid-sweep
d._on_l2(l2("QQQ", "ARCA", 400.00, 1000, b"B"), 0)
d._on_l2(l2("QQQ", "BATS", 400.05, 4000, b"A"), 0)
d._on_l2(l2("QQQ", "", now_ms, 0, b"C"), 0)
bk = books[-1]
g.check(bk.bids.get(400.00) == 1000 and 399.99 not in bk.bids
        and bk.asks.get(400.05) == 4000,
        "a sweep interrupted by a disconnect does not contaminate the next "
        "(no doubled sizes, no phantom levels)")

d = TakionDecoder()
d.on_book(lambda _b: None)
for i in range(TakionDecoder.MAX_PARTIAL_LEVELS * 2):
    d._on_l2(l2("ZZZ", "X", 100.0 + i * 0.01, 10, b"B"), 0)
g.check(len(d._bids.get("ZZZ", {})) <= TakionDecoder.MAX_PARTIAL_LEVELS,
        "a sweep that never completes cannot grow without bound")

d = TakionDecoder()
got = []
d.on_book(got.append)
d._on_l2(l2("QQQ", "ARCA", 400.00, 300, b"B"), 0)
d._on_l2(l2("QQQ", "NSDQ", 400.00, 700, b"B"), 0)
d._on_l2(l2("QQQ", "", now_ms, 0, b"C"), 0)
g.check(got[-1].bids.get(400.00) == 1000,
        "two market makers at one price still SUM (that is real depth)")

d = TakionDecoder()
d._ts_offset = 0
t6 = []
d.on_trade(t6.append)
d._on_l1(l1("QQQ", 400.00, 399.99, 400.01, 1000), 0)
for bad in (0.0, -5.0, float("nan")):
    d._on_l1(l1("QQQ", bad, 399.99, 400.01, 1500), 0)
d._on_l1(l1("QQQ", 400.05, 399.99, 400.05, 3000), 0)
g.check(all(t.price > 0 for t in t6) and len(t6) == 1,
        "zero / negative / NaN last price never becomes a trade "
        "(a price-0 print classifies SELL and drags the chart to zero)")

d = TakionDecoder()
d._ts_offset = 0
t7 = []
d.on_trade(t7.append)
d._on_l1(l1("QQQ", 400.00, 399.99, 400.01, 1_000_000), 0)
d._on_l1(l1("QQQ", 400.02, 399.99, 400.01, 1_000_100), 0)
d.on_l1_disconnect()
d._on_l1(l1("QQQ", 400.50, 400.49, 400.51, 5_000_000), 0)   # 3.9 M missed
d._on_l1(l1("QQQ", 400.52, 400.49, 400.51, 5_000_200), 0)
g.check(max(t.size for t in t7) <= 1000,
        "a volume gap across an outage stays a GAP, not a fabricated block")

# =====================================================================
g.section("compact bars are indistinguishable from the dict version")


def _ref(trades):
    cells, delta = {}, 0
    for t, size, aggr in trades:
        c = cells.setdefault(t, [0, 0])
        x, y = split_size(size, aggr, t)
        c[0] += y
        c[1] += x
        delta += x - y
    return cells, delta


def _ref_poc(cells):
    tot = {t: c[0] + c[1] for t, c in cells.items()}
    if not tot:
        return None, tot
    mx = max(tot.values())
    return min(k for k, v in tot.items() if v == mx), tot


def _ref_va(cells, pct):
    poc, tot = _ref_poc(cells)
    if not tot:
        return None, None
    target = sum(tot.values()) * pct
    idxs = sorted(tot)
    lo = hi = idxs.index(poc)
    acc = tot[poc]
    n = len(idxs)
    while acc < target and (lo > 0 or hi < n - 1):
        up = tot[idxs[hi + 1]] if hi < n - 1 else -1
        dn = tot[idxs[lo - 1]] if lo > 0 else -1
        if up < 0 and dn < 0:
            break
        if up >= dn:
            hi += 1
            acc += tot[idxs[hi]]
        else:
            lo -= 1
            acc += tot[idxs[lo]]
    return idxs[hi], idxs[lo]


def _ref_imb(cells, factor, min_vol):
    bi, si = set(), set()
    for t, c in cells.items():
        sv, bv = c
        if bv >= min_vol:
            dn = cells.get(t - 1)
            ds = dn[0] if dn else 0
            if ds == 0 or bv >= factor * ds:
                bi.add(t)
        if sv >= min_vol:
            up = cells.get(t + 1)
            ub = up[1] if up else 0
            if ub == 0 or sv >= factor * ub:
                si.add(t)
    return bi, si


bad = {k: 0 for k in ("cells", "poc", "va", "imb", "fold", "delta")}
for seed in range(300):
    rr = random.Random(seed)
    spread = rr.choice((1, 3, 40))
    trades = [(40000 + rr.randint(-spread, spread),
               rr.choice((1, 2, 3, 17, 100, 999)),
               rr.choice([B, S, U])) for _ in range(rr.randint(1, 120))]
    bar = Bar(1_700_000_000, 60, 400.0)
    for t, size, aggr in trades:
        bar.add(t * TICK, t, size, aggr)
    cells, delta = _ref(trades)
    bar.seal()
    at, asl, abu = bar.arrays()
    got = {int(x): [int(y), int(z)] for x, y, z in
           zip(at.tolist(), asl.tolist(), abu.tolist())}
    if got != cells or list(at) != sorted(at) or bar.cells is not None:
        bad["cells"] += 1
    if bar.delta != delta:
        bad["delta"] += 1
    if bar.poc != _ref_poc(cells)[0]:
        bad["poc"] += 1
    if any(bar.value_area(p) != _ref_va(cells, p) for p in (0.6, 0.7, 0.8)):
        bad["va"] += 1
    if any(bar.imbalances(f, m) != _ref_imb(cells, f, m)
           for f, m in ((3.0, 20), (2.0, 0), (5.0, 100))):
        bad["imb"] += 1
    for step in (2, 5, 25, 100):
        ag = bar.aggregated(step)
        gt, gs, gb = ag.arrays()
        want = {}
        for t, c in cells.items():
            o = want.setdefault(t // step, [0, 0])
            o[0] += c[0]
            o[1] += c[1]
        if {int(x): [int(y), int(z)] for x, y, z in
                zip(gt.tolist(), gs.tolist(), gb.tolist())} != want:
            bad["fold"] += 1
for k, v in bad.items():
    g.check(v == 0, f"{k}: exact over 300 random bars ({v} mismatches)")

# POC must not depend on the order trades arrived
r = random.Random(1234)
base = [(40000 + r.randint(-3, 3), r.choice((100, 100, 250)),
         r.choice([B, S])) for _ in range(60)]
pocs, vas = set(), set()
for perm in range(40):
    q = list(base)
    random.Random(perm).shuffle(q)
    bb = Bar(1, 60, 400.0)
    for t, size, aggr in q:
        bb.add(t * TICK, t, size, aggr)
    bb.seal()
    pocs.add(bb.poc)
    vas.add(bb.value_area(0.7))
g.check(len(pocs) == 1 and len(vas) == 1,
        f"POC and value area are order-independent across 40 shuffles "
        f"(poc={pocs})")

# a late tick into an already-sealed (compacted) bar
inst = Instruments()
ser = BarSeries("Q", inst)
t0 = 1_700_000_000_000
for i in range(400):
    ser.add_trade(Trade("Q", 400.00 + (i % 7) * TICK, 100, B, t0 + i * 1000))
before = sum(x.volume for x in ser.bars)
err = None
try:
    for k in range(50):
        ser.add_trade(Trade("Q", 400.03, 250, S, t0 + (k * 5) * 1000))
except Exception as e:                                   # noqa: BLE001
    err = f"{type(e).__name__}: {e}"
g.check(err is None and sum(x.volume for x in ser.bars) == before + 50 * 250,
        f"a late tick into a sealed bar is recorded, not a crash ({err})")

raise SystemExit(g.finish())
