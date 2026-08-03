"""Bookmap-style time×price buffer.

Unlike `BarSeries` (which buckets into OHLC candles), this keeps a fine-grained
per-time-column record suited to a Bookmap liquidity view:

  * `Column` per `col_dt` seconds holding the resting-liquidity book, executed
    buy/sell volume by price, the best bid/ask, total volume, and how many book
    sweeps were actually observed in that column.
  * a flat `trades` deque for drawing sized bubbles at their exact time.

A column's book is forward-filled from its predecessor so a resting wall draws
as one continuous band. That is right for liquidity that genuinely persists, but
it makes "no sweep arrived" look identical to "nothing changed" - so `sweeps`
records what was really measured and the renderer fades the rest.

x-coordinate convention: the absolute column bucket `int(ts_s // col_dt)` is the
x value, so columns and trade bubbles share one continuous time axis and scroll
without re-indexing.
"""

from __future__ import annotations

import bisect
from collections import deque

import numpy as np

from .model import Aggressor, PriceLadder, EMPTY_LADDER
from .instruments import Instruments


class Column:
    __slots__ = ("bucket", "book", "buy", "sell", "bid_ti", "ask_ti", "vol",
                 "sweeps")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.book: PriceLadder = EMPTY_LADDER   # tick_index -> resting size
        self.buy: dict[int, int] = {}       # tick_index -> aggressive buy vol
        self.sell: dict[int, int] = {}      # tick_index -> aggressive sell vol
        self.bid_ti: int | None = None
        self.ask_ti: int | None = None
        self.vol = 0
        # Book sweeps actually observed in this column. Deliberately NOT
        # inherited by forward-fill: a column carrying the previous ladder
        # because nothing arrived is a column we never measured, and 0 here is
        # what lets the renderer say so rather than drawing an assumption as
        # fact. >1 means several sweeps collapsed into this column and only the
        # last survived.
        self.sweeps = 0


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
        # Aggregation cache, mirroring BarSeries. `view()` runs on the Bookmap's
        # 80 ms timer, and rebuilding the whole fold each time cost 6.6 ms at
        # one hour and 30.8 ms at eight - 38% of a core, growing with uptime.
        # Keyed by agg -> (version, folded cols, evicted count).
        self._agg_cache: dict[int, tuple[int, list, int]] = {}
        # Earliest BASE bucket modified since each agg was last folded. This is
        # what makes the rebuild incremental: a version counter alone bumps on
        # every trade, so it invalidates the cache without saying how much of it
        # is actually stale.
        self._dirty: dict[int, int | None] = {}
        self._version = 0
        self._evicted = 0
        self._all_cache: tuple[int, int, list] | None = None
        # O(1) running tape stats. The stats dock asked for these 2.5x a second
        # by materialising every print in the deque into a Python list - 6.5 ms
        # a call once it filled. Like BarSeries' session figures these cover
        # every trade ingested and deliberately do NOT shrink when the deque
        # rolls: a session's largest print is not less true for having scrolled
        # out of the ring.
        self.trade_count = 0
        self.trade_vol = 0
        self.trade_max = 0

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
                # to a fresh PriceLadder rather than mutating, and a ladder is
                # read-only by contract, so no column can alter another's book
                # and forward-fill costs nothing.
                prev = self.cols[self.order[pos - 1]]
                c.book = prev.book
                c.bid_ti = prev.bid_ti
                c.ask_ti = prev.ask_ti
            while len(self.order) > self.max_cols:
                self.cols.pop(self.order.pop(0), None)
                self._evicted += 1
        self._touch(b)
        return c

    def _touch(self, bucket: int) -> None:
        """Record that `bucket` changed, for every cached aggregation."""
        self._version += 1
        d = self._dirty
        for agg, cur in d.items():
            if cur is None or bucket < cur:
                d[agg] = bucket

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
        self.trade_count += 1
        self.trade_vol += tr.size
        if tr.size > self.trade_max:
            self.trade_max = tr.size

    def add_book(self, bk) -> None:
        c = self._col(bk.ts_ms)
        sym = self.symbol
        tick = self.instruments.tick(sym)
        # One vectorised conversion, cached on the snapshot, so the BarSeries
        # gets the same object for free instead of redoing the whole book.
        c.book = bk.ladder(tick)
        c.sweeps += 1
        if bk.best_bid is not None:
            c.bid_ti = round(bk.best_bid / tick)
        if bk.best_ask is not None:
            c.ask_ti = round(bk.best_ask / tick)

    # ---- read access -----------------------------------------------------
    def columns(self) -> list[Column]:
        """All columns in time order. Cached: at the 14,400-column cap this
        list costs ~0.9 ms to rebuild and several consumers ask for it on
        their own timers."""
        c = self._all_cache
        if c is not None and c[0] == self._version and c[1] == self._evicted:
            return c[2]
        out = [self.cols[b] for b in self.order]
        self._all_cache = (self._version, self._evicted, out)
        return out

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
        buy/sell/vol are summed; BBO = latest.

        Incremental. This is on the 80 ms refresh path, and re-folding the whole
        buffer every call grew from 6.6 ms at one hour to 30.8 ms at eight -
        38% of a core, purely because history got longer. Live data only ever
        appends, so all but the last group are already correct; only the tail
        from the dirty watermark is rebuilt.
        """
        if agg <= 1:
            return self.columns()

        cached = self._agg_cache.get(agg)
        out = None
        if cached is not None and cached[2] == self._evicted:
            if cached[0] == self._version:
                return cached[1]                  # nothing changed at all
            dirty = self._dirty.get(agg)
            if dirty is not None:
                # Groups strictly before the dirty one are untouched; keep them
                # and re-fold from there.
                gb = dirty // agg
                prev = cached[1]
                k = len(prev)
                while k > 0 and prev[k - 1].bucket >= gb:
                    k -= 1
                i = len(self.order)
                while i > 0 and self.order[i - 1] // agg >= gb:
                    i -= 1
                out = prev[:k] + self._fold(agg, i)
        if out is None:
            out = self._fold(agg, 0)

        self._agg_cache[agg] = (self._version, out, self._evicted)
        self._dirty[agg] = None
        return out

    def _fold(self, agg: int, start: int) -> list[Column]:
        groups: dict[int, Column] = {}
        order: list[int] = []
        for b in self.order[start:]:
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
            # Summed, so an aggregated column reports how many sweeps its whole
            # span was built from - 0 still means "nothing was observed here".
            g.sweeps += c.sweeps
            if c.bid_ti is not None:
                g.bid_ti = c.bid_ti
            if c.ask_ti is not None:
                g.ask_ti = c.ask_ti
        return [groups[b] for b in order]
