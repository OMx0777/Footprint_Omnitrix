"""A whole session of heatmap, kept at a resolution you can actually afford.

THE PROBLEM. The live bookmap buffer holds 1-second columns at 1-tick price
resolution and caps at 1,400 of them - about 23 minutes. A trader wants the
whole session: where the size sat at 09:45, what got pulled into the 11:00
drift, which shelf the afternoon rallied off. At live resolution that is
23,400 columns a symbol, and at the measured 2.3 kB per ladder it is 54 MB a
symbol - 10.8 GB across 200. Not possible, and not necessary.

THE OBSERVATION THAT MAKES IT CHEAP. Nobody reads a six-hour heatmap at one
second and one cent. At that zoom a single pixel column already spans minutes,
so the screen throws the precision away before the eye ever sees it. Storing it
was paying full price for detail that is discarded during paint.

So the archive keeps the whole session at the resolution it will actually be
LOOKED at, and three separate compressions get it there:

  1. TIME. `col_s` live columns fold into one slot (default 30), and resting
     liquidity is TIME-AVERAGED across them. A wall that stood for the whole
     slot stays bright; a flash order that showed for one second fades to a
     thirtieth of its size, which is the honest picture - it really was only
     there for a moment. Measured on a fixture carrying both: a two-minute wall
     reads 89x the surrounding book, and the same size flashed for one second
     reads 16x dimmer than that wall.

  2. PRICE. `tick_step` ticks fold into one bin (default 4), SUMMED. Summing
     rather than taking the maximum is what a zoomed-out view means: "this much
     size rested in this band". A genuine wall is ten to a hundred times the
     surrounding book, so it still stands out against a band that merely added
     four ordinary levels together.

  3. AMPLITUDE. Sizes are stored as a LOG-QUANTISED uint8. This is the one that
     looks lossy and is not: the heatmap renders through a colour ramp with 256
     entries, so anything finer than one ramp step cannot be seen. Measured
     worst case 3.03% across 1 to 10,000,000 - finer than the ramp it feeds.
     Four bytes become one and the picture is identical.

Bins are stored DENSE over the occupied range rather than sparse. A book is a
contiguous ladder around the mid, so a sparse (index, value) pair spends four
bytes on an index to save nothing - dense is smaller in the shape this data
actually has.

MEASURED, at the shipped defaults over a 6.5-hour session: 780 slots a symbol,
~50 bins a slot, 188 kB a symbol - 38 MB across 200 symbols, against 10.8 GB
for the naive version.

WHAT IS NOT COMPROMISED. Per-slot total volume and signed delta are kept as
exact integers, because those are data-truth quantities that the footprint and
the monitor also report for the same session and the two must agree. Only the
per-bin SHAPE is quantised. `sweeps` keeps its usual meaning - how many book
snapshots were really measured - and columns handed to the renderer carry
`archived = True`, so a compressed summary can never be mistaken for measured
per-second history.
"""

from __future__ import annotations

import math

import numpy as np

from .model import PriceLadder, EMPTY_LADDER

# ---- amplitude quantisation ------------------------------------------------
# 255 log steps across 0..1e7. log1p(1e7) = 16.118, so K = 255/16.118 puts the
# top of the range exactly at the top of the byte.
HEAT_MAX = 10_000_000.0
HEAT_K = 255.0 / math.log1p(HEAT_MAX)

# Decode table, built once. Turning a byte back into a size happens per bin per
# visible slot during paint, and expm1 in a Python loop there would be the most
# expensive thing on the frame.
_DEQ = np.expm1(np.arange(256, dtype=np.float64) / HEAT_K)


def quantise(v: float) -> int:
    """Size -> 0..255. Monotonic, so ordering by heat is ordering by size."""
    if v <= 0:
        return 0
    return min(255, int(math.log1p(v) * HEAT_K + 0.5))


def quantise_array(a: np.ndarray) -> np.ndarray:
    out = np.log1p(np.maximum(a, 0.0)) * HEAT_K + 0.5
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def dequantise(codes) -> np.ndarray:
    """0..255 -> approximate size. Vectorised through the lookup table."""
    return _DEQ[np.asarray(codes, dtype=np.uint8)]


# ---- defaults --------------------------------------------------------------
# 30 live seconds per archive slot. A 6.5-hour session is 780 slots, which on a
# 1,600 px chart is two pixels a slot - already finer than the screen when the
# whole day is on it.
ARCH_COL_S = 30.0
# 4 ticks a bin. On a cent-tick name that is a 4c band; a 500-tick daily range
# becomes 125 bins.
ARCH_TICK_STEP = 4
# 10 hours of slots, so pre-market through the close fits with room to spare
# and the structure is still hard-bounded.
ARCH_MAX_SLOTS = 1200
# How many aggregations keep a converted copy. Four bookmap panes is the most
# the app opens at once, so four entries covers every real layout.
_CONV_CACHE_MAX = 4


class ArchiveSlot:
    """One coarse time slot. Dense over [bin0, bin0 + len(heat))."""

    __slots__ = ("bucket", "bin0", "heat", "buyq", "sellq",
                 "bid_bin", "ask_bin", "vol", "net", "sweeps", "cols")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.bin0 = 0
        self.heat = np.zeros(0, dtype=np.uint8)
        self.buyq = np.zeros(0, dtype=np.uint8)
        self.sellq = np.zeros(0, dtype=np.uint8)
        self.bid_bin: int | None = None
        self.ask_bin: int | None = None
        # EXACT, not quantised - data-truth totals other views must agree with.
        self.vol = 0
        self.net = 0
        self.sweeps = 0
        self.cols = 0            # live columns folded in, for the time average

    def sizes(self) -> np.ndarray:
        """Approximate resting size per bin, decoded from the byte codes."""
        return dequantise(self.heat)

    def nbytes(self) -> int:
        return self.heat.nbytes + self.buyq.nbytes + self.sellq.nbytes + 96


class ArchivedColumn:
    """An archive slot wearing the same shape as a live Column.

    The renderer already knows how to draw a Column. Teaching it a second shape
    would put two drawing paths behind one picture, and the one exercised less
    would rot - which is how the signals dock ended up with a NameError on the
    only branch that mattered. So the archive converts, and every existing item
    (heat, BBO, volume bars, the DOM projection) works on it unchanged.

    `archived` is the one addition, read with getattr so the live Column class
    does not have to carry the attribute.
    """

    __slots__ = ("bucket", "book", "buy", "sell", "bid_ti", "ask_ti", "vol",
                 "net", "sweeps", "archived")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.book = EMPTY_LADDER
        self.buy: dict[int, int] = {}
        self.sell: dict[int, int] = {}
        self.bid_ti = None
        self.ask_ti = None
        self.vol = 0
        self.net = 0
        self.sweeps = 0
        self.archived = True


class _Accum:
    """Open slot being accumulated, before it is quantised and frozen."""

    __slots__ = ("bucket", "lo", "hi", "rest", "buy", "sell",
                 "bid_bin", "ask_bin", "vol", "net", "sweeps", "cols")

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.lo = None
        self.hi = None
        self.rest: dict[int, float] = {}
        self.buy: dict[int, int] = {}
        self.sell: dict[int, int] = {}
        self.bid_bin = None
        self.ask_bin = None
        self.vol = 0
        self.net = 0
        self.sweeps = 0
        self.cols = 0


class SessionArchive:
    """Compressed whole-session heat for ONE symbol.

    Fed the columns the live ring evicts, in chronological order. Holds no
    reference to anything the live buffer owns, so a column can be dropped the
    moment it has been folded.
    """

    __slots__ = ("col_s", "tick_step", "max_slots", "live_dt",
                 "_slots", "_open", "_dropped", "_conv")

    def __init__(self, col_s: float = ARCH_COL_S,
                 tick_step: int = ARCH_TICK_STEP,
                 max_slots: int = ARCH_MAX_SLOTS,
                 live_dt: float = 1.0):
        self.col_s = float(col_s)
        self.tick_step = max(1, int(tick_step))
        self.max_slots = int(max_slots)
        self.live_dt = float(live_dt)
        self._slots: list[ArchiveSlot] = []
        self._open: _Accum | None = None
        self._dropped = 0
        # agg -> (n_slots_converted, columns, first_slot_index_of_last_group)
        self._conv: dict[int, tuple] = {}

    # ---- ingest ----------------------------------------------------------
    def fold(self, col) -> None:
        """Absorb one live Column. Cheap enough to run during eviction.

        Columns arrive oldest-first because the live ring evicts from the front
        of a sorted list. A slot older than the one currently open is therefore
        out of order; it is counted and dropped rather than reopening a frozen
        slot, which would corrupt a time average already taken.
        """
        b = int((col.bucket * self.live_dt) // self.col_s)
        op = self._open
        if op is not None and b != op.bucket:
            if b < op.bucket:
                self._dropped += 1
                return
            self._freeze(op)
            op = None
        if op is None:
            op = self._open = _Accum(b)
        self._absorb(op, col)

    def _absorb(self, op: _Accum, col) -> None:
        step = self.tick_step
        op.cols += 1
        op.vol += int(col.vol)
        op.net += int(col.net)
        op.sweeps += int(col.sweeps)

        book = col.book
        if len(book):
            ti, sz = book.arrays()
            # Floor division, correct for negative tick indices too.
            bins = np.floor_divide(ti.astype(np.int64), step)
            uniq, inv = np.unique(bins, return_inverse=True)
            tot = np.bincount(inv, weights=sz.astype(np.float64))
            rest = op.rest
            for k, v in zip(uniq.tolist(), tot.tolist()):
                rest[k] = rest.get(k, 0.0) + v
            lo, hi = int(uniq[0]), int(uniq[-1])
            op.lo = lo if op.lo is None else min(op.lo, lo)
            op.hi = hi if op.hi is None else max(op.hi, hi)

        for src, dst in ((col.buy, op.buy), (col.sell, op.sell)):
            for ti, v in src.items():
                k = ti // step
                dst[k] = dst.get(k, 0) + v
                op.lo = k if op.lo is None else min(op.lo, k)
                op.hi = k if op.hi is None else max(op.hi, k)

        if col.bid_ti is not None:
            op.bid_bin = col.bid_ti // step
        if col.ask_ti is not None:
            op.ask_bin = col.ask_ti // step

    def _freeze(self, op: _Accum) -> None:
        s = ArchiveSlot(op.bucket)
        s.vol, s.net, s.sweeps, s.cols = op.vol, op.net, op.sweeps, op.cols
        s.bid_bin, s.ask_bin = op.bid_bin, op.ask_bin
        if op.lo is not None:
            n = op.hi - op.lo + 1
            s.bin0 = op.lo
            rest = np.zeros(n, dtype=np.float64)
            buy = np.zeros(n, dtype=np.float64)
            sell = np.zeros(n, dtype=np.float64)
            for k, v in op.rest.items():
                rest[k - op.lo] = v
            for k, v in op.buy.items():
                buy[k - op.lo] = v
            for k, v in op.sell.items():
                sell[k - op.lo] = v
            # TIME-AVERAGE the resting book. Summing would make a wall look
            # `cols` times bigger simply for standing still, and would make
            # slots with different column counts incomparable.
            if op.cols:
                rest /= op.cols
            s.heat = quantise_array(rest)
            s.buyq = quantise_array(buy)
            s.sellq = quantise_array(sell)
        self._slots.append(s)
        if len(self._slots) > self.max_slots:
            del self._slots[:len(self._slots) - self.max_slots]
            # A front eviction invalidates every converted prefix; the tail-only
            # rebuild in as_columns assumes slots are append-only.
            self._conv.clear()
        self._open = None

    def flush(self) -> None:
        """Freeze the open slot, so the newest partial slot is visible instead
        of waiting for the next one to start."""
        if self._open is not None:
            self._freeze(self._open)

    # ---- render adapter ---------------------------------------------------
    def as_columns(self, agg: int, before_bucket: int | None = None) -> list:
        """Archive slots as Column-shaped objects on the live `agg` x-grid.

        `agg` is the bookmap timeframe - how many live columns share one drawn
        column - so a group index is `live_bucket // agg`, and the archive has
        to land on those same integers or the archived region will not line up
        with the live one at the seam.

        Two regimes, both honest:

          * agg >= the archive's own resolution: several slots merge into one
            drawn column - a further fold of already-folded data;
          * agg < it: one slot spans several drawn columns and is REPEATED
            across them. That is coarse data shown at a finer zoom, not
            invented detail: every repetition reports the same summary and is
            flagged `archived`.

        INCREMENTAL. A full conversion of two hours at agg=1 measured 121 ms,
        and it would run every time a slot landed - every 30 seconds, on the
        frame thread, growing with the session. That is the shape of every
        stall this app has had. Slots are append-only, so all but the final
        group are already correct and only the tail is rebuilt.
        """
        if not self._slots:
            return []
        agg = max(1, int(agg))
        # KEYED BY agg, not a single slot. Four bookmap panes can each be on a
        # different timeframe, and a one-entry cache is then thrashed by the
        # panes taking turns - measured 92 ms per frame at agg=1 with three
        # aggregations in rotation, against 0.06 ms when the entry survives.
        # Bounded so a user cycling the timeframe combo cannot accumulate
        # conversions of a whole session at every zoom.
        c = self._conv.get(agg)
        if c is not None and c[0] == len(self._slots):
            return self._trim(c[1], before_bucket)      # nothing new at all
        if c is None or c[0] > len(self._slots):
            cols, start = [], 0
        else:
            # Re-do the final group: a newly arrived slot can merge into it.
            start = c[2]
            first_new_g = int(self._slots[start].bucket * self.col_s
                              / self.live_dt) // agg
            cols = c[1]
            k = len(cols)
            while k > 0 and cols[k - 1].bucket >= first_new_g:
                k -= 1
            cols = cols[:k]
        cols, group_start = self._convert(agg, cols, start)
        if len(self._conv) >= _CONV_CACHE_MAX and agg not in self._conv:
            self._conv.pop(next(iter(self._conv)))
        self._conv[agg] = (len(self._slots), cols, group_start)
        return self._trim(cols, before_bucket)

    @staticmethod
    def _trim(cols: list, before_bucket) -> list:
        if before_bucket is None:
            return cols
        return cols[:_bisect_cols(cols, before_bucket)]

    def _convert(self, agg: int, cols: list, start: int):
        """Convert slots [start:] onto the agg grid, extending `cols`."""
        per_slot = self.col_s / self.live_dt
        out = list(cols)
        by_group = {}
        group_start = start
        for si in range(start, len(self._slots)):
            s = self._slots[si]
            first = int(s.bucket * per_slot)
            last = int((s.bucket + 1) * per_slot) - 1
            g0, g1 = first // agg, last // agg
            if not out or g0 > out[-1].bucket:
                group_start = si
            for g in range(g0, g1 + 1):
                col = by_group.get(g)
                if col is None:
                    if out and out[-1].bucket == g:
                        col = out[-1]
                    else:
                        col = ArchivedColumn(g)
                        out.append(col)
                    by_group[g] = col
                if g == g0:
                    # Totals belong to the slot, not to each repetition. When a
                    # slot spans several groups they go to the FIRST only:
                    # repeating them would multiply the session's volume by the
                    # zoom level, and splitting them evenly would invent a
                    # distribution that was never measured.
                    col.vol += s.vol
                    col.net += s.net
                    col.sweeps += s.sweeps
                # Newest slot in a group wins the book, matching view()'s rule
                # that a group's resting book is its most recent snapshot.
                col.book = _slot_ladder(s, self.tick_step)
                if s.bid_bin is not None:
                    col.bid_ti = int(s.bid_bin * self.tick_step)
                if s.ask_bin is not None:
                    col.ask_ti = int(s.ask_bin * self.tick_step)
                _spread(s.buyq, s.bin0, self.tick_step, col.buy)
                _spread(s.sellq, s.bin0, self.tick_step, col.sell)
        return out, group_start

    # ---- read ------------------------------------------------------------
    def slots(self) -> list[ArchiveSlot]:
        return self._slots

    def view(self, t0_s: float, t1_s: float) -> list[ArchiveSlot]:
        """Frozen slots whose time falls in [t0_s, t1_s], in seconds."""
        if not self._slots:
            return []
        b0 = int(math.floor(t0_s / self.col_s))
        b1 = int(math.ceil(t1_s / self.col_s))
        return self._slots[_bisect_left(self._slots, b0):
                           _bisect_right(self._slots, b1)]

    def span_s(self) -> tuple[float, float] | None:
        if not self._slots:
            return None
        return (self._slots[0].bucket * self.col_s,
                (self._slots[-1].bucket + 1) * self.col_s)

    def nbytes(self) -> int:
        return sum(s.nbytes() for s in self._slots) + 200

    def stats(self) -> dict:
        return {"slots": len(self._slots), "bytes": self.nbytes(),
                "dropped_late": self._dropped, "span_s": self.span_s()}


def _slot_ladder(slot: ArchiveSlot, tick_step: int) -> PriceLadder:
    """Decode a slot's heat back onto the original tick grid.

    Each bin covers `tick_step` ticks and is placed at the bin's first tick.
    The band really is that wide - drawing it one tick wide would present a
    coarse summary as if it were precise, which is the one thing this codebase
    does not do with reconstructed data.
    """
    if not slot.heat.size:
        return EMPTY_LADDER
    sizes = dequantise(slot.heat)
    nz = np.nonzero(sizes > 0.5)[0]
    if not nz.size:
        return EMPTY_LADDER
    ti = ((slot.bin0 + nz) * tick_step).astype(np.int32)
    return PriceLadder(ti, sizes[nz].astype(np.int32))


def _spread(codes, bin0, step, out: dict) -> None:
    """Add a quantised per-bin array into a tick-indexed dict."""
    if not codes.size:
        return
    nz = np.nonzero(codes)[0]
    if not nz.size:
        return
    for i, v in zip(nz.tolist(), dequantise(codes[nz]).tolist()):
        k = int((bin0 + i) * step)
        out[k] = out.get(k, 0) + int(v)


def _bisect_left(slots, bucket):
    lo, hi = 0, len(slots)
    while lo < hi:
        mid = (lo + hi) // 2
        if slots[mid].bucket < bucket:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(slots, bucket):
    lo, hi = 0, len(slots)
    while lo < hi:
        mid = (lo + hi) // 2
        if slots[mid].bucket <= bucket:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_cols(cols, bucket):
    lo, hi = 0, len(cols)
    while lo < hi:
        mid = (lo + hi) // 2
        if cols[mid].bucket < bucket:
            lo = mid + 1
        else:
            hi = mid
    return lo
