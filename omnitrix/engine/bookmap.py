"""Bookmap-style time×price buffer.

Unlike `BarSeries` (which buckets into OHLC candles), this keeps a fine-grained
per-time-column record suited to a Bookmap liquidity view:

  * `Column` per `col_dt` seconds holding the resting-liquidity book, executed
    buy/sell volume by price, the best bid/ask, and total volume.
  * a flat `trades` deque for drawing sized bubbles at their exact time.

x-coordinate convention: the absolute column bucket `int(ts_s // col_dt)` is the
x value, so columns and trade bubbles share one continuous time axis and scroll
without re-indexing.
"""

from __future__ import annotations

import bisect
from collections import deque

from .model import Aggressor
from .instruments import Instruments


class Column:
    __slots__ = ("bucket", "book", "buy", "sell", "bid_ti", "ask_ti", "vol")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.book: dict[int, int] = {}      # tick_index -> resting size
        self.buy: dict[int, int] = {}       # tick_index -> aggressive buy vol
        self.sell: dict[int, int] = {}      # tick_index -> aggressive sell vol
        self.bid_ti: int | None = None
        self.ask_ti: int | None = None
        self.vol = 0


class BookmapBuffer:
    def __init__(self, symbol: str, instruments: Instruments,
                 col_dt: float = 1.0, max_cols: int = 1400,
                 max_trades: int = 60000):
        self.symbol = symbol
        self.instruments = instruments
        self.col_dt = col_dt
        self.max_cols = max_cols
        self.cols: dict[int, Column] = {}
        # Kept sorted by bucket, not insertion order: two feeds (trades on L1,
        # books on L2) interleave, and a late arrival must not make latest()
        # report an older column or leave columns() non-monotonic in x - the
        # BBO line and the x-axis follow that order directly.
        self.order: list[int] = []
        self.trades: deque[tuple[float, int, int, Aggressor]] = deque(maxlen=max_trades)

    def _col(self, ts_ms: int) -> Column:
        b = int((ts_ms / 1000.0) // self.col_dt)
        c = self.cols.get(b)
        if c is None:
            c = Column(b)
            self.cols[b] = c
            if self.order and b > self.order[-1]:
                pos = len(self.order)         # fast path: normal forward time
                self.order.append(b)
            else:
                pos = bisect.bisect_left(self.order, b)
                self.order.insert(pos, b)
            if pos > 0:
                # Carry the last known book forward. A resting limit order stays
                # on the ladder until a later sweep replaces it, so its band has
                # to be continuous across every column in between - that
                # unbroken streak is what makes a wall that has held for minutes
                # visible at a glance. Without this the heatmap only marks the
                # columns that happened to receive a sweep, and a standing wall
                # renders as scattered dashes.
                #
                # The reference is shared, not copied: add_book rebinds c.book
                # to a fresh dict rather than mutating, so no column can alter
                # another's book, and forward-fill costs nothing.
                prev = self.cols[self.order[pos - 1]]
                c.book = prev.book
                c.bid_ti = prev.bid_ti
                c.ask_ti = prev.ask_ti
            while len(self.order) > self.max_cols:
                self.cols.pop(self.order.pop(0), None)
        return c

    def add_trade(self, tr) -> None:
        c = self._col(tr.ts_ms)
        ti = self.instruments.to_index(self.symbol, tr.price)
        if tr.aggressor is Aggressor.BUY:
            c.buy[ti] = c.buy.get(ti, 0) + tr.size
        elif tr.aggressor is Aggressor.SELL:
            c.sell[ti] = c.sell.get(ti, 0) + tr.size
        else:
            h = tr.size // 2
            c.buy[ti] = c.buy.get(ti, 0) + h
            c.sell[ti] = c.sell.get(ti, 0) + tr.size - h
        c.vol += tr.size
        x = (tr.ts_ms / 1000.0) / self.col_dt
        self.trades.append((x, ti, tr.size, tr.aggressor))

    def add_book(self, bk) -> None:
        c = self._col(bk.ts_ms)
        to_index = self.instruments.to_index
        sym = self.symbol
        book: dict[int, int] = {}
        for price, size in bk.bids.items():
            book[to_index(sym, price)] = size
        for price, size in bk.asks.items():
            book[to_index(sym, price)] = size
        c.book = book
        if bk.best_bid is not None:
            c.bid_ti = to_index(sym, bk.best_bid)
        if bk.best_ask is not None:
            c.ask_ti = to_index(sym, bk.best_ask)

    # ---- read access -----------------------------------------------------
    def columns(self) -> list[Column]:
        return [self.cols[b] for b in self.order]

    def latest(self) -> Column | None:
        return self.cols[self.order[-1]] if self.order else None

    def latest_book(self) -> Column | None:
        """Most recent column that actually carries resting liquidity.

        `latest()` can land on a column created by a trade after the last book
        snapshot, which has no book at all - using it for the DOM ladder or the
        forward projection makes them blink empty. Walk back to the newest
        column that has depth."""
        for b in reversed(self.order):
            c = self.cols[b]
            if c.book:
                return c
        return None

    def view(self, agg: int = 1) -> list[Column]:
        """Aggregate every `agg` base columns into one coarser column (the
        bookmap 'timeframe'). Resting book = most recent snapshot in the group;
        buy/sell/vol are summed; BBO = latest."""
        if agg <= 1:
            return self.columns()
        groups: dict[int, Column] = {}
        order: list[int] = []
        for b in self.order:
            c = self.cols[b]
            gb = b // agg
            g = groups.get(gb)
            if g is None:
                g = Column(gb)
                groups[gb] = g
                order.append(gb)
            if c.book:
                # Share, don't copy: books are only ever rebound, never mutated
                # in place. Now that every column carries a forward-filled book,
                # copying here would clone the whole ladder once per column on
                # every refresh.
                g.book = c.book                # latest book wins
            for ti, v in c.buy.items():
                g.buy[ti] = g.buy.get(ti, 0) + v
            for ti, v in c.sell.items():
                g.sell[ti] = g.sell.get(ti, 0) + v
            g.vol += c.vol
            if c.bid_ti is not None:
                g.bid_ti = c.bid_ti
            if c.ask_ti is not None:
                g.ask_ti = c.ask_ti
        return [groups[b] for b in order]
