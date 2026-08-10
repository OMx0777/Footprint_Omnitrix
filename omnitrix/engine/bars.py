"""Market-time bar builder with cached footprint analytics.

Fixes three structural bugs from the old app:

  1. Bars are bucketed by the *trade's* market timestamp (`ts_ms`), never by
     `time.time()` on the receiving side. Replay, premarket and late ticks all
     land in the correct bar.

  2. Per-bar analytics (POC, value area, imbalances, delta) are computed lazily
     and cached. A finished bar computes them once, ever. Only the live bar is
     dirty. The renderer no longer recomputes everything for every candle on
     every frame.

  3. The footprint is keyed by integer *tick index*, so diagonal-imbalance
     neighbours are exact.

Footprint cell layout, per tick index:
    [sell_vol, buy_vol]
      sell_vol -> aggressive sells that hit the bid  (left column)
      buy_vol  -> aggressive buys that lifted the ask (right column)
"""

from __future__ import annotations

import numpy as np

from .model import (Trade, Aggressor, PriceLadder, EMPTY_LADDER,
                    split_size)
from .instruments import Instruments


# How far back per-bar detail is kept.
#
# BOOK_BARS: the L2 snapshot is only ever drawn by the footprint chart's
# heatmap overlay, for bars ON SCREEN. 1,500 base bars is ~4 hours at the 10 s
# base, so scrolling back stays fully painted while the other 10,500 bars stop
# costing 1,880 B each.
#
# COLD_BARS: what a symbol nothing is drawing keeps.
#
# 30 bars is five minutes at the 10 s base. It was 150, which LOOKED like the
# bookmap's cold window of 150 columns and is not the same thing at all: a
# column is one second and a base bar is ten, so the two "cold" windows
# differed by 10x and the bar side was retaining twenty-five minutes of
# footprint detail per symbol.
#
# The 1,000-symbol run caught it. Per-bar state grew linearly for the whole
# thirteen minutes and never began to plateau, because at ~78 bars a symbol
# nothing had yet crossed the 150-bar threshold to be stripped - projected to
# 322 MB at the plateau against 44 MB of ladders and 127 MB of buy/sell dicts,
# making it the largest term in the model by a wide margin.
#
# What five minutes costs: selecting a symbol shows five minutes of per-price
# footprint behind it instead of twenty-five. OHLC, volume and delta are
# untouched for all 12,000 bars either way, so the candles, the delta and
# every statistic still draw over the full history - it is only the per-price
# cells behind the cold window that are gone.
BOOK_BARS = 1500
COLD_BARS = 30


class Bar:
    """One footprint candle over a fixed market-time window."""

    __slots__ = (
        "start_ts", "tf_s", "open", "high", "low", "close",
        "cells", "volume", "delta", "book", "_dirty", "_cache", "_agg",
        "_ti", "_sell", "_buy", "_imb",
    )

    def __init__(self, start_ts: int, tf_s: int, price: float):
        self.start_ts = start_ts          # bar-open epoch seconds (market time)
        self.tf_s = tf_s
        self.open = self.high = self.low = self.close = price
        self.cells: dict[int, list[int]] = {}   # tick_index -> [sell_vol, buy_vol]
        self.volume = 0
        self.delta = 0                    # buy_vol - sell_vol, cumulative in-bar
        self.book: PriceLadder = EMPTY_LADDER  # tick_index -> resting L2 size
        self._dirty = True
        self._cache: dict = {}
        self._agg = None                  # (step, volume, folded Bar)
        # Compact frozen footprint, set by seal(). See `arrays()`.
        self._ti = None
        self._sell = None
        self._buy = None
        self._imb = None                  # (factor, min_vol, buy_set, sell_set)

    # ---- ingestion -------------------------------------------------------
    def add(self, price: float, tick_index: int, size: int, aggressor: Aggressor) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

        if self.cells is None:
            # A sealed bar can still receive a LATE tick - out-of-order arrivals
            # are normal on a live feed and BarSeries folds them back into their
            # own bar. Compaction released the dict, so restore it, take the
            # trade, and let the caller re-seal. Rare by construction: this only
            # fires for a tick that arrived after its bar closed.
            self._thaw()

        cell = self.cells.get(tick_index)
        if cell is None:
            cell = [0, 0]
            self.cells[tick_index] = cell

        buy, sell = split_size(size, aggressor, tick_index)
        cell[0] += sell
        cell[1] += buy
        self.delta += buy - sell

        self.volume += size
        self._dirty = True
        self._imb = None

    def seal(self) -> None:
        """Finish the bar: freeze its analytics AND compact its footprint.

        `cells` is a dict[int, list[int]] because that is the right shape while
        trades are still arriving. It is the wrong shape afterwards: measured at
        154 bytes per price level, 2.26 MB per symbol-hour, which at a
        100-symbol basket over a 16-hour session is the difference between ~7 GB
        and ~3.7 GB - and a process that swaps is a process that lags, which is
        the failure this whole exercise is meant to prevent.

        A sealed bar never changes again, so it can be frozen into three int32
        arrays: 8.1x smaller, and the shape every consumer actually wants.
        The dict is dropped.
        """
        self._analytics()          # force one computation
        self._dirty = False
        self._agg = None           # a folded copy of the pre-seal state is stale
        if self._ti is None and self.cells:
            n = len(self.cells)
            ti = np.fromiter(self.cells.keys(), dtype=np.int32, count=n)
            order = np.argsort(ti, kind="stable")
            vals = np.fromiter(
                (v for c in self.cells.values() for v in c),
                dtype=np.int64, count=n * 2).reshape(n, 2)
            self._ti = ti[order]
            self._sell = vals[order, 0].astype(np.int32)
            self._buy = vals[order, 1].astype(np.int32)
            self.cells = None

    def _thaw(self) -> None:
        """Compact form -> writable dict. The inverse of seal()'s compaction."""
        if self._ti is not None:
            self.cells = {int(t): [int(s), int(b)] for t, s, b in
                          zip(self._ti.tolist(), self._sell.tolist(),
                              self._buy.tolist())}
            self._ti = self._sell = self._buy = None
            self._imb = None
        elif self.cells is None:
            self.cells = {}
        self._agg = None                   # any folded copy is now stale

    # ---- footprint access ------------------------------------------------
    def arrays(self):
        """(tick_index, sell, buy) as int32 arrays, ascending by price.

        The one way to read a bar's footprint. Works for a live bar (built from
        its dict) and a sealed one (returned directly, no copy). Consumers must
        NOT reach for `cells`: it is None once the bar is sealed, and rebuilding
        a dict to read it would undo both the memory win and the speed win - the
        same trap PriceLadder's mapping protocol turned out to be.
        """
        if self._ti is not None:
            return self._ti, self._sell, self._buy
        cells = self.cells
        if not cells:
            return (np.empty(0, np.int32), np.empty(0, np.int32),
                    np.empty(0, np.int32))
        n = len(cells)
        ti = np.fromiter(cells.keys(), dtype=np.int32, count=n)
        order = np.argsort(ti, kind="stable")
        vals = np.fromiter((v for c in cells.values() for v in c),
                           dtype=np.int64, count=n * 2).reshape(n, 2)
        return (ti[order], vals[order, 0].astype(np.int32),
                vals[order, 1].astype(np.int32))

    def add_cells_only(self, tick_index: int, size: int,
                       aggressor: Aggressor) -> None:
        """Put a trade into the footprint WITHOUT touching volume or delta.

        For replaying history into a bar that already counted it. `add()`
        maintains volume and delta as it goes, so routing a replayed trade
        through it would add the same size twice - to the bar, and through
        BarSeries._stat_trade to the session totals as well. This is the
        cells, and only the cells.

        The distinction is not cosmetic: volume and delta are what the candle,
        the CVD and the monitor read, and they were already correct before the
        footprint was ever stripped.
        """
        if self.cells is None:
            self._thaw()
        cell = self.cells.get(tick_index)
        if cell is None:
            cell = [0, 0]
            self.cells[tick_index] = cell
        buy, sell = split_size(size, aggressor, tick_index)
        cell[0] += sell
        cell[1] += buy
        self._dirty = True
        self._imb = None

    def drop_book(self) -> None:
        """Release this bar's L2 snapshot, keeping everything else.

        MEASURED THE BIGGEST SINGLE ITEM IN A SEALED BAR: 1,880 B of 3,501 B,
        53.7%, and no two bars share one - 288 distinct ladders across 288
        sealed bars. At the 12,000-bar cap that is 22.6 GB across a thousand
        symbols for data whose only consumer is the footprint chart's heatmap
        overlay, which draws the bars ON SCREEN.

        Nothing else in a bar depends on it: OHLC, volume, delta, the
        footprint arrays and the cached POC/value-area are all independent.
        """
        self.book = EMPTY_LADDER

    def drop_dense(self) -> None:
        """Release the per-price footprint too. OHLCV and delta survive.

        This is the one that loses information a user could otherwise see -
        the bar keeps its candle, its volume and its delta, but its per-price
        cells are gone and cannot come back. It is therefore applied ONLY to
        bars far behind the screen on symbols nothing is drawing, and it is
        NOT undone by promotion: a symbol brought back to the foreground has
        full detail from that moment on and stripped bars behind it.

        arrays() already returns empty arrays when both forms are absent, and
        _analytics() was computed and cached at seal(), so a stripped bar
        answers every question it could answer before except "what traded at
        each price".
        """
        self.drop_book()
        self.cells = None
        self._ti = None
        self._sell = None
        self._buy = None
        self._imb = None
        self._agg = None

    def n_levels(self) -> int:
        return int(self._ti.size) if self._ti is not None else len(self.cells or ())

    def has_cells(self) -> bool:
        return self.n_levels() > 0

    # ---- cached analytics ------------------------------------------------
    def _analytics(self) -> dict:
        if self._dirty or not self._cache:
            self._cache = self._compute()
            # a live bar stays dirty until sealed; finished bars set _dirty False
        return self._cache

    def _compute(self) -> dict:
        ti, sell, buy = self.arrays()
        if ti.size == 0:
            return {"poc": None, "tot": {}, "ti_v": 0.0, "ti2_v": 0.0}
        t64 = ti.astype(np.int64)
        v = sell.astype(np.int64) + buy.astype(np.int64)
        # Volume-weighted tick-index moments, cached alongside the POC. VWAP and
        # its standard-deviation bands are then an O(bars) running sum instead of
        # re-walking every cell of every bar on every frame.
        ti_v = float((t64 * v).sum())
        ti2_v = float((t64 * t64 * v).sum())
        # Ties go to the LOWEST price. This is a deliberate change from
        # `max(tot, key=tot.get)` over the old dict, which resolved a tie by
        # INSERTION order - i.e. by the order trades happened to arrive. The
        # same bar rebuilt from a replay could therefore report a different POC
        # than it did live, and nothing downstream could tell. Ties are common
        # on round sizes (measured: 14 of 400 random bars), so this is worth
        # pinning down rather than leaving to arrival order.
        poc = int(ti[int(v.argmax())])
        # NO "tot" DICT. It used to cache {tick_index: volume} here, which is
        # the same data _ti/_sell/_buy already hold - a second copy, in the
        # exact boxed-dict shape seal() exists to get rid of.
        #
        # Measured with a deep sizer (sys.getsizeof does NOT follow a dict's
        # values, which is how it was previously reported as 184 B): 7,098 B a
        # bar, 68% of a sealed bar and nearly four times the L2 book. Across
        # 1,000 symbols at the 12,000-bar cap, 85 GB.
        #
        # Its only consumer was value_area(), which now walks the arrays. They
        # are sorted ascending by seal(), which is what the dict version got
        # from sorted(tot) - so the walk is the same walk, without the copy.
        return {"poc": poc, "ti_v": ti_v, "ti2_v": ti2_v}

    @property
    def ti_moments(self) -> tuple[float, float]:
        """(Σ tick_index·vol, Σ tick_index²·vol) — multiply by tick for prices."""
        a = self._analytics()
        return a["ti_v"], a["ti2_v"]

    @property
    def poc(self) -> int | None:
        return self._analytics()["poc"]

    def value_area(self, pct: float = 0.70) -> tuple[int | None, int | None]:
        """(VAH index, VAL index) enclosing `pct` of volume, expanded from the
        POC toward the heavier adjacent side. Computed on demand so the pct is
        adjustable; only ever called for on-screen bars.

        Walks the footprint ARRAYS. It used to walk a {tick_index: volume}
        dict cached in _cache["tot"] - the same data a second time, boxed, at
        7,098 B a bar. The arrays are sorted ascending by seal(), which is
        exactly what sorted(tot) produced, so this is the same traversal in
        the same order and returns the same pair.
        """
        ti, sell, buy = self.arrays()
        if ti.size == 0:
            return None, None
        poc = self._analytics()["poc"]
        if poc is None:
            return None, None
        v = (sell.astype(np.int64) + buy.astype(np.int64))
        n = int(ti.size)
        pos = int(np.searchsorted(ti, poc))
        if pos >= n or int(ti[pos]) != poc:
            # A stripped bar keeps its cached POC but not its cells; there is
            # no area to report and inventing one would be worse than saying so.
            return None, None
        target = float(v.sum()) * pct
        vl = v.tolist()                      # one conversion, then plain ints
        lo = hi = pos
        acc = vl[pos]
        while acc < target and (lo > 0 or hi < n - 1):
            up = vl[hi + 1] if hi < n - 1 else -1
            dn = vl[lo - 1] if lo > 0 else -1
            if up < 0 and dn < 0:
                break
            if up >= dn:
                hi += 1
                acc += vl[hi]
            else:
                lo -= 1
                acc += vl[lo]
        return int(ti[hi]), int(ti[lo])

    def imbalances(self, factor: float = 3.0, min_vol: int = 20) -> tuple[set[int], set[int]]:
        """Diagonal imbalances (recomputed on demand — cheap, factor-dependent).

        buy imbalance  : buy_vol at index i dominates sell_vol at i-1
        sell imbalance : sell_vol at index i dominates buy_vol at i+1
        Returns (buy_idx_set, sell_idx_set).
        """
        # Cached per (factor, min_vol). The renderer asks every visible bar for
        # this on EVERY frame - measured at 150 calls and ~14 ms a frame on a
        # normal chart - and a sealed bar's answer can never change. `add()`
        # clears it, so the live bar stays correct.
        c = self._imb
        if c is not None and c[0] == factor and c[1] == min_vol:
            return c[2], c[3]

        ti, sell, buy = self.arrays()
        if ti.size == 0:
            self._imb = (factor, min_vol, set(), set())
            return set(), set()

        def neighbour(offset: int, src):
            """`src` value at ti+offset, or 0 where that level did not trade."""
            want = ti + offset
            idx = np.searchsorted(ti, want)
            safe = np.clip(idx, 0, ti.size - 1)
            hit = (idx < ti.size) & (ti[safe] == want)
            return np.where(hit, src[safe], 0).astype(np.int64)

        buy64 = buy.astype(np.int64)
        sell64 = sell.astype(np.int64)
        dn_sell = neighbour(-1, sell)     # sell one level BELOW
        up_buy = neighbour(+1, buy)       # buy one level ABOVE
        # `dn_sell == 0` means the diagonal neighbour never traded, which counts
        # as an imbalance - preserved exactly from the dict version.
        b_mask = (buy64 >= min_vol) & ((dn_sell == 0) | (buy64 >= factor * dn_sell))
        s_mask = (sell64 >= min_vol) & ((up_buy == 0) | (sell64 >= factor * up_buy))
        out = (set(ti[b_mask].tolist()), set(ti[s_mask].tolist()))
        self._imb = (factor, min_vol, out[0], out[1])
        return out

    def aggregated(self, step: int) -> "Bar":
        """This bar's footprint folded onto a coarser price grid.

        At a 1-hour candle a penny-ticked name puts hundreds of rows in one
        bar, so the cells collapse into an unreadable stripe. Folding `step`
        ticks into one row and SUMMING their volume makes each row thick enough
        to carry its numbers, and a level spread across several cents shows its
        true weight instead of being split into slivers.

        Returns a real Bar keyed by BUCKET index (ti // step), so every existing
        analytic - POC, value area, and in particular the diagonal imbalance
        test - runs unchanged on the grid actually being drawn. That is the
        whole reason this happens here rather than in the renderer: comparing
        buy at bucket i against sell at bucket i-1 is only meaningful once the
        folding is done, and aggregating after the imbalance test would mark
        cells the viewer cannot see.

        Floor division keeps bucket edges absolute, so a row does not slide as
        price moves - the same reason the Bookmap heatmap floors its rows.

        `step <= 1` returns self, so the default path allocates nothing.
        """
        if step <= 1:
            return self
        c = self._agg
        # `volume` doubles as the version: it changes on every trade, so a live
        # bar refolds and a sealed one never does.
        if c is not None and c[0] == step and c[1] == self.volume:
            return c[2]

        agg = Bar(self.start_ts, self.tf_s, self.open)
        agg.high, agg.low, agg.close = self.high, self.low, self.close
        agg.volume, agg.delta, agg.book = self.volume, self.delta, self.book
        ti, sell, buy = self.arrays()
        if ti.size:
            # Floor-divide onto the coarse grid, then sum each bucket in one
            # pass. np.unique returns the buckets already ascending, which is
            # the order `arrays()` promises.
            b = np.floor_divide(ti.astype(np.int64), step)
            keys, inv = np.unique(b, return_inverse=True)
            agg._ti = keys.astype(np.int32)
            agg._sell = np.bincount(inv, weights=sell.astype(np.float64),
                                    minlength=keys.size).astype(np.int32)
            agg._buy = np.bincount(inv, weights=buy.astype(np.float64),
                                   minlength=keys.size).astype(np.int32)
            agg.cells = None
        agg.seal()                 # analytics computed once; we refold on change
        self._agg = (step, self.volume, agg)
        return agg

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open


class BarSeries:
    """All bars for one symbol, built at a base timeframe with memoized
    aggregation to higher timeframes."""

    def __init__(self, symbol: str, instruments: Instruments,
                 base_tf_s: int = 10, max_bars: int = 12000):
        self.symbol = symbol
        self.instruments = instruments
        # Starts HOT and the window demotes - see set_hot. The data-truth gate
        # rejected the other way round for BookmapBuffer and the reasoning is
        # identical: a bare series must retain what every consumer expects.
        self._hot = True
        self.base_tf_s = base_tf_s
        self.max_bars = max_bars
        self.bars: list[Bar] = []
        self._bar_by_ts: dict[int, Bar] = {}    # start_ts -> bar, for O(1) book routing
        self._version = 0                       # bumps whenever base bars change
        self._agg_cache: dict[int, tuple[int, list[Bar], int]] = {}
        # Per-timeframe dirty watermark: the earliest base-bar start_ts modified
        # since that timeframe's aggregate was last built. Lets `view` rebuild
        # only the affected tail instead of re-folding the whole session.
        self._tf_dirty: dict[int, int] = {}
        self._evicted = 0          # bumped when max_bars drops a bar off the front
        # Trades that arrived too late to place at all - see add_trade. A bulk
        # loader checks these are zero; live they are a health signal.
        self.dropped_late = 0
        self.dropped_late_vol = 0
        self._ov_cache: dict[int, tuple[tuple, tuple]] = {}

        # ---- O(1) running session stats -------------------------------------
        # The market monitor needs last/open/high/low/volume/delta for *every*
        # tracked symbol twice a second. Deriving them by calling view() per
        # symbol re-aggregated the whole history on every refresh (view()'s cache
        # is keyed on _version, which every trade invalidates), so a 100-symbol
        # basket rebuilt 100 histories per tick on the GUI thread. These are
        # maintained incrementally instead.
        #
        # They cover every trade ingested since construction and deliberately do
        # NOT shrink when old bars are evicted by `max_bars` — a session volume
        # that falls as history scrolls off would be worse than useless.
        self.sess_open: float | None = None
        self.sess_high: float = 0.0
        self.sess_low: float = 0.0
        self.sess_last: float = 0.0
        self.sess_volume: int = 0
        self.sess_delta: int = 0
        self.sess_trades: int = 0

    def rebuild_footprint(self, trades) -> dict:
        """Refill stripped bars' per-price cells from replayed history.

        A cold symbol has its footprint released behind COLD_BARS (see
        set_hot); OHLC, volume and delta survive, the cells do not. This puts
        the cells back from a replay of the same trades.

        IT DELIBERATELY DOES NOT GO THROUGH add_trade(). That path maintains
        the bar's volume and delta AND feeds _stat_trade, and every one of
        those figures already counted these trades when they arrived live. A
        replay through the normal path would double the session volume, the
        session delta and the bar's own volume - permanently, with nothing to
        indicate it. So this writes cells and nothing else.

        NO BAR IS CREATED. A trade whose bar has been evicted, or which
        belongs to a bar that still has its cells, is counted and skipped:
        inventing a bar from replayed data would put volume on the chart that
        the session totals do not know about, which is the same falsehood in
        the other direction.

        Returns a report rather than nothing, because the useful question
        afterwards is whether the refilled bars now agree with themselves -
        `mismatched` counts bars whose cells do not sum to the volume they
        have carried all along, which means the replay was not the same data.
        """
        filled = {}
        applied = skipped = 0
        for tr in trades:
            bucket = int(tr.ts_ms // 1000 // self.base_tf_s) * self.base_tf_s
            bar = self._bar_by_ts.get(bucket)
            if bar is None or bar is self.bars[-1]:
                # Evicted, or the live bar - which owns its own cells and must
                # not be written behind add_trade's back.
                skipped += 1
                continue
            if bar.start_ts not in filled and bar.n_levels() > 0:
                skipped += 1
                continue                     # this bar still has its footprint
            filled[bar.start_ts] = bar
            ti = self.instruments.to_index(self.symbol, tr.price)
            bar.add_cells_only(ti, tr.size, tr.aggressor)
            applied += 1

        # A BAR GETS ITS WHOLE FOOTPRINT OR NONE OF IT.
        #
        # A fetch window cuts across bars at its edges, so a boundary bar
        # receives only the trades that fell inside the window and its cells
        # then sum to less than the volume it has carried all along. Left in
        # place that is the worst kind of wrong: a footprint that looks
        # complete, next to a candle that says a different number, with
        # nothing to indicate which to believe.
        #
        # Observed immediately - the very first UI run reported one such bar -
        # so the partial fill is reverted and the bar stays honestly empty
        # until a window that covers it fully comes along.
        partial = 0
        for bar in list(filled.values()):
            _t, sell, buy = bar.arrays()
            if int(sell.sum()) + int(buy.sum()) != bar.volume:
                bar.drop_dense()
                del filled[bar.start_ts]
                partial += 1
                continue
            bar.seal()                       # recompact and refresh analytics
        mismatched = partial
        if filled:
            self._agg_cache.clear()
            self._tf_dirty.clear()
            self._version += 1
        return {"bars": len(filled), "applied": applied,
                "skipped": skipped, "partial": partial,
                "mismatched": mismatched}

    def prepend_history(self, other: "BarSeries") -> dict:
        """Splice in the part of `other` that is strictly OLDER than this one.

        WHY THIS EXISTS. The startup backfill built a complete session off the
        GUI thread and then refused to install it whenever the live slot had
        already counted bars - which, on a multicast feed carrying the whole
        basket, is every symbol within seconds of launch. So history loaded for
        the one symbol on screen at startup and was discarded for every symbol
        selected afterwards: the chart began at the moment the app did.

        Replacing the live series was rightly refused, because it holds volume
        and delta the replay knows nothing about. Merging does not have that
        problem, and the reason is the seam: only bars STRICTLY OLDER than the
        oldest live bar are taken. Old and new never describe the same bucket,
        so nothing can be counted twice - the one failure mode that would be
        silent and unrecoverable.

        The partially-live boundary bar is deliberately NOT merged. The replay
        holds only the part of it that arrived before the client connected, and
        adding that to a bar the live stream is still filling would produce a
        bar that is neither. One bar of lost detail at the seam is the honest
        price; a wrong bar is not.

        Returns a report, so a caller can log what was actually gained rather
        than assume.
        """
        if not other.bars:
            return {"added": 0, "reason": "replay empty"}
        if not self.bars:
            # Nothing live yet - take the lot, seam included.
            cut = None
        else:
            cut = self.bars[0].start_ts
        add = [b for b in other.bars if cut is None or b.start_ts < cut]
        if not add:
            return {"added": 0, "reason": "replay has nothing older"}

        self.bars[:0] = add
        for b in add:
            self._bar_by_ts[b.start_ts] = b
        # Honour the cap from the FRONT, which is where the oldest are.
        if len(self.bars) > self.max_bars:
            drop = len(self.bars) - self.max_bars
            for b in self.bars[:drop]:
                self._bar_by_ts.pop(b.start_ts, None)
            del self.bars[:drop]
            self._evicted += drop

        # Session figures gain the prepended flow. These are what the monitor
        # and the stats panel report, and a session that starts mid-morning
        # because the app did is exactly the wrongness this fixes.
        vol = sum(b.volume for b in add)
        dlt = sum(b.delta for b in add)
        self.sess_volume += vol
        self.sess_delta += dlt
        hi = max(b.high for b in add)
        lo = min(b.low for b in add)
        self.sess_high = max(self.sess_high, hi) if self.sess_open is not None else hi
        self.sess_low = min(self.sess_low, lo) if self.sess_open is not None else lo
        # The session OPEN is now the oldest bar's open, not whatever price
        # happened to print when the client attached.
        self.sess_open = self.bars[0].open
        if self.sess_last == 0.0:
            self.sess_last = self.bars[-1].close

        self._agg_cache.clear()
        self._tf_dirty.clear()
        self._version += 1
        return {"added": len(add), "volume": vol, "delta": dlt,
                "from_ts": add[0].start_ts, "to_ts": add[-1].start_ts}

    def _stat_trade(self, tr: Trade) -> None:
        if self.sess_open is None:
            self.sess_open = self.sess_high = self.sess_low = tr.price
        elif tr.price > self.sess_high:
            self.sess_high = tr.price
        elif tr.price < self.sess_low:
            self.sess_low = tr.price
        self.sess_last = tr.price
        self.sess_volume += tr.size
        self.sess_trades += 1
        # Same split as Bar.add, via the one shared definition - deriving it
        # separately here is exactly how the monitor's session delta drifted
        # from the charted delta.
        b, s = split_size(tr.size, tr.aggressor,
                          self.instruments.to_index(self.symbol, tr.price))
        self.sess_delta += b - s

    def add_trade(self, tr: Trade) -> None:
        bucket = (tr.ts_ms // 1000 // self.base_tf_s) * self.base_tf_s
        ti = self.instruments.to_index(self.symbol, tr.price)

        if self.bars and bucket < self.bars[-1].start_ts:
            # A tick that arrives out of order must never append a bar behind
            # the last one - `bars` is drawn by index, so a non-monotonic
            # start_ts sends the x-axis backwards. Fold it into its own bar if
            # that is still in the window, else drop it as too late to place.
            late = self._bar_by_ts.get(bucket)
            if late is not None:
                late.add(tr.price, ti, tr.size, tr.aggressor)
                late.seal()     # re-seal: add() dirtied an already-finished bar
                self._stat_trade(tr)
                self._touch(bucket)
                self._version += 1
            else:
                # NOT CHARTED, NOT COUNTED - and now at least recorded.
                #
                # `bars` is drawn by index, so a bar cannot be inserted behind
                # the last one without sending the x-axis backwards; a trade
                # whose bucket was never created therefore has nowhere to go.
                # Live that is one stale tick. In a BULK REPLAY it was 0.08% of
                # a session's volume - measured - vanishing with nothing to say
                # so, which is the kind of quiet shortfall this codebase treats
                # as false data.
                #
                # The cure is to sort a bulk payload by timestamp before
                # ingesting it, which removes the case entirely. This counter
                # is how a loader checks that it did.
                self.dropped_late += 1
                self.dropped_late_vol += tr.size
            return

        if not self.bars or self.bars[-1].start_ts != bucket:
            if self.bars:
                self.bars[-1].seal()            # finalize the previous bar
            bar = Bar(bucket, self.base_tf_s, tr.price)
            self.bars.append(bar)
            self._bar_by_ts[bucket] = bar
            if len(self.bars) > self.max_bars:
                old = self.bars.pop(0)
                self._bar_by_ts.pop(old.start_ts, None)
                self._evicted += 1        # invalidates every cached prefix
            self._prune_dense()

        self.bars[-1].add(tr.price, ti, tr.size, tr.aggressor)
        self._stat_trade(tr)
        self._touch(bucket)
        self._version += 1

    def _prune_dense(self) -> None:
        """Release detail from the bar that just fell out of the keep window.

        O(1) per sealed bar, deliberately. Sweeping the list would be O(bars)
        on every bar close, which at the 12,000-bar cap is exactly the kind of
        work-that-grows-with-uptime this codebase keeps removing.
        """
        bars = self.bars
        n = len(bars)
        i = n - 1 - (BOOK_BARS if self._hot else COLD_BARS)
        if i >= 0:
            bars[i].drop_book()
        if not self._hot:
            j = n - 1 - COLD_BARS
            if j >= 0:
                bars[j].drop_dense()

    def set_hot(self, hot: bool) -> bool:
        """How much per-BAR detail this symbol is worth keeping.

        Returns whether it actually STRIPPED anything, so the caller can
        budget on work done rather than on calls made.

        The counterpart to BookmapBuffer.set_hot, and the larger of the two.
        Measured per sealed bar at 100 depth a side: 3,501 B, of which the L2
        book is 53.7% and the footprint arrays 36.3%. At the 12,000-bar cap
        that is 42 MB a symbol - 4.2 GB across a hundred, 42 GB across a
        thousand, which is the wall that stops the universe growing.

        Demotion strips bars behind the cold window and IS NOT UNDONE by
        promotion. A symbol brought back to the foreground has full detail
        from that moment forward and stripped bars behind it: OHLC, volume and
        delta intact, per-price cells gone. That is a real loss and the reason
        the default is hot and only the window demotes - the same discipline
        the data-truth gate forced on BookmapBuffer.
        """
        if hot == self._hot:
            return False
        self._hot = hot
        if hot:
            return False                # nothing to restore; detail accrues again
        bars = self.bars
        cut = len(bars) - COLD_BARS
        if cut <= 0:
            return False                # nothing behind the window yet: free
        for k in range(cut):
            bars[k].drop_dense()
        # Folded aggregations describe bars that no longer carry cells.
        self._agg_cache.clear()
        self._tf_dirty.clear()
        self._version += 1
        # MUST report the work. This loop is the single most expensive thing
        # demotion does - up to BOOK_BARS bars stripped in one call - and
        # _sync_hot budgets MAX_DEMOTIONS_PER_SYNC per frame by counting what
        # set_hot says it released. Falling off the end returned None, so the
        # expensive path scored as free and a pass could strip an unbounded
        # number of symbols in one frame. That is the exact shape of every
        # freeze in this app: unbounded work on the frame thread.
        return True

    def add_book(self, bk) -> None:
        """Attach an L2 resting-liquidity snapshot to the bar whose time window
        contains it (not merely the live bar) so backfilled books route right."""
        bucket = (bk.ts_ms // 1000 // self.base_tf_s) * self.base_tf_s
        bar = self._bar_by_ts.get(bucket)
        if bar is None:
            return
        # Compact storage, for the same reason as BookmapBuffer - and the cost
        # here is larger. `max_bars` is 12,000, and at a 10-second base bar
        # every bar receives a sweep, so a dict-per-bar is ~283 MB per symbol
        # at the cap against ~28 MB as int32 arrays.
        #
        # Shares the snapshot's cached ladder, so on the normal path (bookmap
        # first, then here) this costs a dict lookup rather than a second full
        # conversion of the same book.
        bar.book = bk.ladder(self.instruments.tick(self.symbol))
        self._touch(bar.start_ts)
        self._version += 1

    # ---- higher-timeframe view (memoized) --------------------------------
    def view(self, tf_s: int) -> list[Bar]:
        if tf_s <= self.base_tf_s:
            return self.bars

        cached = self._agg_cache.get(tf_s)
        if cached and cached[0] == self._version:
            return cached[1]

        # Rebuild only the dirty tail.
        #
        # The cache was keyed on `_version`, which increments on EVERY trade, so
        # a live feed invalidated it constantly and each redraw re-folded the
        # entire session. That cost grows linearly with uptime - measured 0.57 ms
        # at one hour and 4.77 ms at seven, times five call sites per frame -
        # which is exactly the "gets laggy after a few hours" report. Trades land
        # in recent bars, so almost always only the last group changed.
        agg = None
        if cached is not None and cached[2] == self._evicted:
            dirty = self._tf_dirty.get(tf_s)
            if dirty is not None:
                bucket = (dirty // tf_s) * tf_s
                prev = cached[1]
                k = len(prev)
                while k > 0 and prev[k - 1].start_ts >= bucket:
                    k -= 1
                j = len(self.bars)
                while j > 0 and self.bars[j - 1].start_ts >= bucket:
                    j -= 1
                agg = prev[:k] + self._aggregate(tf_s, j)
        if agg is None:
            agg = self._aggregate(tf_s)

        self._agg_cache[tf_s] = (self._version, agg, self._evicted)
        self._tf_dirty[tf_s] = None       # this timeframe is now clean
        return agg

    def _touch(self, start_ts: int) -> None:
        """Record that the base bar at `start_ts` changed.

        Kept per cached timeframe rather than as one global watermark: two
        timeframes are refreshed at different moments, and a single shared
        watermark cleared by whichever read first would leave the other
        rebuilding from a point after its own stale region.
        """
        d = self._tf_dirty
        for tf, cur in d.items():
            if cur is None or start_ts < cur:
                d[tf] = start_ts

    def _aggregate(self, tf_s: int, start: int = 0) -> list[Bar]:
        """Fold base bars into `tf_s` groups.

        VECTORISED PER GROUP. This walked every price level of every base bar
        through a Python dict - about thirty operations a bar - so a cold fold
        cost 35 ms over a 6.5-hour session and grew linearly with it. That is
        paid whenever a symbol or a timeframe is selected for the first time,
        which is the jank a user feels on the very actions they take most:
        measured 87-107 ms on a symbol switch at 200 symbols.

        A sealed bar already holds its footprint as sorted int32 arrays, so a
        group can be concatenated, sorted once and summed with reduceat -
        numpy doing in one pass what the dict did per level.
        """
        bars = self.bars
        n = len(bars)
        out: list[Bar] = []
        i = start
        while i < n:
            bucket = (bars[i].start_ts // tf_s) * tf_s
            j = i
            while j < n and (bars[j].start_ts // tf_s) * tf_s == bucket:
                j += 1
            out.append(self._fold_group(bucket, tf_s, bars, i, j))
            i = j
        return out

    @staticmethod
    def _fold_group(bucket: int, tf_s: int, bars: list, i: int, j: int) -> Bar:
        """One aggregated bar from bars[i:j]."""
        first = bars[i]
        b = Bar(bucket, tf_s, first.open)
        hi = first.high
        lo = first.low
        vol = 0
        dlt = 0
        book = EMPTY_LADDER
        tis = []
        sells = []
        buys = []
        for k in range(i, j):
            x = bars[k]
            if x.high > hi:
                hi = x.high
            if x.low < lo:
                lo = x.low
            vol += x.volume
            dlt += x.delta
            if x.book:
                book = x.book              # most-recent book in the group wins
            t, sv, bv = x.arrays()
            if t.size:
                tis.append(t)
                sells.append(sv)
                buys.append(bv)
        b.high, b.low, b.close = hi, lo, bars[j - 1].close
        b.volume, b.delta, b.book = vol, dlt, book
        if tis:
            ti = tis[0] if len(tis) == 1 else np.concatenate(tis)
            sv = sells[0] if len(sells) == 1 else np.concatenate(sells)
            bv = buys[0] if len(buys) == 1 else np.concatenate(buys)
            if len(tis) > 1:
                order = np.argsort(ti, kind="stable")
                ti = ti[order]
                sv = sv[order]
                bv = bv[order]
            cuts = np.flatnonzero(np.diff(ti))
            starts = np.empty(cuts.size + 1, dtype=np.intp)
            starts[0] = 0
            starts[1:] = cuts + 1
            b._ti = ti[starts].astype(np.int32)
            b._sell = np.add.reduceat(sv.astype(np.int64),
                                      starts).astype(np.int32)
            b._buy = np.add.reduceat(bv.astype(np.int64),
                                     starts).astype(np.int32)
        b.cells = None
        # Analytics stay LAZY. The old path sealed every group eagerly, which
        # computed POC and value area for bars that may never be drawn; a
        # folded bar answers them on first access exactly as a sealed one does.
        b._dirty = True
        return b

    def cvd(self, tf_s: int) -> list[float]:
        """Cumulative volume delta series aligned to the view bars."""
        acc = 0
        series = []
        for b in self.view(tf_s):
            acc += b.delta
            series.append(acc)
        return series

    def overlays(self, tf_s: int, tick: float
                 ) -> tuple[list[float], list[float], list[float]]:
        """(vwap, stdev, cvd) aligned to `view(tf_s)`, memoized per version.

        Uses each bar's cached volume-weighted moments, so this is one pass over
        the bars rather than a walk of every price cell in the history. The old
        renderer recomputed the full double sum on every frame — at the 10s
        timeframe that is ~12 000 bars x their cells, thirty times a second.
        """
        cached = self._ov_cache.get(tf_s)
        if cached and cached[0] == (self._version, tick):
            return cached[1]

        bars = self.view(tf_s)
        vwap: list[float] = []
        std: list[float] = []
        cvd: list[float] = []
        cum_v = cum_pv = cum_pv2 = 0.0
        acc_delta = 0
        for b in bars:
            ti_v, ti2_v = b.ti_moments
            cum_v += b.volume
            cum_pv += ti_v * tick
            cum_pv2 += ti2_v * tick * tick
            if cum_v > 0:
                m = cum_pv / cum_v
                vwap.append(m)
                std.append(max(0.0, cum_pv2 / cum_v - m * m) ** 0.5)
            else:                       # a bar with no prints yet
                vwap.append(bars[0].open if bars else 0.0)
                std.append(0.0)
            acc_delta += b.delta
            cvd.append(acc_delta)

        out = (vwap, std, cvd)
        self._ov_cache[tf_s] = ((self._version, tick), out)
        return out
