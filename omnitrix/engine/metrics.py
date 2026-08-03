"""Microstructure metric series derived from BookmapBuffer columns.

Pure functions — each returns (xs, ys) ready to plot. They turn the raw L2
book and tick-by-tick prints into the reads an institutional desk watches:

  book_imbalance  — resting bid depth vs ask depth  (L2 pressure, -1..+1)
  spread_ticks    — bid/ask spread width            (liquidity stress)
  intensity       — traded volume per column        (speed of tape)
  delta / cvd     — aggressive buy minus sell       (who is lifting/hitting)
  large_ratio     — share of volume from big prints (institutional footprint)
"""

from __future__ import annotations

import numpy as np


def sides(col):
    """Split a column's combined book into (bid_depth, ask_depth).

    Vectorised: this is called per column across the Analytics panes (400
    columns) and once per symbol on the Market Monitor's timer, and iterating
    `book.items()` boxed the ladder's int32 arrays into Python lists every time.
    """
    ti, sz = col.book.arrays()
    if ti.size == 0:
        return 0, 0
    sz = sz.astype(np.int64)
    b_ti, a_ti = col.bid_ti, col.ask_ti

    # Ask is tested first, exactly as the original if/elif did, so on a crossed
    # book a level that satisfies both bounds counts as ask.
    is_ask = (ti >= a_ti) if a_ti is not None else np.zeros(ti.size, dtype=bool)
    is_bid = ((ti <= b_ti) if b_ti is not None
              else np.zeros(ti.size, dtype=bool)) & ~is_ask
    inside = ~(is_ask | is_bid)          # inside the spread — split evenly

    half = sz[inside] // 2               # integer split, as before
    bid = int(sz[is_bid].sum() + half.sum())
    ask = int(sz[is_ask].sum() + (sz[inside] - half).sum())
    return bid, ask


def book_imbalance(cols) -> tuple[list, list]:
    """(bid-ask)/(bid+ask) per column: +1 = all bid depth, -1 = all ask."""
    xs, ys = [], []
    for c in cols:
        if not c.book:
            continue
        b, a = sides(c)
        tot = b + a
        if tot:
            xs.append(c.bucket + 0.5)
            ys.append((b - a) / tot)
    return xs, ys


def spread_ticks(cols) -> tuple[list, list]:
    xs, ys = [], []
    for c in cols:
        if c.bid_ti is None or c.ask_ti is None:
            continue
        xs.append(c.bucket + 0.5)
        ys.append(max(0, c.ask_ti - c.bid_ti))
    return xs, ys


def intensity(cols) -> tuple[list, list]:
    return [c.bucket + 0.5 for c in cols], [c.vol for c in cols]


def delta_series(cols) -> tuple[list, list]:
    xs, ys = [], []
    for c in cols:
        xs.append(c.bucket + 0.5)
        ys.append(sum(c.buy.values()) - sum(c.sell.values()))
    return xs, ys


def cvd_series(cols) -> tuple[list, list]:
    xs, ys, acc = [], [], 0
    for c in cols:
        acc += sum(c.buy.values()) - sum(c.sell.values())
        xs.append(c.bucket + 0.5)
        ys.append(acc)
    return xs, ys


def depth_totals(cols) -> tuple[list, list, list]:
    """Per-column (x, bid_depth, ask_depth) — resting liquidity on each side."""
    xs, bids, asks = [], [], []
    for c in cols:
        if not c.book:
            continue
        b, a = sides(c)
        xs.append(c.bucket + 0.5)
        bids.append(b)
        asks.append(a)
    return xs, bids, asks
