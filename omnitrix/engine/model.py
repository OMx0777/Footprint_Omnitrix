"""Core immutable data types that flow through the engine.

Everything downstream (bars, footprint, delta, CVD) is built from `Trade`
events. A `BookSnapshot` is only used by the liquidity heatmap / DOM ladder —
it never contributes to footprint volume.

Aggressor convention (this is the whole ballgame for order flow):
    BUY  -> trade executed at/above the ask; an aggressive buyer lifted offers.
            Counts toward the *ask* (right) column of the footprint.
    SELL -> trade executed at/below the bid; an aggressive seller hit bids.
            Counts toward the *bid* (left) column of the footprint.
    UNKNOWN -> could not be classified (mid print, no quote). Split evenly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np


class Aggressor(str, Enum):
    BUY = "buy"        # lifted the ask
    SELL = "sell"      # hit the bid
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class Trade:
    """A single time-and-sales print."""

    symbol: str
    price: float
    size: int
    aggressor: Aggressor
    ts_ms: int          # market timestamp, epoch milliseconds


@dataclass(slots=True, frozen=True)
class BookSnapshot:
    """A full L2 depth sweep at one instant (used by heatmap / DOM only)."""

    symbol: str
    bids: dict[float, int]      # price -> resting size
    asks: dict[float, int]
    ts_ms: int

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None


class PriceLadder:
    """Resting depth for one column: tick_index -> size, kept as int32 arrays.

    This is the app's dominant allocation, so it does not get to be a dict. At
    the live sweep rate (2.1/sec/symbol against 1-second columns) essentially
    every column receives its own sweep, so the forward-fill sharing below
    never actually fires on live data - measured at 0% - and each column pays
    for a full ladder. As a `dict[int, int]` of boxed ints that is 23.6 kB per
    column; as two int32 arrays it is 2.3 kB. Measured 10.4x, which across 100
    symbols at the 1400-column cap is 4.2 GB -> 0.4 GB.

    READ-ONLY BY CONTRACT. Columns share ladders by reference (see `_col`), so
    mutating one would silently rewrite history for every column carrying it
    forward. Nothing in the codebase mutates a book; keep it that way.

    Exposes just enough of the mapping protocol for the existing call sites
    (`values`, `items`, `get`, truthiness, `len`). `arrays()` is the fast path:
    the renderer wants the raw buffers, not 256 boxed tuples per column.
    """

    __slots__ = ("ti", "sz", "_max")

    def __init__(self, ti: np.ndarray, sz: np.ndarray):
        self.ti = ti                 # int32, ascending - `get` binary-searches
        self.sz = sz                 # int32, parallel to ti
        self._max = -1               # lazily cached max size

    # ---- mapping protocol (enough for every existing consumer) -----------
    def __len__(self) -> int:
        return int(self.ti.size)

    def __bool__(self) -> bool:
        return bool(self.ti.size)

    def __iter__(self):
        # Iterating a mapping yields its keys; `max(book)` (the DOM ladder's
        # top-of-book probe) depends on this.
        return iter(self.ti.tolist())

    def __contains__(self, ti: int) -> bool:
        i = int(np.searchsorted(self.ti, ti))
        return i < self.ti.size and int(self.ti[i]) == ti

    def __getitem__(self, ti: int) -> int:
        i = int(np.searchsorted(self.ti, ti))
        if i < self.ti.size and int(self.ti[i]) == ti:
            return int(self.sz[i])
        raise KeyError(ti)

    def get(self, ti: int, default: int = 0) -> int:
        i = int(np.searchsorted(self.ti, ti))
        if i < self.ti.size and int(self.ti[i]) == ti:
            return int(self.sz[i])
        return default

    def keys(self):
        return self.ti

    def values(self):
        # numpy array: `max(...)` and `sum(...)` both work on it, and callers
        # that want speed can use `.max()` / `.sum()` directly.
        return self.sz

    def items(self):
        # Boxes, so it is the slow path by construction - only used off the
        # per-frame hot path (metrics, signals, S/R levels).
        return zip(self.ti.tolist(), self.sz.tolist())

    # ---- fast paths ------------------------------------------------------
    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """(tick_index, size) as int32 arrays. No copy - do not write to them."""
        return self.ti, self.sz

    def max_size(self) -> int:
        """Largest resting size, cached. The renderer asks every visible column
        for this on every frame to normalise the heat ramp."""
        if self._max < 0:
            self._max = int(self.sz.max()) if self.sz.size else 0
        return self._max


EMPTY_LADDER = PriceLadder(np.empty(0, dtype=np.int32),
                            np.empty(0, dtype=np.int32))
