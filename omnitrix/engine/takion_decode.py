r"""Shared Takion wire decoding: the ONE place records become events.

`PipeFeed` (local named pipes) and `NetworkFeed` (remote broadcaster) carry the
same bytes over different transports. They used to hold two copies of the
decoding - 107 of NetworkFeed's 169 lines were duplicated from PipeFeed - which
means a correction to one silently leaves the other wrong. There is one copy
now; the transports only supply bytes.

  \\.\pipe\TakionOHLCV   104-byte  '<32sddddddQIiII'
      symbol, open, high, low, last, bid, ask, cum_vol, time_ms, pos,
      bid_size, ask_size

  \\.\pipe\TakionData    32-byte   '<8s8sdIc3x'
      symbol, mmid, price, size, side   (side 'B' bid, 'A' ask, 'C' = sweep
      complete -> emit the assembled BookSnapshot; its price field carries the
      DLL's epoch-ms publish timestamp)

WHAT A "TRADE" HERE ACTUALLY IS - read this before trusting a delta.
The L1 record is a SNAPSHOT, not a print. A trade's size is the change in
cumulative volume since the previous snapshot, priced at that snapshot's last
price. If several executions land between two snapshots they arrive as one
synthetic print carrying their combined size at the newest price. Volume is
therefore exact; the split of that volume across prices and aggressors is as
fine-grained as the snapshot rate, no finer. Nothing downstream can recover
detail the feed did not carry, and pretending otherwise is how a chart starts
asserting things that never happened.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import replace

from .model import Trade, BookSnapshot, Execution, Aggressor
from .feed import Feed

log = logging.getLogger("omnitrix.takion")

L1 = struct.Struct("<32sddddddQIiII")     # 104 bytes
L2 = struct.Struct("<8s8sdIc3x")          # 32 bytes


def _cstr(b: bytes) -> str:
    return b.split(b"\x00")[0].decode("ascii", "ignore").strip().upper()


_DAY_MS = 86_400_000
_TS_SAMPLES = 25          # records median-averaged before locking the L1 offset
# ...but do not wait forever for them. A single-symbol session can take tens of
# seconds to produce 25 L1 records, and trades are withheld until the offset is
# known, so lock early off a smaller sample rather than stall the chart.
_TS_MIN_SAMPLES = 5
_TS_MAX_WAIT_S = 5.0
_TS_PENDING_MAX = 20_000  # hard bound on withheld trades (safety valve only)

# Below this a 'C' marker's price field is not a timestamp (2001-09-09). A DLL
# predating the sweep-stamping change sends 0.0 there.
_MIN_EPOCH_MS = 1_000_000_000_000


def _midnight_ms() -> int:
    """Local midnight today, in epoch milliseconds."""
    t = time.localtime()
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0,
                            t.tm_wday, t.tm_yday, t.tm_isdst))
    return int(midnight * 1000)


def to_epoch_ms(raw: int) -> int:
    """Normalise the L1 record's timestamp to epoch milliseconds.

    The struct field is a uint32, which cannot hold epoch-ms (that overflows at
    ~49.7 days), so Takion sends milliseconds-since-midnight. Bucketing bars on
    the raw value would place every bar in 1970, so rebase it onto today.
    Values that already look like epoch-ms are passed through unchanged.
    """
    if raw <= 0:
        return int(time.time() * 1000)
    if raw < _DAY_MS:                     # ms since midnight -> today
        return _midnight_ms() + int(raw)
    return int(raw)                       # already epoch ms


class TakionDecoder(Feed):
    """Turns L1/L2 records into Trade / BookSnapshot events."""

    # A sweep that never sends its 'C' marker would otherwise accumulate every
    # price it ever saw. 4096 is ~16x the deepest real sweep (128 per side, and
    # the DLL is capped at 1024), so hitting it means the stream is broken, not
    # that the book is unusually deep.
    MAX_PARTIAL_LEVELS = 4096

    def __init__(self, symbols: list[str] | None = None,
                 lot_multiplier: int = 1):
        super().__init__()
        # None = accept every symbol the DLL sends
        self.symbols = {s.upper() for s in symbols} if symbols else None
        # The DLL already reports sizes in shares (verified against the live
        # book: "x202 ARCA", "x8466 NYS"), so no lot conversion is applied.
        self.lot_multiplier = lot_multiplier
        self._ts_offset: int | None = None      # see _align_ts()
        self._ts_samples: list[int] = []
        self._ts_first_at: float | None = None
        # Trades held until the clock offset locks - see _on_l1().
        self._pending: list[tuple[Trade, int]] = []
        self._last_vol: dict[str, int] = {}
        self._last_px: dict[str, float] = {}    # for the tick test
        # Your position per symbol. A change in it is a fill - the only
        # execution information this feed carries. None = not yet seen, which
        # is NOT the same as flat: seeding from the first snapshot would
        # otherwise report your whole existing position as a fresh trade the
        # moment the app connects.
        self._pos: dict[str, int] = {}
        self._bids: dict[str, dict[float, int]] = {}
        self._asks: dict[str, dict[float, int]] = {}
        self._overflow_warned: set = set()   # (symbol, side) already logged
        # "dll" | "receipt", logged once so a stale DLL is visible rather than
        # silently degrading book timing back to arrival time.
        self.sweep_clock: str | None = None
        # How each print was classified. Exposed because it is the single best
        # measure of feed quality: a high `mid`/`tick` share means the quote is
        # lagging the prints, and a high `unknown` share means the chart's
        # delta is being carried by an even split rather than by evidence.
        self.cls = {"quote": 0, "mid": 0, "tick": 0, "ztick": 0,
                    "unknown": 0}
        # Direction of the last PRICE CHANGE per symbol, for the zero-tick
        # rule below. +1 after an uptick, -1 after a downtick.
        self._last_dir: dict[str, int] = {}
        # Per-symbol diagnostics. "(no prints yet)" is true but useless - a
        # symbol can be silent for four different reasons and they need
        # different fixes. Counted here so the status line can say which.
        self.sym_l1: dict[str, int] = {}      # L1 records seen
        self.sym_l2: dict[str, int] = {}      # book records seen
        self.sym_trades: dict[str, int] = {}  # trades emitted
        self.sym_zero_px: dict[str, int] = {}  # L1 arrived with no last price

    # ---- connection lifecycle --------------------------------------------
    def on_l1_disconnect(self) -> None:  # noqa: D401
        """Forget cumulative volume. MUST be called when the L1 link drops.

        A trade's size is the change in cumulative volume between consecutive
        snapshots. Across an outage that difference spans the whole gap, so the
        first record back emitted ONE synthetic print carrying every share that
        traded while we were away - measured at 3,999,900 shares on a 30-second
        gap. That is a fabricated block: it lands at a single price with a
        single aggressor, trips the block detector, and moves delta by millions.

        Clearing the counter makes the first record after a reconnect re-seed
        instead, so the missed volume is simply absent rather than invented. It
        IS a gap either way; an honest gap is far less harmful than a fake
        block, and `dropped`/status already tell the user the feed broke.
        """
        if self._last_vol:
            log.info("L1 link dropped: forgetting cumulative volume for %d "
                     "symbols so the gap is not emitted as one huge print",
                     len(self._last_vol))
        self._last_vol.clear()
        # Same reasoning: after a gap the position we come back to may differ
        # from the one we left, and the difference is not a trade we saw.
        self._pos.clear()

    def on_disconnect(self) -> None:
        """Drop every half-assembled book. MUST be called when a link drops.

        A sweep is assembled across many records and only published on its 'C'
        marker, so a link that drops mid-sweep leaves a partial book behind.
        Nothing used to clear it, and the merge is additive:

            d[price] = d.get(price, 0) + size

        so the FIRST sweep after every reconnect was silently corrupt - levels
        present on both sides of the outage reported their combined size, and
        levels pulled during the outage survived as liquidity that no longer
        existed. A phantom wall on the heatmap is exactly the kind of false
        data a trader would act on.

        Cumulative volume is deliberately NOT reset: `_on_l1` already treats a
        decrease as a restart, and clearing it would emit one bogus trade for
        the whole session's volume on the next record.
        """
        n = sum(len(d) for d in self._bids.values()) + \
            sum(len(d) for d in self._asks.values())
        if n:
            log.info("link dropped mid-sweep: discarding %d partial book "
                     "levels rather than merging them into the next sweep", n)
        self._bids.clear()
        self._asks.clear()

    # ---- clock -----------------------------------------------------------
    def _wanted(self, sym: str) -> bool:
        return self.symbols is None or sym in self.symbols

    def _align_ts(self, raw_ms: int) -> int:
        """Put L1 trade timestamps on the same clock as L2 book snapshots.

        The L1 record carries ms-since-midnight in the *exchange's* timezone,
        while book snapshots are stamped with local wall-clock time. Rebasing
        the L1 value onto local midnight therefore lands it hours away (ET vs
        IST put trades 9.5h behind the book), so trades and books fell into
        column buckets far apart and the chart snapped between the two regions.

        Measure the discrepancy and snap it to a 15-minute boundary - every real
        timezone offset is a multiple of 15 minutes - so Takion's sub-second
        precision is kept while the absolute time matches the book feed.

        The offset is fixed from the MEDIAN of the first `_TS_SAMPLES` records
        rather than the single first one: one stale or zero-ish `time_ms` at
        connect time would otherwise poison every timestamp for the whole
        session, with no way to recover.
        """
        t = to_epoch_ms(raw_ms)
        if raw_ms <= 0:
            return t        # no usable timestamp: to_epoch_ms already gave now
        if self._ts_offset is None:
            now = time.time()
            if self._ts_first_at is None:
                self._ts_first_at = now
            self._ts_samples.append(int(now * 1000) - t)
            enough = len(self._ts_samples) >= _TS_SAMPLES
            waited = (len(self._ts_samples) >= _TS_MIN_SAMPLES
                      and now - self._ts_first_at >= _TS_MAX_WAIT_S)
            if not (enough or waited):
                return t                     # un-shifted until we are confident
            self._ts_samples.sort()
            diff = self._ts_samples[len(self._ts_samples) // 2]
            quarter = 15 * 60 * 1000
            self._ts_offset = int(round(diff / quarter)) * quarter
            log.info("L1 clock offset locked at %+d min (from %d samples)",
                     self._ts_offset // 60000, len(self._ts_samples))
            # AND EVERY CLOCK IN THE UI FOLLOWS THE EXCHANGE FROM HERE.
            # This is the same number, measured the same way, so the labels
            # cannot drift from the timestamps they describe.
            try:
                from ..render.crosshair import set_display_offset
                set_display_offset(self._ts_offset)
            except Exception:
                log.debug("could not set the display clock", exc_info=True)
        return t + self._ts_offset

    def _sweep_ts(self, marker_price: float) -> int:
        """Timestamp for a completed sweep, in epoch milliseconds.

        The DLL stamps the 'C' record's otherwise-unused price field with the
        instant it published the sweep. Preferring that to local receipt time
        removes the pipe's batching smear: the writer drains up to 4096 records
        per WriteFile, so a whole batch used to arrive carrying near-identical
        timestamps, and a batch spanning more than one bookmap column collapsed
        several distinct books into one.

        A DLL predating the change sends 0.0; fall back to receipt time.
        """
        if marker_price >= _MIN_EPOCH_MS:
            if self.sweep_clock is None:
                self.sweep_clock = "dll"
                log.info("sweep timestamps: from DLL (batch smear removed)")
            return int(marker_price)
        if self.sweep_clock is None:
            self.sweep_clock = "receipt"
            log.warning("sweep timestamps: falling back to receipt time - the "
                        "deployed DLL predates sweep stamping, so heatmap "
                        "timing carries pipe latency")
        return int(time.time() * 1000)

    # ---- classification --------------------------------------------------
    def classify(self, sym: str, last: float, bid: float,
                 ask: float) -> Aggressor:
        """Which side was the aggressor (Lee-Ready).

        Three tiers, strongest evidence first, each counted separately so the
        share of guessing is visible rather than hidden:

          quote  at or through a side of the NBBO. Direct evidence.
          mid    inside the spread: the side of the midpoint it landed on.
                 Inference, but the standard one - a print above the mid was
                 far likelier taken from the offer.
          tick   exactly at the mid, or no usable quote: compare with the last
                 DIFFERENT trade price. Uptick = buyer-initiated.
          ztick  same price as the last print: inherit the direction of the
                 last price CHANGE. This is the zero-tick half of Lee-Ready,
                 and leaving it out was costing real attribution - measured on
                 a live post-market feed, UNKNOWN climbed 3% -> 19% across a
                 session, because both conditions for it (a print at the mid,
                 and a price that has not moved) get commoner as a book
                 thins. Every one of those was being split 50/50.

        Only a print with no quote, no price change AND no prior direction
        stays UNKNOWN, which the rest of the app splits evenly. The previous rule had just the first
        tier and dropped everything else into UNKNOWN, so on a feed whose quote
        refreshes after each trade the majority of prints carried no direction
        at all - and the renderers that then mis-split them are what made the
        chart read green.

        The quote is the one attached to this snapshot. It can lag the print it
        is compared against; that is a property of the feed, not something this
        function can repair.
        """
        prev = self._last_px.get(sym)
        if last > 0 and prev is not None and last != prev:
            # Record the direction BEFORE overwriting the price, so a later
            # zero-tick knows which way the last real move went.
            self._last_dir[sym] = 1 if last > prev else -1
        if last > 0:
            if prev is None or last != prev:
                self._last_px[sym] = last

        if bid > 0 and ask > 0 and ask >= bid:
            if last >= ask:
                self.cls["quote"] += 1
                return Aggressor.BUY
            if last <= bid:
                self.cls["quote"] += 1
                return Aggressor.SELL
            mid = (bid + ask) / 2.0
            if last > mid:
                self.cls["mid"] += 1
                return Aggressor.BUY
            if last < mid:
                self.cls["mid"] += 1
                return Aggressor.SELL

        if prev is not None and last > 0 and last != prev:
            self.cls["tick"] += 1
            return Aggressor.BUY if last > prev else Aggressor.SELL

        # Zero tick: the price has not moved, so carry the last move's
        # direction. Weaker than a quote and counted separately, but it is
        # evidence - and the alternative is splitting the print in half.
        d = self._last_dir.get(sym)
        if d and last > 0:
            self.cls["ztick"] += 1
            return Aggressor.BUY if d > 0 else Aggressor.SELL

        self.cls["unknown"] += 1
        return Aggressor.UNKNOWN

    def symbol_health(self, sym: str) -> str:
        """Why does this symbol have no prints? Returns a short reason.

        A symbol reaches the picker as soon as ANY record mentions it, depth
        included, so "registered" and "printing" are different things. The four
        cases below need different responses, and "(no prints yet)" told the
        user none of them.
        """
        l1 = self.sym_l1.get(sym, 0)
        l2 = self.sym_l2.get(sym, 0)
        zero = self.sym_zero_px.get(sym, 0)
        if l1 == 0 and l2 == 0:
            return "nothing received for this symbol"
        if l1 == 0:
            return f"depth only ({l2:,} book records, no L1) - not in the feed's basket?"
        if zero and zero >= l1 * 0.9:
            return f"L1 arriving but with no last price ({zero:,} records)"
        if l1 == 1:
            return "one L1 record so far - a trade needs a volume CHANGE"
        return f"{l1:,} L1 records, volume unchanged - no trades since connect"

    def quality(self) -> dict:
        """Classification mix as fractions, for the status readout."""
        tot = sum(self.cls.values()) or 1
        return {k: v / tot for k, v in self.cls.items()}

    # ---- record handlers -------------------------------------------------
    def _on_l1(self, chunk: bytes, off: int) -> None:
        (sym_b, _o, _h, _l, last, bid, ask, cum_vol, time_ms,
         pos_size, _bsz, _asz) = L1.unpack_from(chunk, off)
        sym = _cstr(sym_b)
        if not sym or not self._wanted(sym):
            return
        self.sym_l1[sym] = self.sym_l1.get(sym, 0) + 1

        # A change in position size is a fill. Emitted before the price guard
        # below only if we have a price to attach it to.
        prev_pos = self._pos.get(sym)
        if prev_pos is None:
            self._pos[sym] = int(pos_size)      # seed; not a trade
        elif int(pos_size) != prev_pos:
            delta = int(pos_size) - prev_pos
            self._pos[sym] = int(pos_size)
            if last > 0.0 and last == last:
                self._emit_exec(Execution(
                    sym, float(last), abs(delta), delta > 0, int(pos_size),
                    self._align_ts(int(time_ms))))

        # An unpriceable record cannot become a trade. `last` is 0.0 whenever
        # the Security has no last price yet (the struct is memset to zero and
        # every fallback in the DLL failed), and a print at price 0 is not a
        # harmless oddity: it is classified SELL because 0 <= bid, it drags the
        # bar's low - and therefore the whole chart's y-range - down to zero,
        # and it poisons VWAP and the volume profile for the session. One bad
        # record used to be enough to make the chart unreadable.
        if not (last > 0.0) or last != last:          # <=0, NaN
            self.sym_zero_px[sym] = self.sym_zero_px.get(sym, 0) + 1
            return

        prev = self._last_vol.get(sym)
        self._last_vol[sym] = cum_vol
        if prev is None or cum_vol <= prev:
            return                              # first tick, or a volume reset
        size = int(cum_vol - prev)
        if size <= 0:
            return

        aggr = self.classify(sym, last, bid, ask)
        raw = int(time_ms)
        tr = Trade(sym, float(last), size, aggr, self._align_ts(raw))

        # Withhold trades until the exchange-vs-local offset is known.
        #
        # _align_ts() returns the timestamp UN-SHIFTED while it is still
        # measuring, which places those trades hours from the book clock (ET vs
        # IST is 9.5 h). Emitting them created a bar far in the past at the head
        # of the series - and because VWAP and CVD are cumulative from bar 0,
        # that one bogus bar skewed both for the rest of the session.
        if self._ts_offset is None and raw > 0:
            if len(self._pending) < _TS_PENDING_MAX:
                self._pending.append((tr, raw))
            return

        if self._pending:
            held, self._pending = self._pending, []
            for held_tr, held_raw in held:
                self._emit_trade(replace(held_tr, ts_ms=self._align_ts(held_raw)))
        self._emit_trade(tr)

    def _on_l2(self, chunk: bytes, off: int) -> None:
        sym_b, _mmid_b, price, size, side_b = L2.unpack_from(chunk, off)
        sym = _cstr(sym_b)
        if not sym or not self._wanted(sym):
            return
        self.sym_l2[sym] = self.sym_l2.get(sym, 0) + 1
        side = side_b.decode("ascii", "ignore")

        if side == "C":                          # sweep complete
            bids = self._bids.pop(sym, {})
            asks = self._asks.pop(sym, {})
            if bids or asks:
                self._emit_book(BookSnapshot(sym, bids, asks,
                                             self._sweep_ts(price)))
        elif side == "B" or side == "A":
            # Sizes at one price SUM across market makers - that is real depth,
            # not double counting.
            d = (self._bids if side == "B" else self._asks).setdefault(sym, {})
            if price in d or len(d) < self.MAX_PARTIAL_LEVELS:
                d[price] = d.get(price, 0) + size * self.lot_multiplier
            else:
                # Guard, not a policy: a sweep this deep never completes, so it
                # would otherwise grow for the life of the process. Existing
                # levels still update, so the book stays coherent - it just
                # stops accepting NEW prices until a 'C' clears it.
                key = (sym, side)
                if key not in self._overflow_warned:
                    self._overflow_warned.add(key)
                    log.warning("%s %s side passed %d levels with no sweep "
                                "marker; refusing further prices until it "
                                "completes", sym, side, self.MAX_PARTIAL_LEVELS)
