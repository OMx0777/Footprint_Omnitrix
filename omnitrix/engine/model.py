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

from dataclasses import dataclass, field
from enum import Enum

import numpy as np


class Aggressor(str, Enum):
    BUY = "buy"        # lifted the ask
    SELL = "sell"      # hit the bid
    UNKNOWN = "unknown"


# A price this far from anything real is not a price. The bound is generous on
# purpose - the job is to reject the impossible, not to have an opinion about
# what an instrument may cost - but it MUST hold, because a tick index is an
# int32 in every array this app stores and a price of 1e12 overflows it:
#
#     OverflowError: Python int too large to convert to C long
#
# raised inside Bar.seal, which then poisons every later paint of that bar. NaN
# is worse than an overflow: it does not raise, it propagates, and one NaN high
# makes the whole price axis unusable with nothing on screen to say why.
MAX_PRICE = 1e9
MAX_SIZE = 1 << 40


def sane_trade(tr) -> bool:
    """Is this print safe to ingest?

    Called at the boundary where external data enters - the drain and the
    history loader. Two float comparisons and a range check, about 50 ns
    against the tens of microseconds an event costs downstream.

    NOT a filter for "unlikely" values. A wide stop or a fat-finger print is
    real data and must be charted. This rejects only what cannot be charted at
    all: non-finite prices, non-positive prices, sizes that cannot be summed
    into an int64, and prices whose tick index would not fit the int32 arrays
    the whole storage layer is built on.
    """
    p = tr.price
    # `p != p` is the NaN test; NaN fails every comparison, so an ordinary
    # range check would let it through.
    if p != p or p <= 0.0 or p > MAX_PRICE:
        return False
    sz = tr.size
    if sz < 0 or sz > MAX_SIZE:
        return False
    return bool(tr.symbol)


def split_size(size: int, aggressor: Aggressor, tick_index: int) -> tuple[int, int]:
    """One print -> (buy_volume, sell_volume). THE definition of the split.

    Every consumer must route an UNKNOWN print through here. Four places used
    to decide this independently and three of them were wrong in ways that all
    leaned the same direction - green:

      * the bubble / pie / split-bar overlay counted UNKNOWN as 100% BUY;
      * the tape reader's prints and CVD did the same;
      * the footprint gave the odd share of an odd split to BUY while the
        bookmap gave it to SELL, so the two panes disagreed about the same
        trade.

    An UNKNOWN print carries no directional information, so attributing all of
    it to either side is fabricating data. It is split evenly.

    The odd share of an odd size cannot be split in integers, so it alternates
    on the PRICE's parity. That matters: a fixed side accumulates a real bias
    (a 1-lot unclassified print would count as a whole buy, every time), while
    keying on the trade itself - not on a per-consumer counter - keeps every
    consumer in agreement about the same print. Over any price walk the
    residual averages to zero instead of accumulating.

    buy + sell == size always, which the session-figure derivations rely on.
    """
    if aggressor is Aggressor.BUY:
        return size, 0
    if aggressor is Aggressor.SELL:
        return 0, size
    buy = size // 2
    if (size & 1) and (tick_index & 1):
        buy += 1
    return buy, size - buy


def split_sizes(sizes, aggr_codes, tick_indices):
    """Vectorised split_size over numpy arrays. Returns (buys, sells).

    `aggr_codes` are the uint8 codes the tape stores: 0 BUY, 1 SELL, 2 UNKNOWN.

    THIS LIVES HERE, DIRECTLY BELOW split_size, ON PURPOSE. The single worst
    bug this codebase has had was a second, disagreeing implementation of the
    buy/sell split - the renderer counted every UNKNOWN print as 100% buying
    while the footprint halved it, and the chart read green on a balanced tape.
    Two definitions in two files is how that happens. Keeping them adjacent
    means a change to the rule is visibly a change to BOTH, and
    tests/split_vec.py asserts they agree across the whole input space.

    It exists because the bookmap's cache rebuild folds up to 60,000 prints in
    one go, and doing that a print at a time in Python took 110 ms - long
    enough to freeze the UI once the tape filled.
    """
    import numpy as np
    sizes = np.asarray(sizes, dtype=np.int64)
    codes = np.asarray(aggr_codes, dtype=np.uint8)
    tis = np.asarray(tick_indices, dtype=np.int64)

    buys = np.zeros(sizes.shape, dtype=np.int64)
    sells = np.zeros(sizes.shape, dtype=np.int64)

    is_buy = codes == 0
    buys[is_buy] = sizes[is_buy]
    is_sell = codes == 1
    sells[is_sell] = sizes[is_sell]

    unk = ~(is_buy | is_sell)
    u_sz = sizes[unk]
    # The scalar rule: buy = size // 2, plus one when BOTH the size and the
    # tick index are odd. `& 1` on each is the same parity test the scalar
    # `(size & 1) and (tick_index & 1)` performs.
    u_buy = u_sz // 2
    u_buy += (u_sz & 1) & (tis[unk] & 1)
    buys[unk] = u_buy
    sells[unk] = u_sz - u_buy
    return buys, sells


@dataclass(slots=True, frozen=True)
class Trade:
    """A single time-and-sales print."""

    symbol: str
    price: float
    size: int
    aggressor: Aggressor
    ts_ms: int          # market timestamp, epoch milliseconds


@dataclass(slots=True, frozen=True)
class Execution:
    """One of YOUR fills, derived from a change in position size.

    The feed does not carry an execution report. What it does carry is the
    account's position in each security (`posSize`), and a change in that is a
    fill. Read the limits before trusting a marker to the cent:

      * `size` is the NET change between two L1 snapshots. Several fills inside
        one snapshot interval arrive as a single marker, and two fills that
        cancel out are invisible.
      * `price` is the snapshot's last trade price, not the actual fill price.
        It is the right price to within one snapshot's worth of movement.

    That is honest enough to mark where you traded on the chart, and not
    precise enough to reconcile a blotter against. `is_buy` is exact: the sign
    of a position change cannot be ambiguous.
    """

    symbol: str
    price: float
    size: int               # absolute share count of the change
    is_buy: bool            # position increased
    position: int           # resulting position, signed
    ts_ms: int


@dataclass(slots=True, frozen=True)
class BookSnapshot:
    """A full L2 depth sweep at one instant (used by heatmap / DOM only)."""

    symbol: str
    bids: dict[float, int]      # price -> resting size
    asks: dict[float, int]
    ts_ms: int
    # Memoised PriceLadder as (tick, ladder). Frozen dataclass, so it is written
    # through object.__setattr__ in `ladder()`.
    _cache: list = field(default_factory=list, compare=False, repr=False)

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def ladder(self, tick: float) -> "PriceLadder":
        """This sweep as a PriceLadder, built once and shared.

        Two things made book ingestion the app's bottleneck, and this fixes
        both. Measured at 774,000 `Instruments.to_index` calls for 3,000 books -
        258 per book, each re-doing `symbol.upper()` and a dict lookup before a
        divide - which was 73% of the entire drain.

          * the whole sweep converts in ONE vectorised pass instead of a Python
            call per level;
          * the result is cached on the snapshot, so the BookmapBuffer and the
            BarSeries share one ladder instead of building the same thing twice.

        Duplicate tick indices keep the LAST occurrence, i.e. asks win over
        bids, which is what the dict-merge it replaces did on a crossed book.
        """
        cache = self._cache
        if cache and cache[0] == tick:
            return cache[1]

        nb, na = len(self.bids), len(self.asks)
        if nb + na == 0:
            lad = EMPTY_LADDER
        else:
            px = np.concatenate((
                np.fromiter(self.bids.keys(), dtype=np.float64, count=nb),
                np.fromiter(self.asks.keys(), dtype=np.float64, count=na)))
            sz = np.concatenate((
                np.fromiter(self.bids.values(), dtype=np.int64, count=nb),
                np.fromiter(self.asks.values(), dtype=np.int64, count=na)))
            # DROP UNCHARTABLE LEVELS BEFORE THE CAST. astype(np.int32) on a
            # NaN or a price of 1e12 does not raise - it wraps to garbage, and
            # a garbage tick index puts a phantom wall at an arbitrary price
            # that looks exactly like real liquidity. Filtering first is the
            # difference between a level that is missing and a level that is
            # invented, and only one of those is survivable.
            good = np.isfinite(px) & (px > 0.0) & (px <= MAX_PRICE)
            if not good.all():
                px = px[good]
                sz = sz[good]
            if px.size == 0:
                lad = EMPTY_LADDER
                cache[:] = (tick, lad)
                return lad
            ti = np.rint(px / tick).astype(np.int32)
            # np.unique over the REVERSED arrays: its "first" occurrence is the
            # last in original order, so asks override bids, and the returned
            # keys are already sorted ascending - which is the ladder's contract.
            u, idx = np.unique(ti[::-1], return_index=True)
            lad = PriceLadder(u, sz[::-1][idx].astype(np.int32))

        cache[:] = (tick, lad)
        return lad


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
