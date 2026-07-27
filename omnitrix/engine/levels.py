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

        # Accumulate size-over-time per price. Consecutive columns share one
        # forward-filled book object, so a run is charged once and weighted by
        # its length instead of being walked column by column.
        score: dict[int, float] = {}
        held: dict[int, int] = {}
        seen = None
        run = 0
        for c in recent:
            bk = c.book
            if bk is seen:
                run += 1
                continue
            if seen is not None:
                _accumulate(score, held, seen, run)
            seen, run = bk, 1
        if seen is not None:
            _accumulate(score, held, seen, run)

        best_sup = best_res = None
        for ti, sc in score.items():
            if ti not in current or held[ti] < self.min_hold:
                continue          # gone from the book, or no track record yet
            lv = Level(ti, sc, current[ti], held[ti])
            if ti < mid_ti:
                if best_sup is None or sc > best_sup.score:
                    best_sup = lv
            elif ti > mid_ti:
                if best_res is None or sc > best_res.score:
                    best_res = lv

        self.support = self._settle(self.support, best_sup, current, score, held)
        self.resistance = self._settle(self.resistance, best_res, current,
                                       score, held)
        return self.support, self.resistance

    def _settle(self, holder: Level | None, best: Level | None,
                current: dict, score: dict, held: dict) -> Level | None:
        """Keep the incumbent unless the challenger clearly beats it."""
        if best is None:
            return None
        if holder is None or holder.ti == best.ti:
            return best
        if holder.ti not in current:
            return best                      # incumbent was pulled
        # Re-score the incumbent on this frame's evidence before comparing.
        inc = Level(holder.ti, score.get(holder.ti, 0.0),
                    current[holder.ti], held.get(holder.ti, 0))
        if best.score > inc.score * (1.0 + self.hysteresis):
            return best
        return inc


def _accumulate(score: dict, held: dict, book: dict, run: int) -> None:
    for ti, size in book.items():
        if size > 0:
            score[ti] = score.get(ti, 0.0) + size * run
            held[ti] = held.get(ti, 0) + run
