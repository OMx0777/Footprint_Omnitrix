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

from .model import Aggressor, PriceLadder, EMPTY_LADDER, split_size
from .instruments import Instruments


# Aggressor <-> uint8, because storing the enum boxes a pointer per trade.
_AG_CODE = {Aggressor.BUY: 0, Aggressor.SELL: 1, Aggressor.UNKNOWN: 2}

# Tape ring: initial allocation, and how much per-column history a symbol
# nobody is watching keeps. Both exist for the same reason - the process has to
# hold thousands of symbols, and almost none of them are on screen.
#
# 2,048 prints is a few seconds of a busy name and 8 kB of the 1.02 MB a full
# ring costs, so a thin symbol - most of a thousand-symbol universe - never
# pays for depth it does not use.
TAPE_SEED = 2048
# 150 one-second columns is two and a half minutes: enough that selecting a
# symbol shows immediate context rather than an empty chart, at about a ninth
# of the full 1400-column retention.
COLD_COLS = 150
# Tape ceiling for a symbol nothing is drawing.
#
# Growing on demand fixed the symbol that never prints; it does nothing for the
# symbol that prints constantly and is simply not on screen, which grows to the
# full 60,000 and 1.02 MB. Across a thousand active symbols that is the last
# unbounded gigabyte.
#
# 8,192 is chosen to match what a cold symbol keeps elsewhere rather than
# picked round: COLD_COLS is 150 seconds of columns, and at the ~50 prints a
# second a busy name sustains, 8,192 prints is about the same 150 seconds. The
# two cold windows therefore describe the same span of history, which is what
# makes "select a cold symbol and see two and a half minutes" true of the tape
# and the heat field alike.
TAPE_COLD = 8192
_AG_FROM = (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)


class TapeView:
    """Sequence view over the tape's ring arrays.

    The tape used to be a deque of (float, int, int, Aggressor) TUPLES.
    Measured: 80 bytes for the tuple plus boxed members is 261 bytes to carry
    17 bytes of data, so a full 60,000-entry tape cost 15.65 MB per symbol -
    63.6% of the process footprint and about 1 GB across 100 symbols.

    The data now lives in four parallel numpy arrays. This class exists so the
    seven places that read `buffer.trades` - the signals scanner, the tape
    widget and window, the monitor, the bookmap's mid-price fallback - keep
    working unchanged: it supports len(), indexing, negative indexing, slicing
    and iteration, and builds a tuple only for the entries someone actually
    asks for. The hot path in _TapeItem._cells bypasses it and reads the arrays
    directly.

    Index 0 is the OLDEST retained trade, matching deque(maxlen=N) exactly.
    """

    __slots__ = ("_b",)

    def __init__(self, buf):
        self._b = buf

    def __len__(self) -> int:
        b = self._b
        return b.trade_count if b.trade_count < b.max_trades else b.max_trades

    def __bool__(self) -> bool:
        return self._b.trade_count > 0

    def _phys(self, i: int) -> int:
        """Logical index (0 = oldest retained) -> physical slot."""
        b = self._b
        return (b._tape_first + i) % b.max_trades

    def __getitem__(self, i):
        n = len(self)
        if isinstance(i, slice):
            return [self[k] for k in range(*i.indices(n))]
        if i < 0:
            i += n
        if not (0 <= i < n):
            raise IndexError(i)
        b = self._b
        p = self._phys(i)
        return (float(b.trade_x[p]), int(b.trade_ti[p]), int(b.trade_sz[p]),
                _AG_FROM[b.trade_ag[p]])

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __reversed__(self):
        for i in range(len(self) - 1, -1, -1):
            yield self[i]


class Column:
    __slots__ = ("bucket", "book", "buy", "sell", "bid_ti", "ask_ti", "vol",
                 "sweeps", "net")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.book: PriceLadder = EMPTY_LADDER   # tick_index -> resting size
        self.buy: dict[int, int] = {}       # tick_index -> aggressive buy vol
        self.sell: dict[int, int] = {}      # tick_index -> aggressive sell vol
        self.bid_ti: int | None = None
        self.ask_ti: int | None = None
        self.vol = 0
        # Signed aggressive volume, maintained INCREMENTALLY as prints arrive.
        # The volume-bar renderer needs it to colour each bar, and it used to
        # recompute it there with two sum() passes over this column's whole
        # price dict on every frame - for every visible column, every book.
        # Kept here it is one addition per trade and it can never be stale.
        self.net = 0
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
        # Column retention depends on whether anything is DRAWING this symbol -
        # see set_hot.
        #
        # STARTS HOT, and the window demotes. Starting cold was tried and the
        # data-truth gate caught it immediately: a bare buffer retained less
        # than the BarSeries beside it, so the bookmap and the footprint
        # disagreed about the same session's volume. Every consumer that
        # constructs a buffer directly - the gates, the tests, any future
        # tool - is entitled to the full retention it has always had, and
        # only the window knows which symbols are on screen. So the default
        # is the safe one and the saving is applied by whoever has the
        # knowledge to apply it.
        self.hot_cols = max_cols
        self.cold_cols = min(COLD_COLS, max_cols)
        self.max_cols = max_cols
        self.cols: dict[int, Column] = {}
        # Kept sorted by bucket, not insertion order: two feeds (trades on L1,
        # books on L2) interleave, and a late arrival must not make latest()
        # report an older column or leave columns() non-monotonic in x - the
        # BBO line and the x-axis follow that order directly.
        self.order: list[int] = []
        # The tape, as four parallel ring arrays rather than a deque of
        # tuples - see TapeView for the measurement that motivated it.
        #
        # GROWN ON DEMAND, not allocated up front. A full ring is 1.02 MB, and
        # allocating it per symbol cost 102 MB across 100 symbols whether those
        # symbols ever printed or not - measured, it was the single largest
        # fixed cost in the process and none of it varied with activity. At the
        # thousands of symbols this has to reach, that alone is a gigabyte of
        # untouched memory.
        #
        # `max_trades` is the CURRENT allocation and `tape_cap` the ceiling.
        # That way every consumer's modular arithmetic - add_trade, TapeView,
        # the renderer's ring walk - keeps using the one attribute it always
        # used and needs no knowledge that the array can grow.
        #
        # Eviction is unchanged: growth happens only while below the ceiling
        # and strictly before the ring would wrap, so the retention seen by a
        # caller is still exactly deque(maxlen=tape_cap).
        # tape_hot is the ceiling for a symbol on screen, tape_cold for one
        # that is not, and tape_cap is whichever applies right now. Starts hot
        # for the same reason the column retention does - see set_hot.
        self.tape_hot = int(max_trades)
        self.tape_cold = min(TAPE_COLD, self.tape_hot)
        self.tape_cap = self.tape_hot
        self.max_trades = min(TAPE_SEED, self.tape_cap)
        self.trade_x = np.empty(self.max_trades, dtype=np.float64)
        self.trade_ti = np.empty(self.max_trades, dtype=np.int32)
        self.trade_sz = np.empty(self.max_trades, dtype=np.int32)
        self.trade_ag = np.empty(self.max_trades, dtype=np.uint8)
        # Physical slot of the OLDEST retained trade. Stays 0 until the ring
        # wraps, then tracks the write head.
        self._tape_first = 0
        self.trades = TapeView(self)
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

    def _grow_tape(self) -> None:
        """Enlarge the tape ring, preserving every retained print.

        Called only when the ring is exactly full and still under its ceiling,
        which is before it has ever wrapped - so `_tape_first` is 0 and the
        arrays are already in logical order. That makes the move a plain
        resize rather than an unwrap, and it is the reason growth is checked
        BEFORE the write rather than after.

        Quadrupling, not doubling: reaching a 60,000 ceiling from the 2,048
        seed is five copies instead of nine, and a copy of an array this size
        is memcpy - the cost is the allocation, so fewer and larger wins.
        """
        old = self.max_trades
        new = min(old * 4, self.tape_cap)
        if new <= old:
            return

        def _ext(a, dtype):
            b = np.empty(new, dtype=dtype)
            b[:old] = a
            return b

        self.trade_x = _ext(self.trade_x, np.float64)
        self.trade_ti = _ext(self.trade_ti, np.int32)
        self.trade_sz = _ext(self.trade_sz, np.int32)
        self.trade_ag = _ext(self.trade_ag, np.uint8)
        self.max_trades = new
        self._tape_first = 0

    def _set_tape_cap(self, cap: int) -> None:
        """Move the tape ceiling, shrinking the ring if it is already past it."""
        cap = max(TAPE_SEED, min(int(cap), self.tape_hot))
        if cap == self.tape_cap:
            return
        self.tape_cap = cap
        if self.max_trades > cap:
            self._shrink_tape(cap)

    def _shrink_tape(self, new_cap: int) -> None:
        """Drop the oldest prints and rehouse the rest in a smaller ring.

        THE RING INVARIANTS HAVE TO SURVIVE THIS, and getting them wrong is
        not a crash - it is bubbles drawn at the wrong prices, which is the
        failure this file cares about most. Two of them:

          * logical index 0 is the OLDEST retained print, and TapeView reads
            it at (_tape_first + i) % max_trades;
          * add_trade writes at trade_count % max_trades, and once the ring is
            full that slot must be exactly where the oldest print sits, so the
            next write evicts it.

        The second is why the survivors cannot simply be packed at slot 0.
        trade_count keeps counting across the shrink - it is a session total
        and deliberately does not reset - so the head lands wherever
        trade_count % new_cap falls, and the data must be laid out around THAT
        rather than the other way round.

        Allocating once per demotion, not per print: a demotion follows a user
        action, so this is rare, and doing it in place would leave the old
        arrays alive anyway.
        """
        old_cap = self.max_trades
        n_have = self.trade_count if self.trade_count < old_cap else old_cap
        keep = min(n_have, new_cap)
        if self.trade_count >= new_cap:
            first = self.trade_count % new_cap     # == the write head: full ring
        else:
            first = 0
        src = (self._tape_first
               + np.arange(n_have - keep, n_have, dtype=np.int64)) % old_cap
        dst = (first + np.arange(keep, dtype=np.int64)) % new_cap

        def _move(a, dtype):
            b = np.empty(new_cap, dtype=dtype)
            if keep:
                b[dst] = a[src]
            return b

        self.trade_x = _move(self.trade_x, np.float64)
        self.trade_ti = _move(self.trade_ti, np.int32)
        self.trade_sz = _move(self.trade_sz, np.int32)
        self.trade_ag = _move(self.trade_ag, np.uint8)
        self.max_trades = new_cap
        self._tape_first = first
        # Every renderer cache keyed on the tape describes prints that are gone.
        self._all_cache = None

    def set_hot(self, hot: bool) -> None:
        """How much per-column history this symbol is worth keeping.

        A symbol nobody is looking at still needs its bars, its latest book
        and its tape - alerts fire on it, the monitor lists it, and selecting
        it must not start from nothing. What it does NOT need is a full
        screen-width of heat history that no window is drawing.

        Measured at 100 symbols and 100 depth per side, the per-column state -
        ladders plus the aggressive buy/sell dicts - reached 116 MB in six
        minutes and was still climbing linearly toward roughly 440 MB at the
        1400-column cap. That is the cost that makes thousands of symbols
        impossible, and almost all of it belongs to symbols off screen.

        Cold symbols keep COLD_COLS instead, which is still a couple of
        minutes of context so that selecting one shows history immediately
        rather than an empty chart that fills in. Promotion is instant;
        demotion evicts on the spot rather than waiting for the next column,
        because the point is to release the memory.
        """
        self._set_tape_cap(self.tape_hot if hot else self.tape_cold)
        want = self.hot_cols if hot else min(self.cold_cols, self.hot_cols)
        if want == self.max_cols:
            return
        self.max_cols = want
        if len(self.order) > want:
            while len(self.order) > want:
                self.cols.pop(self.order.pop(0), None)
                self._evicted += 1
            # Every cached fold now describes columns that are gone.
            self._all_cache = None
            self._agg_cache.clear()
            for agg in self._dirty:
                self._dirty[agg] = None
            self._version += 1

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
        # One shared split (see model.split_size): this used to give the odd
        # share of an UNKNOWN print to SELL while the footprint gave it to BUY,
        # so the two panes disagreed about the same trade.
        buy, sell = split_size(tr.size, tr.aggressor, ti)
        if buy:
            c.buy[ti] = c.buy.get(ti, 0) + buy
        if sell:
            c.sell[ti] = c.sell.get(ti, 0) + sell
        c.vol += tr.size
        c.net += buy - sell
        x = (tr.ts_ms / 1000.0) / self.col_dt
        if self.trade_count >= self.max_trades > 0 and self.max_trades < self.tape_cap:
            self._grow_tape()
        slot = self.trade_count % self.max_trades
        self.trade_x[slot] = x
        self.trade_ti[slot] = ti
        self.trade_sz[slot] = tr.size
        self.trade_ag[slot] = _AG_CODE.get(tr.aggressor, 2)
        self.trade_count += 1
        if self.trade_count > self.max_trades:
            # Wrapped: the oldest retained entry is now the one after the head,
            # which is what deque(maxlen=N) did by dropping from the left.
            self._tape_first = self.trade_count % self.max_trades
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
            g.net += c.net
            # Summed, so an aggregated column reports how many sweeps its whole
            # span was built from - 0 still means "nothing was observed here".
            g.sweeps += c.sweeps
            if c.bid_ti is not None:
                g.bid_ti = c.bid_ti
            if c.ask_ti is not None:
                g.ask_ti = c.ask_ti
        return [groups[b] for b in order]
