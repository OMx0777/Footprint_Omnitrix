"""PERFORMANCE GATE - the app must not get slower the longer it runs.

ONE INVARIANT, and every lag report in this project has been a violation of it:

    a repeating timer must do work proportional to what is VISIBLE,
    not to total session history.

`BarSeries.view`, `BookmapBuffer.view`, `signals.detect_all` and the stats dock
all broke it, and together burnt 75% of a core after eight hours - `detect_all`
alone froze the GUI thread for a quarter second every 0.9 s. Nothing in the code
prevents the next panel from doing the same, which is what this gate is for.

A consumer whose cost rises with session length fails here even if it is still
"fast enough" today, because it will not be tomorrow.

Run:  py tests/gate_perf.py     (exit 0 = safe to ship)
"""

from __future__ import annotations

import gc
import random
import time
import tracemalloc

from _harness import Gate

from PyQt6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])

import numpy as np
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtCore import QRectF

from omnitrix.engine import Instruments, BookmapBuffer, SessionProfile
from omnitrix.engine.bars import BarSeries
from omnitrix.engine.model import Trade, BookSnapshot, Aggressor
from omnitrix.engine import metrics, signals, levels
from omnitrix.render.footprint import FootprintItem

TICK = 0.01
# Short and long, not 1..8 h: the ratio is what matters and the gate has to be
# quick enough that people actually run it.
SHORT_H, LONG_H = 0.5, 3.0
# A consumer above this is growing with uptime. 1.0 would be ideal; the slack
# absorbs timer noise and ring-fill effects, and every real offender measured
# 4x or worse - there is no ambiguous middle.
MAX_GROWTH = 1.6
# Below this a measurement is timer noise, not a cost. Ratios there are
# meaningless - three attribute reads "grew 2.2x" from 0.0001 to 0.0002 ms -
# and a consumer this cheap cannot cause lag whatever its ratio does.
NOISE_FLOOR_MS = 0.05

g = Gate("performance")


def build(hours: float):
    inst = Instruments()
    buf = BookmapBuffer("QQQ", inst, col_dt=1.0, max_cols=14400)
    ser = BarSeries("QQQ", inst)
    prof = SessionProfile("QQQ", inst)
    r = random.Random(7)
    t0 = int(time.time() * 1000) - int(hours * 3600) * 1000
    for i in range(int(hours * 3600)):
        ts = t0 + i * 1000
        mid = 40000 + int(r.gauss(0, 80))
        bk = BookSnapshot("QQQ",
                          {(mid - k) * TICK: r.randint(100, 20000)
                           for k in range(1, 129)},
                          {(mid + k) * TICK: r.randint(100, 20000)
                           for k in range(1, 129)}, ts)
        buf.add_book(bk)
        ser.add_book(bk)
        for _ in range(6):
            t = mid + int(r.gauss(0, 8))
            tr = Trade("QQQ", t * TICK, r.randint(1, 900),
                       r.choice([Aggressor.BUY, Aggressor.SELL,
                                 Aggressor.UNKNOWN]), ts)
            buf.add_trade(tr)
            ser.add_trade(tr)
            prof.add_trade(tr)
    return buf, ser, prof


def timed(fn, reps=3):
    fn()
    t = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - t) * 1000 / reps


def consumers(buf, ser, prof):
    """name -> (callable, timer period in seconds from the owning widget)."""
    sr = levels.SRTracker()
    cols400 = buf.view(1)[-400:]
    return {
        "bookmap.view(1m)": (lambda: buf.view(60), 0.080),
        "bookmap.view(1s)": (lambda: buf.view(1), 0.080),
        "signals.detect_all": (lambda: signals.detect_all(buf, agg=1), 0.900),
        "SRTracker.update": (lambda: sr.update(buf.view(1)[-180:], 40000.0),
                             0.080),
        "metrics 400 cols": (lambda: metrics.imbalance_from(
            *metrics.depth_sides(cols400)), 0.250),
        "profile.analytics": (lambda: (
            setattr(prof, "_version", prof._version + 1),
            prof.analytics())[1], 0.400),
        "stats tape figures": (lambda: (buf.trade_count, buf.trade_vol,
                                        buf.trade_max), 0.400),
        "BarSeries.view(1m)": (lambda: (ser.add_trade(
            Trade("QQQ", 400.0, 1, Aggressor.BUY, int(time.time() * 1000))),
            ser.view(60))[1], 0.033),
    }


g.section(f"growth: cost at {LONG_H:g} h vs {SHORT_H:g} h (max {MAX_GROWTH}x)")
rows = {}
for hours in (SHORT_H, LONG_H):
    buf, ser, prof = build(hours)
    for name, (fn, _period) in consumers(buf, ser, prof).items():
        rows.setdefault(name, {})[hours] = timed(fn)
    periods = {n: p for n, (_f, p) in consumers(buf, ser, prof).items()}

total_cpu = 0.0
for name, by_h in sorted(rows.items()):
    short, long = by_h[SHORT_H], by_h[LONG_H]
    ratio = long / max(short, 1e-9)
    cpu = long / (periods[name] * 1000) * 100
    total_cpu += cpu
    if max(short, long) < NOISE_FLOOR_MS:
        g.check(True, f"{name:<20} {long:7.3f} ms - below the noise floor, "
                      f"ratio not meaningful")
        continue
    g.check(ratio <= MAX_GROWTH,
            f"{name:<20} {short:7.2f} -> {long:7.2f} ms  ({ratio:4.1f}x, "
            f"{cpu:4.1f}% of a core)")

g.check(total_cpu < 25.0,
        f"all periodic consumers together use {total_cpu:.1f}% of one core "
        f"at {LONG_H:g} h")

# =====================================================================
g.section("frame budgets at worst-case settings (33 ms frame)")


# The deployment target is a fleet of identical boxes: i7-9700, 16 GB,
# UHD 630, ONE 1920x1080 monitor. Every budget below is measured at that
# resolution - a gate calibrated to some other screen measures the wrong
# machine. Paint here is bound by per-primitive CALL COUNT, not fill rate
# (1600x900 measured 56 ms against 60 ms at 1920x1080), so the numbers move
# little with resolution, but the reference should still be the real one.
SCREEN_W, SCREEN_H = 1920, 1080


class _VB:
    def __init__(self, xr, yr, w, h):
        self._r, self._w, self._h = [xr, yr], w, h

    def viewRange(self):
        return self._r

    def viewPixelSize(self):
        return ((self._r[0][1] - self._r[0][0]) / self._w,
                (self._r[1][1] - self._r[1][0]) / self._h)


def paint_ms(item, vb, reps=5, W=SCREEN_W, H=SCREEN_H):
    def once():
        img = QImage(W, H, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        (x0, x1), (y0, y1) = vb.viewRange()
        p.scale(W / (x1 - x0), H / (y1 - y0))
        p.translate(-x0, -y0)
        item.getViewBox = lambda: vb
        item.paint(p)
        p.end()
    once()
    t = time.perf_counter()
    for _ in range(reps):
        once()
    return (time.perf_counter() - t) * 1000 / reps


inst = Instruments()
ser = BarSeries("QQQ", inst)
r = random.Random(3)
t0 = 1_700_000_000_000
for i in range(200 * 3600):                     # 200 one-hour bars' worth
    if i % 3600 == 0:
        pass
    t = 40000 + int(r.gauss(0, 120))
    if i % 12 == 0:
        ser.add_trade(Trade("QQQ", t * TICK, r.randint(1, 900),
                            r.choice([Aggressor.BUY, Aggressor.SELL]),
                            t0 + i * 1000))
# The LIVE path at a REALISTIC trade density.
#
# Budget note, stated rather than buried in a constant: the measured baseline is
# ~50 ms for 150 bars, and it is dominated by per-bar Qt primitives (candle
# wick, body, value-area lines), NOT by footprint cells - going from 5 to 147
# levels per bar only moves it ~44 -> ~67 ms. So this threshold is a REGRESSION
# guard at today's baseline, not a claim that 50 ms is good. The real fix is the
# one the liquidity heatmap already uses: composite the cells into a single
# image and blit once, which took that renderer from 170 ms to 4 ms. Until that
# is done, this number should not be allowed to creep.
def bars_at_density(trades_per_min: int, spread: int, minutes: int = 200):
    inst = Instruments()
    s = BarSeries("Q", inst)
    rr = random.Random(3)
    t0 = 1_700_000_000_000
    for m in range(minutes):
        for k in range(trades_per_min):
            t = 40000 + int(rr.gauss(0, spread))
            s.add_trade(Trade("Q", t * TICK, rr.randint(1, 900),
                              rr.choice([Aggressor.BUY, Aggressor.SELL]),
                              t0 + (m * 60 + k % 60) * 1000))
    return s.view(60)[-150:]


live = bars_at_density(60, 30)
vb_live = _VB((0, len(live)), (399.0, 401.0), SCREEN_W, SCREEN_H)
fp = FootprintItem(TICK)
fp.bars = live
fp.price_step = 0.0                              # Auto - the shipped default
ms_live = paint_ms(fp, vb_live)
lv = sum(b.n_levels() for b in live) / len(live)
g.check(ms_live <= 70.0,
        f"live view, {len(live)} one-minute bars at {lv:.0f} levels/bar: "
        f"{ms_live:.1f} ms  (baseline guard - see the note in this file)")

# Cost must not blow up with cell count. If someone reintroduces per-cell object
# construction this ratio is where it shows, long before the absolute budget
# does.
dense = bars_at_density(1000, 30)
fp_d = FootprintItem(TICK)
fp_d.bars = dense
fp_d.price_step = 0.0
ms_dense = paint_ms(fp_d, _VB((0, len(dense)), (399.0, 401.0),
                              SCREEN_W, SCREEN_H))
lv_d = sum(b.n_levels() for b in dense) / len(dense)
g.check(ms_dense <= ms_live * 1.8,
        f"20x the cells ({lv:.0f} -> {lv_d:.0f} levels/bar) costs only "
        f"{ms_dense / max(ms_live, 1e-9):.2f}x ({ms_dense:.1f} ms) - "
        f"per-bar bound, not per-cell")



# The EXTREME: the whole retained history on the heaviest timeframe. Deliberately
# a looser budget, and the reason is worth stating rather than hiding in a
# constant - this is a user who has zoomed all the way out, not the live frame
# path, and its cost is bounded by `max_bars` so it does NOT grow with uptime.
# Degrading to ~20 fps there is acceptable; silently getting slower is not.
bars = ser.view(3600)
vb_all = _VB((0, len(bars)), (398.0, 402.0), SCREEN_W, SCREEN_H)
fp2 = FootprintItem(TICK)
fp2.bars = bars
fp2.price_step = 0.0
ms_all = paint_ms(fp2, vb_all)
g.check(ms_all <= 75.0,
        f"zoomed fully out, {len(bars)} hourly bars on Auto: {ms_all:.1f} ms "
        f"(looser budget: not the live path, and capped by max_bars)")
# 75 ms, not 55: the earlier figure came from 1600x900 measurements. At the
# deployment resolution the same view costs 58 ms. Raised to match the target
# machine, not to make the gate pass - the live-path budget above is unchanged.
fp2.price_step = 0.01                            # the crowded worst case
ms1 = paint_ms(fp2, vb_all)
g.note(f"the same view forced to a 1c grid costs {ms1:.1f} ms - "
       f"Auto exists precisely to avoid that")

# =====================================================================
g.section("memory does not grow without bound")
tracemalloc.start()
inst = Instruments()
buf = BookmapBuffer("Q", inst, col_dt=1.0, max_cols=1400)
ser = BarSeries("Q", inst)
r = random.Random(9)
t0 = int(time.time() * 1000) - 4 * 3600 * 1000
marks = []
for h in range(4):
    for sec in range(3600):
        ts = t0 + (h * 3600 + sec) * 1000
        mid = 40000 + int(r.gauss(0, 60))
        if r.random() < 1.74:
            bk = BookSnapshot("Q",
                              {(mid - k) * TICK: r.randint(100, 9000)
                               for k in range(1, 129)},
                              {(mid + k) * TICK: r.randint(100, 9000)
                               for k in range(1, 129)}, ts)
            buf.add_book(bk)
            ser.add_book(bk)
        for _ in range(5):
            t = mid + int(r.gauss(0, 6))
            # ONE trade to both consumers, as the app does - feeding two
            # different Trade objects would measure a load that never happens.
            tr = Trade("Q", t * TICK, r.randint(1, 600),
                       r.choice([Aggressor.BUY, Aggressor.SELL,
                                 Aggressor.UNKNOWN]), ts)
            ser.add_trade(tr)
            buf.add_trade(tr)
    gc.collect()
    marks.append(tracemalloc.get_traced_memory()[0] / 1e6)
tracemalloc.stop()
per_h = (marks[-1] - marks[1]) / (len(marks) - 2)
# A REGRESSION guard, not an endorsement. The measured baseline on this load is
# ~3 MB/h/symbol after compacting sealed bars (down from ~4.4). Roughly 65% of
# what remains is `Bar.book`: every historical bar pins a 256-level ladder for
# the chart's heatmap overlay, which only ever draws the visible window.
# Bounding that is a deliberate open decision - it costs historical liquidity on
# scroll-back - so the threshold guards against getting WORSE than today rather
# than pretending the architecture is finished.
g.check(per_h < 6.0,
        f"one symbol grows {per_h:+.2f} MB/h once the rings fill "
        f"({marks[1]:.0f} -> {marks[-1]:.0f} MB)")
g.check(len(buf.order) == buf.max_cols and len(buf.cols) == len(buf.order),
        f"the column ring is at its cap with no orphans "
        f"({len(buf.order):,})")
g.note(f"projected 100 symbols x 16 h: "
       f"{(marks[-1] + per_h * 12) * 100:,.0f} MB - dominated by Bar.book; "
       f"see the note above")

raise SystemExit(g.finish())
