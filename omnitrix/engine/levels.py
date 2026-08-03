"""Persistence-weighted support / resistance from the resting order book.

Picking the single largest level in the current snapshot is a poor S/R signal:
a spoof that flashes 20k shares for two seconds outscores a genuine 5k wall that
has absorbed everything thrown at it for four minutes. Traders read the second
one as support precisely *because* it has held.

So a level is scored by the liquidity-time it has accumulated - the size summed
over every column it was resting in, i.e. share-seconds - rather than by its
instantaneous size. Standing size wins over flashes automatically, with no
special-casing.

Three qualifiers keep the output honest:

  * still resting  - a level absent from the newest book is history, not a level
                     price is about to meet.
  * minimum hold   - a level seen in only a column or two has no track record
                     yet, whatever its size.
  * hysteresis     - the chosen level only changes when a challenger clearly
                     beats it. A line that flips between two adjacent prices
                     every frame is unreadable, and traders anchor to a level
                     for as long as it holds.
"""

from __future__ import annotations

import numpy as np


class Level:
    """A support or resistance price with the evidence behind it."""

    __slots__ = ("ti", "score", "size", "held")

    def __init__(self, ti: int, score: float, size: int, held: int):
        self.ti = ti          # tick index (price / tick)
        self.score = score    # accumulated size-over-time (share-columns)
        self.size = size      # size currently resting there
        self.held = held      # columns it has been present for

    def __repr__(self) -> str:                       # pragma: no cover
        return (f"Level(ti={self.ti}, size={self.size}, "
                f"held={self.held}, score={self.score:.0f})")


class SRTracker:
    """Tracks the strongest resting support and resistance around price."""

    def __init__(self, window: int = 180, hysteresis: float = 0.20,
                 min_hold: int = 3):
        self.window = window          # columns of history to weigh
        self.hysteresis = hysteresis  # challenger must beat the holder by this
        self.min_hold = min_hold      # columns before a level is eligible
        self.support: Level | None = None
        self.resistance: Level | None = None

    def reset(self) -> None:
        self.support = self.resistance = None

    def update(self, cols: list, mid_ti: float | None
               ) -> tuple[Level | None, Level | None]:
        """Recompute from the newest `window` columns. Returns (support, resist)."""
        if not cols or mid_ti is None:
            self.reset()
            return None, None

        current = cols[-1].book
        if not current:
            return self.support, self.resistance

        recent = cols[-self.window:]

        # Score only the prices still resting in the NEWEST book.
        #
        # A level absent from `current` is filtered out below whatever it
        # scored, so accumulating over every price ever seen in the window was
        # work thrown away - and on a busy name the window's union of prices is
        # many times the size of one ladder. Restricting up front makes the
        # whole pass a fixed-width numpy accumulation over ~256 candidates.
        cur_ti, cur_sz = current.arrays()

        # Consecutive columns share one forward-filled book object, so a run is
        # charged once and weighted by its length rather than walked column by
        # column. (On live data sharing measures 0%, so this is the general
        # case, not the fast path - which is exactly why the accumulation has
        # to be vectorised rather than a Python loop over levels.)
        runs: list[tuple[object, int]] = []
        seen = None
        run = 0
        for c in recent:
            bk = c.book
            if bk is seen:
                run += 1
                continue
            if seen is not None and len(seen):
                runs.append((seen, run))
            seen, run = bk, 1
        if seen is not None and len(seen):
            runs.append((seen, run))
        if not runs:
            return self.support, self.resistance

        score, held = _accumulate(cur_ti, runs)

        # `held > 0` as well as the threshold: a candidate that never once
        # carried size has no evidence at all, and the dict version could not
        # represent it. That only diverges if min_hold is set to 0, but it
        # would silently admit empty levels if it did.
        eligible = (held >= self.min_hold) & (held > 0)
        best_sup = _best(cur_ti, cur_sz, score, held,
                         eligible & (cur_ti < mid_ti))
        best_res = _best(cur_ti, cur_sz, score, held,
                         eligible & (cur_ti > mid_ti))

        lookup = _Scores(cur_ti, score, held)
        self.support = self._settle(self.support, best_sup, current, lookup)
        self.resistance = self._settle(self.resistance, best_res, current,
                                       lookup)
        return self.support, self.resistance

    def _settle(self, holder: Level | None, best: Level | None,
                current, lookup: "_Scores") -> Level | None:
        """Keep the incumbent unless the challenger clearly beats it."""
        if best is None:
            return None
        if holder is None or holder.ti == best.ti:
            return best
        if holder.ti not in current:
            return best                      # incumbent was pulled
        # Re-score the incumbent on this frame's evidence before comparing.
        sc, hd = lookup.get(holder.ti)
        inc = Level(holder.ti, sc, current[holder.ti], hd)
        if best.score > inc.score * (1.0 + self.hysteresis):
            return best
        return inc


class _Scores:
    """Score/held lookup by tick index, over the candidate array."""

    __slots__ = ("ti", "score", "held")

    def __init__(self, ti, score, held):
        self.ti, self.score, self.held = ti, score, held

    def get(self, ti: int) -> tuple[float, int]:
        i = int(np.searchsorted(self.ti, ti))
        if i < self.ti.size and int(self.ti[i]) == ti:
            return float(self.score[i]), int(self.held[i])
        return 0.0, 0


def _best(cur_ti, cur_sz, score, held, mask) -> Level | None:
    """Highest-scoring eligible level on one side of the mid."""
    if not mask.any():
        return None
    masked = np.where(mask, score, -1.0)
    i = int(masked.argmax())
    if masked[i] < 0:
        return None
    return Level(int(cur_ti[i]), float(score[i]), int(cur_sz[i]),
                 int(held[i]))


# Above this many distinct tick indices the flat accumulator is not worth
# allocating; fall back to a search per run. A window spans a few hundred ticks
# in practice, so this is a guard against a corrupt ladder, not a real case.
_MAX_SPAN = 1 << 20


def _accumulate(cur_ti, runs) -> tuple[np.ndarray, np.ndarray]:
    """Score/held for each candidate, over every run in the window.

    Was a Python loop over `book.items()`, which on a PriceLadder boxes both
    int32 arrays into lists on every call - roughly 46,000 boxed iterations per
    refresh at a 180-column window.

    Every run is folded in ONE pass rather than one vectorised call per run:
    with no forward-fill sharing on live data there are ~180 runs of ~256
    levels, and numpy's per-call overhead on arrays that small dominated the
    arithmetic. Concatenating first turns ~1,400 small calls into a handful of
    large ones.
    """
    tis = [r[0].ti for r in runs]
    szs = [r[0].sz for r in runs]
    counts = np.fromiter((t.size for t in tis), dtype=np.int64, count=len(tis))
    all_ti = np.concatenate(tis).astype(np.int64)
    all_sz = np.concatenate(szs).astype(np.float64)
    weight = np.repeat(np.fromiter((r[1] for r in runs), dtype=np.int64,
                                   count=len(runs)), counts)

    base = int(all_ti.min())
    span = int(all_ti.max()) - base + 1
    if span > _MAX_SPAN:
        return _accumulate_sparse(cur_ti, runs)

    pos = all_ti - base
    live = all_sz > 0                       # a level with no size is no evidence
    sc = np.bincount(pos[live], weights=all_sz[live] * weight[live],
                     minlength=span)
    hd = np.bincount(pos[live], weights=weight[live].astype(np.float64),
                     minlength=span)

    # Project onto the candidates; anything outside the window's range scored 0.
    cur = cur_ti.astype(np.int64) - base
    inside = (cur >= 0) & (cur < span)
    safe = np.where(inside, cur, 0)
    score = np.where(inside, sc[safe], 0.0)
    held = np.where(inside, hd[safe], 0.0).astype(np.int64)
    return score, held


def _accumulate_sparse(cur_ti, runs) -> tuple[np.ndarray, np.ndarray]:
    """Fallback for a pathologically wide tick range: search per run."""
    score = np.zeros(cur_ti.size, dtype=np.float64)
    held = np.zeros(cur_ti.size, dtype=np.int64)
    for book, run in runs:
        bt, bs = book.arrays()
        idx = np.searchsorted(bt, cur_ti)
        safe = np.clip(idx, 0, bt.size - 1)
        sz = bs[safe]
        hit = (idx < bt.size) & (bt[safe] == cur_ti) & (sz > 0)
        if hit.any():
            score[hit] += sz[hit].astype(np.float64) * run
            held[hit] += run
    return score, held
