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


class Bar:
    """One footprint candle over a fixed market-time window."""

    __slots__ = (
        "start_ts", "tf_s", "open", "high", "low", "close",
        "cells", "volume", "delta", "book", "_dirty", "_cache", "_agg",
        "_ti", "_sell", "_buy",
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
        return {"poc": poc, "tot": dict(zip(t64.tolist(), v.tolist())),
                "ti_v": ti_v, "ti2_v": ti2_v}

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
        adjustable; only ever called for on-screen bars."""
        a = self._analytics()
        tot = a["tot"]
        poc = a["poc"]
        if not tot:
            return None, None
        target = sum(tot.values()) * pct
        idxs = sorted(tot)
        pos = idxs.index(poc)
        lo = hi = pos
        acc = tot[poc]
        n = len(idxs)
        while acc < target and (lo > 0 or hi < n - 1):
            up = tot[idxs[hi + 1]] if hi < n - 1 else -1
            dn = tot[idxs[lo - 1]] if lo > 0 else -1
            if up < 0 and dn < 0:
                break
            if up >= dn:
                hi += 1
                acc += tot[idxs[hi]]
            else:
                lo -= 1
                acc += tot[idxs[lo]]
        return idxs[hi], idxs[lo]

    def imbalances(self, factor: float = 3.0, min_vol: int = 20) -> tuple[set[int], set[int]]:
        """Diagonal imbalances (recomputed on demand — cheap, factor-dependent).

        buy imbalance  : buy_vol at index i dominates sell_vol at i-1
        sell imbalance : sell_vol at index i dominates buy_vol at i+1
        Returns (buy_idx_set, sell_idx_set).
        """
        ti, sell, buy = self.arrays()
        if ti.size == 0:
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
        return set(ti[b_mask].tolist()), set(ti[s_mask].tolist())

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
            return              # too late to place: not charted, not counted

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

        self.bars[-1].add(tr.price, ti, tr.size, tr.aggressor)
        self._stat_trade(tr)
        self._touch(bucket)
        self._version += 1

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
        out: list[Bar] = []
        cur: Bar | None = None
        for base in self.bars[start:]:
            bucket = (base.start_ts // tf_s) * tf_s
            if cur is None or cur.start_ts != bucket:
                if cur is not None:
                    cur.seal()
                    out.append(cur)
                cur = Bar(bucket, tf_s, base.open)
            cur.high = max(cur.high, base.high)
            cur.low = min(cur.low, base.low)
            cur.close = base.close
            # `base` is usually sealed and therefore compact, so read it through
            # arrays() rather than a dict it no longer has. The accumulator is
            # still a dict because it is being built incrementally; seal()
            # compacts it when the group closes.
            bti, bsell, bbuy = base.arrays()
            cells = cur.cells
            for ti, s, b in zip(bti.tolist(), bsell.tolist(), bbuy.tolist()):
                cell = cells.get(ti)
                if cell is None:
                    cells[ti] = [s, b]
                else:
                    cell[0] += s
                    cell[1] += b
            cur.volume += base.volume
            cur.delta += base.delta
            if base.book:
                cur.book = base.book        # most-recent book in the group wins
        if cur is not None:
            out.append(cur)     # live aggregated bar left unsealed
        return out

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
