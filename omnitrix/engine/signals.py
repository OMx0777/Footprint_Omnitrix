"""Order-flow signal detection from tick-by-tick prints and the L2 book.

  block       — a single print far larger than the running norm (institutional
                size hitting the tape).
  absorption  — heavy volume traded in a column while the BBO barely moved:
                resting limit orders soaked up the aggression. Marks levels
                that held.
  wall_break  — a large resting wall that was there, then vanished while price
                traded through it: liquidity consumed, level failed.

Each detector returns a list of dicts with a common shape so the UI can render
them uniformly:  {kind, bucket, ti, size, side, note}
"""

from __future__ import annotations

from itertools import islice
from statistics import median

import numpy as np


def detect_blocks(trades, min_size: int = 0, top_n: int = 40) -> list[dict]:
    """Largest aggressive prints. If min_size is 0 an adaptive threshold of
    6x the median print is used."""
    if not trades:
        return []
    sizes = [t[2] for t in trades]
    thr = min_size or max(1, int(median(sizes) * 6))
    out = []
    for x, ti, size, aggr in trades:
        if size >= thr:
            out.append({"kind": "block", "bucket": x, "ti": ti, "size": size,
                        "side": aggr.value, "note": f"block {size:,}"})
    out.sort(key=lambda d: -d["size"])
    return out[:top_n]


def _pct(sorted_vals: list, q: float) -> float:
    """Simple percentile (q in 0..1) over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    i = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, i))]


def detect_absorption(cols, vol_q: float = 0.85, move_q: float = 0.30,
                      top_n: int = 40) -> list[dict]:
    """Columns with outsized volume but unusually small BBO movement —
    aggression absorbed by resting liquidity.

    Thresholds are *percentiles of the instrument's own recent behaviour*
    (top-15% volume, bottom-30% movement) rather than multiples of a median.
    A multiple can be unreachable when the volume distribution is tight;
    a percentile always selects the most absorbing columns on any data.
    """
    if len(cols) < 12:
        return []
    mids = [(c, (c.bid_ti + c.ask_ti) / 2) for c in cols
            if c.bid_ti is not None and c.ask_ti is not None]
    if len(mids) < 12:
        return []

    moves = [abs(mids[i][1] - mids[i - 1][1]) for i in range(1, len(mids))]
    vols = [c.vol for c, _ in mids[1:] if c.vol > 0]
    if not moves or not vols:
        return []
    vol_thr = _pct(sorted(vols), vol_q)
    move_thr = _pct(sorted(moves), move_q)

    out = []
    for i in range(1, len(mids)):
        c, mid = mids[i]
        if c.vol >= vol_thr and abs(mid - mids[i - 1][1]) <= move_thr:
            buy = sum(c.buy.values())
            sell = sum(c.sell.values())
            side = "buy" if buy >= sell else "sell"
            out.append({"kind": "absorption", "bucket": c.bucket,
                        "ti": int(mid), "size": c.vol, "side": side,
                        "note": f"absorbed {c.vol:,}"})
    out.sort(key=lambda d: -d["size"])
    return out[:top_n]


def detect_wall_breaks(cols, wall_mult: float = 4.0, top_n: int = 40) -> list[dict]:
    """A large resting level that disappeared while price traded through it."""
    if len(cols) < 3:
        return []
    out = []
    prev = None
    for c in cols:
        if prev is not None and prev.book and c.book:
            mid = None
            if c.bid_ti is not None and c.ask_ti is not None:
                mid = (c.bid_ti + c.ask_ti) / 2
            if mid is None:
                prev = c
                continue
            # Only walls within 2 ticks of the mid can qualify, so narrow to
            # that handful with numpy before touching Python. Scanning every
            # level of every column boxed the whole ladder on each step.
            pt, ps = prev.book.arrays()
            thr = max(1.0, float(ps.mean()) * wall_mult)
            cand = np.nonzero((ps >= thr) & (np.abs(pt - mid) <= 2))[0]
            for i in cand:
                ti, size = int(pt[i]), int(ps[i])
                if c.book.get(ti, 0) <= size * 0.25:
                    side = "bid" if ti < mid else "ask"
                    out.append({"kind": "wall_break", "bucket": c.bucket,
                                "ti": ti, "size": size, "side": side,
                                "note": f"wall {size:,} broke"})
        prev = c
    out.sort(key=lambda d: -d["size"])
    return out[:top_n]


# How much history the live detectors look at. Bounded ON PURPOSE.
#
# These ran over the WHOLE session on a 900 ms timer: 59 ms at one hour, 240 ms
# at eight - a quarter-second freeze of the GUI thread, three times a second's
# worth of budget, every 0.9 s. That is the single worst source of "it gets
# laggy after a while".
#
# Bounding is not just a speed fix, it is the right semantics. This feeds a
# live signals panel that displays 60 events newest-first; a block print from
# four hours ago is neither actionable nor new, yet it was being re-detected,
# re-sorted and re-discarded on every tick. At the default 1 s columns these
# windows are ~20 minutes of tape, which is what "recent flow" means to a
# scalper. Raise them if you want a longer memory - the cost is linear.
COLS_WINDOW = 1200
TRADES_WINDOW = 8000


def detect_all(buffer, agg: int = 1, cols_window: int = COLS_WINDOW,
               trades_window: int = TRADES_WINDOW) -> list[dict]:
    """Run every detector over a BookmapBuffer and return events newest-first.

    Only the most recent `cols_window` columns and `trades_window` prints are
    scanned, so the cost is flat in session length rather than growing with it.
    """
    cols = buffer.view(agg)
    if cols_window and len(cols) > cols_window:
        cols = cols[-cols_window:]
    trades = buffer.trades
    if trades_window and len(trades) > trades_window:
        # SLICE, do not islice. That comment used to read "a deque slices
        # badly" and it was true of a deque - but the tape is now a view over
        # ring arrays, and islice walks it from index 0, materialising a tuple
        # for every entry it then throws away. Measured: 34 ms -> 77 ms on
        # detect_all. The view's slice builds only the entries asked for.
        trades = trades[-trades_window:]
    ev = (detect_blocks(trades)
          + detect_absorption(cols)
          + detect_wall_breaks(cols))
    ev.sort(key=lambda d: -d["bucket"])
    return ev


# ---------------------------------------------------------------- divergence

def detect_delta_divergence(bars, lookback: int = 60, swing: int = 2,
                            min_bars: int = 3, top_n: int = 20) -> list[dict]:
    """Price makes a new extreme; cumulative delta does not.

    The trade this is for: price grinds to a higher high while the aggressive
    buying that should be driving it is smaller than it was at the last high.
    The move is being made by fewer and fewer buyers, so the next seller of
    size has less to absorb.

    MEASURED ON SWING POINTS, NOT ON EVERY BAR. Comparing consecutive bars
    would fire on noise - a single quiet bar in an advance is not a
    divergence, it is a quiet bar. A swing high here is a bar whose high is
    the highest of the `swing` bars either side of it, so the comparison is
    between two points a trader would also have marked.

    CUMULATIVE delta, not per-bar. Per-bar delta at a high says what that one
    bar did; the running total says what the whole leg did, which is the thing
    that is supposed to confirm the move.

    Returns newest last:
        {"kind": "bear_div"|"bull_div", "i": bar index, "prev_i": the swing it
         is compared against, "price": float, "delta": int, "prev_delta": int}
    """
    n = len(bars)
    if n < min_bars + 2 * swing + 2:
        return []
    lo_i = max(0, n - lookback)
    window = list(range(lo_i, n))

    # Running cumulative delta over the whole window, so both swings are read
    # off the same curve.
    cum = {}
    run = 0
    for i in window:
        run += bars[i].delta
        cum[i] = run

    def is_swing_high(i):
        if i - swing < lo_i or i + swing >= n:
            return False
        h = bars[i].high
        return all(bars[j].high <= h for j in range(i - swing, i + swing + 1)
                   if j != i)

    def is_swing_low(i):
        if i - swing < lo_i or i + swing >= n:
            return False
        l_ = bars[i].low
        return all(bars[j].low >= l_ for j in range(i - swing, i + swing + 1)
                   if j != i)

    out = []
    highs = [i for i in window if is_swing_high(i)]
    lows = [i for i in window if is_swing_low(i)]

    for seq, kind in ((highs, "bear_div"), (lows, "bull_div")):
        for a, b in zip(seq, seq[1:]):
            if b - a < min_bars:
                continue                      # too close to be two legs
            if kind == "bear_div":
                # Higher high in price, lower high in cumulative delta.
                if not (bars[b].high > bars[a].high and cum[b] < cum[a]):
                    continue
                price = bars[b].high
            else:
                # Lower low in price, higher low in delta.
                if not (bars[b].low < bars[a].low and cum[b] > cum[a]):
                    continue
                price = bars[b].low
            out.append({"kind": kind, "i": b, "prev_i": a, "price": price,
                        "delta": cum[b], "prev_delta": cum[a],
                        "ts": bars[b].start_ts})
    out.sort(key=lambda d: d["i"])
    return out[-top_n:]
