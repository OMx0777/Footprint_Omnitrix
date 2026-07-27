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

from statistics import median


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
            avg = sum(prev.book.values()) / len(prev.book)
            thr = max(1.0, avg * wall_mult)
            mid = None
            if c.bid_ti is not None and c.ask_ti is not None:
                mid = (c.bid_ti + c.ask_ti) / 2
            for ti, size in prev.book.items():
                if size < thr:
                    continue
                now = c.book.get(ti, 0)
                if now <= size * 0.25 and mid is not None:
                    # price must have reached / crossed the level
                    if abs(mid - ti) <= 2:
                        side = "bid" if ti < mid else "ask"
                        out.append({"kind": "wall_break", "bucket": c.bucket,
                                    "ti": ti, "size": int(size), "side": side,
                                    "note": f"wall {int(size):,} broke"})
        prev = c
    out.sort(key=lambda d: -d["size"])
    return out[:top_n]


def detect_all(buffer, agg: int = 1) -> list[dict]:
    """Run every detector over a BookmapBuffer and return events newest-first."""
    cols = buffer.view(agg)
    ev = (detect_blocks(buffer.trades)
          + detect_absorption(cols)
          + detect_wall_breaks(cols))
    ev.sort(key=lambda d: -d["bucket"])
    return ev
