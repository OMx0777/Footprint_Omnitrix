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
