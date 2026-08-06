"""Price alerts: mark a level, get told when it trades there.

FIRES ON A CROSSING, NOT ON A TOUCH. The difference matters on a live tape. A
level sitting at 400.50 with price oscillating 400.49/400.50/400.49 would fire
on every print if "touched" were the test - dozens of beeps a second, which is
worse than no alert at all because you learn to ignore it.

So an alert arms against a SIDE. It records which side of the level price was
on when the alert was created, and fires the first time a print lands on the
other side (or exactly on the level). After that it is spent, unless it was
created as repeating.

WHY IT LIVES IN THE ENGINE. Alerts must fire for symbols that are not on
screen - the whole point is being told about a level on a name you are not
currently watching. So the check runs where every trade passes, in the event
drain, not in a chart's paint.

The check is one dict lookup and at most a couple of float comparisons per
trade, which matters: it runs on every print of every symbol.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

# Which way price must move through the level for the alert to fire.
ABOVE = "above"      # fire when price reaches or exceeds the level
BELOW = "below"      # fire when price reaches or falls below the level
CROSS = "cross"      # fire on either side - "tell me when it trades here"

_ids = itertools.count(1)


@dataclass
class PriceAlert:
    symbol: str
    price: float
    direction: str = CROSS
    note: str = ""
    repeating: bool = False
    id: int = field(default_factory=lambda: next(_ids))
    armed: bool = True
    created_ts: float = field(default_factory=time.time)
    fired_ts: float = 0.0
    fired_price: float = 0.0
    # Side of the level price was on when this alert was armed. None until the
    # first print arrives - an alert created before any trade has no reference
    # point, and guessing one would fire it immediately on a level price is
    # already past.
    _side: int | None = None

    def describe(self) -> str:
        d = {ABOVE: "rises to", BELOW: "falls to", CROSS: "trades at"}[self.direction]
        return f"{self.symbol} {d} {self.price:,.2f}"


class AlertBook:
    """Every alert, indexed by symbol so the per-trade check stays cheap."""

    def __init__(self) -> None:
        self._by_symbol: dict[str, list[PriceAlert]] = {}
        self.fired: list[PriceAlert] = []          # newest last

    # ---- management ------------------------------------------------------
    def add(self, symbol: str, price: float, direction: str = CROSS,
            note: str = "", repeating: bool = False) -> PriceAlert:
        a = PriceAlert(symbol.upper(), float(price), direction, note, repeating)
        self._by_symbol.setdefault(a.symbol, []).append(a)
        return a

    def remove(self, alert_id: int) -> bool:
        for sym, lst in self._by_symbol.items():
            for i, a in enumerate(lst):
                if a.id == alert_id:
                    del lst[i]
                    return True
        return False

    def clear(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._by_symbol.clear()
        else:
            self._by_symbol.pop(symbol.upper(), None)

    def all(self) -> list[PriceAlert]:
        out: list[PriceAlert] = []
        for lst in self._by_symbol.values():
            out.extend(lst)
        out.sort(key=lambda a: (a.symbol, a.price))
        return out

    def for_symbol(self, symbol: str) -> list[PriceAlert]:
        return list(self._by_symbol.get(symbol.upper(), ()))

    def active_count(self) -> int:
        return sum(1 for a in self.all() if a.armed)

    # ---- the hot path ----------------------------------------------------
    def check(self, symbol: str, last: float) -> list[PriceAlert]:
        """Feed one print. Returns the alerts it triggered, usually none.

        Called for every trade of every symbol, so the common case - no alert
        on this symbol - is a single dict lookup that finds nothing.
        """
        lst = self._by_symbol.get(symbol)
        if not lst or not (last > 0.0) or last != last:
            return []
        hit = []
        for a in lst:
            if not a.armed:
                continue
            side = 1 if last > a.price else (-1 if last < a.price else 0)
            if a._side is None:
                # First print since the alert was armed. Record which side we
                # started on and fire nothing - an alert created at a level
                # price is already past must not go off on the next tick.
                a._side = side
                if side == 0:
                    # Created exactly at the traded price: arm against the side
                    # it leaves on, so the next move through it counts.
                    a._side = None
                continue
            fire = False
            if a.direction == ABOVE:
                fire = side >= 0 and a._side < 0
            elif a.direction == BELOW:
                fire = side <= 0 and a._side > 0
            else:                                   # CROSS
                fire = side == 0 or (side != 0 and a._side != 0 and side != a._side)
            if fire:
                a.fired_ts = time.time()
                a.fired_price = last
                if a.repeating:
                    a._side = side if side != 0 else None
                else:
                    a.armed = False
                hit.append(a)
                self.fired.append(a)
                if len(self.fired) > 200:
                    del self.fired[:-200]
            elif side != 0:
                a._side = side
        return hit

    # ---- persistence -----------------------------------------------------
    def to_list(self) -> list[dict]:
        return [{"symbol": a.symbol, "price": a.price, "direction": a.direction,
                 "note": a.note, "repeating": a.repeating, "armed": a.armed}
                for a in self.all()]

    def load(self, rows) -> None:
        self._by_symbol.clear()
        for r in rows or ():
            try:
                a = self.add(r["symbol"], float(r["price"]),
                             r.get("direction", CROSS), r.get("note", ""),
                             bool(r.get("repeating", False)))
                a.armed = bool(r.get("armed", True))
            except (KeyError, TypeError, ValueError):
                continue
