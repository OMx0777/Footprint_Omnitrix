r"""Live Takion feed — reads the C++ extension's two Windows named pipes and
emits the same `Trade` / `BookSnapshot` events as `SyntheticFeed`, so every
chart in the app works unchanged on real market data.

  \\.\pipe\TakionOHLCV   104-byte  '<32sddddddQIiII'
      symbol, open, high, low, last, bid, ask, cum_vol, time_ms, pos,
      bid_size, ask_size
      -> trades are derived from the cumulative-volume delta, with the
         aggressor classified by where the print sits vs the quote.

  \\.\pipe\TakionData    32-byte   '<8s8sdIc3x'
      symbol, mmid, price, size, side   (side 'B' bid, 'A' ask, 'C' = sweep
      complete -> emit the assembled BookSnapshot)
      -> on a 'C' record the price field carries the sweep's epoch-ms
         publish timestamp; see _sweep_ts().

This process is the pipe *server*: it creates the pipes and waits for the DLL
to connect, reconnecting automatically if Takion restarts.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import replace

from .model import Trade, BookSnapshot, Aggressor
from .feed import Feed

log = logging.getLogger("omnitrix.pipe")

L1_PIPE = r"\\.\pipe\TakionOHLCV"
L2_PIPE = r"\\.\pipe\TakionData"
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


class PipeFeed(Feed):
    """Dual named-pipe reader for the live Takion extension."""

    def __init__(self, symbols: list[str] | None = None,
                 lot_multiplier: int = 1):
        super().__init__()
        # None = accept every symbol the DLL sends
        self.symbols = {s.upper() for s in symbols} if symbols else None
        # The DLL already reports sizes in shares (verified against the live
        # book: "x202 ARCA", "x8466 NYS"), so no lot conversion is applied.
        self.lot_multiplier = lot_multiplier
        self._threads: list[threading.Thread] = []
        self._ts_offset: int | None = None      # see _align_ts()
        self._ts_samples: list[int] = []
        self._ts_first_at: float | None = None
        # Trades held until the clock offset locks - see _on_l1().
        self._pending: list[tuple[Trade, int]] = []
        self._last_vol: dict[str, int] = {}
        self._bids: dict[str, dict[float, int]] = {}
        self._asks: dict[str, dict[float, int]] = {}
        self.connected = {"l1": False, "l2": False}
        # "dll" | "receipt", logged once so a stale DLL is visible rather than
        # silently degrading book timing back to arrival time.
        self.sweep_clock: str | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for target in (self._l1_loop, self._l2_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

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

        Locks early (>= `_TS_MIN_SAMPLES` after `_TS_MAX_WAIT_S`) because
        `_on_l1` withholds trades until the offset is known, and a quiet
        single-symbol feed can take a long time to reach 25 records.
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
        return t + self._ts_offset

    # ---- pipe plumbing ---------------------------------------------------
    def _serve(self, name: str, rec_size: int, on_record, key: str) -> None:
        import win32pipe, win32file, pywintypes

        while self._running:
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    name,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE
                    | win32pipe.PIPE_WAIT,
                    1, 1 << 20, 1 << 20, 0, None)
                win32pipe.ConnectNamedPipe(handle, None)
                self.connected[key] = True
                log.info("%s connected", key.upper())
                buf = bytearray()
                while self._running:
                    _, data = win32file.ReadFile(handle, 1 << 16)
                    if not data:
                        break
                    buf.extend(data)
                    n = len(buf) // rec_size
                    if n:
                        chunk = bytes(buf[:n * rec_size])
                        del buf[:n * rec_size]
                        for off in range(0, len(chunk), rec_size):
                            on_record(chunk, off)
            except pywintypes.error as e:
                # A clean DLL disconnect is BROKEN_PIPE (109); anything else is
                # worth seeing. Swallowing every exception identically here made
                # a struct bug, a permissions failure and a normal Takion
                # restart all look like the same silent 500 ms reconnect loop.
                if getattr(e, "winerror", None) not in (109, 233):
                    log.warning("%s pipe error: %s", key.upper(), e)
            except Exception:
                log.exception("%s reader crashed", key.upper())
            finally:
                self.connected[key] = False
                if handle is not None:
                    try:
                        import win32file as _wf
                        _wf.CloseHandle(handle)
                    except Exception:
                        pass
                if self._running:
                    time.sleep(0.5)          # wait, then re-arm the pipe

    def _l1_loop(self) -> None:
        self._serve(L1_PIPE, L1.size, self._on_l1, "l1")

    def _l2_loop(self) -> None:
        self._serve(L2_PIPE, L2.size, self._on_l2, "l2")

    def _sweep_ts(self, marker_price: float) -> int:
        """Timestamp for a completed sweep, in epoch milliseconds.

        The DLL stamps the 'C' record's otherwise-unused price field with the
        instant it published the sweep. Preferring that to local receipt time
        removes the pipe's batching smear: the writer drains up to 4096 records
        per WriteFile, so a whole batch used to arrive carrying near-identical
        timestamps, and a batch spanning more than one bookmap column collapsed
        several distinct books into one.

        Both clocks are the same machine's wall clock, so this changes only the
        precision of a book's placement in time, never its timezone - the L1
        trade alignment in _align_ts() is unaffected.

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

    # ---- record handlers -------------------------------------------------
    def _on_l1(self, chunk: bytes, off: int) -> None:
        (sym_b, _o, _h, _l, last, bid, ask, cum_vol, time_ms,
         _pos, _bsz, _asz) = L1.unpack_from(chunk, off)
        sym = _cstr(sym_b)
        if not sym or not self._wanted(sym):
            return

        prev = self._last_vol.get(sym)
        self._last_vol[sym] = cum_vol
        if prev is None or cum_vol <= prev:
            return                              # first tick, or a volume reset
        size = int(cum_vol - prev)
        if size <= 0:
            return

        if last >= ask > 0:
            aggr = Aggressor.BUY
        elif 0 < last <= bid:
            aggr = Aggressor.SELL
        else:
            aggr = Aggressor.UNKNOWN

        raw = int(time_ms)
        tr = Trade(sym, float(last), size, aggr, self._align_ts(raw))

        # Withhold trades until the exchange-vs-local offset is known.
        #
        # _align_ts() returns the timestamp UN-SHIFTED while it is still
        # measuring, which places those trades hours from the book clock (ET vs
        # IST is 9.5 h). Emitting them created a bar far in the past at the head
        # of the series - and because VWAP and CVD are cumulative from bar 0,
        # that one bogus bar skewed both for the rest of the session.
        #
        # raw <= 0 has no offset to apply (to_epoch_ms already returned now), so
        # it is emitted straight through.
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
        side = side_b.decode("ascii", "ignore")

        if side == "C":                          # sweep complete
            bids = self._bids.pop(sym, {})
            asks = self._asks.pop(sym, {})
            if bids or asks:
                self._emit_book(BookSnapshot(sym, bids, asks,
                                             self._sweep_ts(price)))
        elif side == "B":
            d = self._bids.setdefault(sym, {})
            d[price] = d.get(price, 0) + size * self.lot_multiplier
        elif side == "A":
            d = self._asks.setdefault(sym, {})
            d[price] = d.get(price, 0) + size * self.lot_multiplier
