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


def sides(col):
    """Split a column's combined book into (bid_depth, ask_depth)."""
    bid = ask = 0
    b_ti, a_ti = col.bid_ti, col.ask_ti
    for ti, size in col.book.items():
        if a_ti is not None and ti >= a_ti:
            ask += size
        elif b_ti is not None and ti <= b_ti:
            bid += size
        else:                       # inside the spread — split evenly
            bid += size // 2
            ask += size - size // 2
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
