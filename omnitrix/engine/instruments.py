"""Per-symbol instrument metadata — principally the price tick size.

The old app hard-coded tick_size = 0.01 for every symbol, which is wrong for
anything not penny-ticked. Here each symbol can carry its own tick, and prices
are addressed by an integer *tick index* so diagonal-neighbour math in the
footprint (ask at level i vs bid at level i-1) is exact, never float-fuzzy.
"""

from __future__ import annotations


class Instruments:
    """Registry of tick sizes with a sane default."""

    def __init__(self, default_tick: float = 0.01):
        self._default = default_tick
        self._ticks: dict[str, float] = {}

    def set_tick(self, symbol: str, tick: float) -> None:
        self._ticks[symbol.upper()] = tick

    def tick(self, symbol: str) -> float:
        return self._ticks.get(symbol.upper(), self._default)

    def to_index(self, symbol: str, price: float) -> int:
        """Snap a raw price onto its integer tick index."""
        return round(price / self.tick(symbol))

    def to_price(self, symbol: str, index: int) -> float:
        """Inverse of to_index — the canonical price for a tick index."""
        return round(index * self.tick(symbol), 10)
