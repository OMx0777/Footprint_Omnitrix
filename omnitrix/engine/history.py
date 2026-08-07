"""Fetch one symbol's history from the replay server, off the GUI thread.

WHY A THREAD AT ALL. The last attempt at backfill did this work inline on the
receive thread with an unbounded range: it replayed a whole session, 1.3 GB of
L2 after fifteen minutes, froze the terminal and lost seventy datagrams while
it was busy. It was removed rather than fixed. This is the same feature done
the other way round - a worker that owns the socket and the decode, and hands
the GUI thread finished objects through a signal.

THREE THINGS IT WILL NOT DO, each of which is why the old one failed:

  * it will not ask for an open-ended range. The caller gives a window, the
    server resolves it to sequence bounds, and MAX_BATCH_BYTES caps the reply;

  * it will not touch a buffer, a series or a widget. It emits, and the GUI
    thread decides what to do on its own clock. Nothing here is allowed to
    know what a chart is;

  * it will not filter client-side. The server drops other symbols' records
    before they reach the wire, so a thousand-symbol recording does not cross
    the LAN to have 99.9% of it discarded here.

THE RESULT IS HISTORY, NOT LIVE DATA. Server-side filtering makes the reply
deliberately non-contiguous in sequence, so it must never be fed to the gap
detector - it is folded into bars that already exist. See
BarSeries.rebuild_footprint, which writes cells and refuses to touch any
figure that was already counted.
"""

from __future__ import annotations

import logging
import socket
import time

from PyQt6.QtCore import QThread, pyqtSignal

from . import wire
from .takion_decode import TakionDecoder, L1, L2
from .model import Trade, BookSnapshot

log = logging.getLogger(__name__)

# A reply is capped so one request cannot become the old unbounded backfill by
# another name. 32 MB of L1 is several hours of one symbol's prints.
MAX_REPLY_BYTES = 32 << 20
# The server is on the same switch. If it has not answered in this long it is
# not going to, and a fetch that hangs is a feature the user will stop using.
TIMEOUT_S = 8.0


class HistoryFetcher(QThread):
    """One request, one thread, one signal. Started per fetch, not pooled.

    A pool would need a queue, a cancel protocol and a shutdown path, for an
    operation the user triggers by hand a few times a minute. A thread that
    runs once and ends is fewer moving parts than any of that.
    """

    # symbol, trades, books, report-dict
    ready = pyqtSignal(str, list, list, dict)
    failed = pyqtSignal(str, str)

    def __init__(self, host: str, port: int, token: str, symbol: str,
                 start_ms: int, end_ms: int, channels=(1, 2), parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.symbol = (symbol or "").strip().upper()
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.channels = tuple(channels)
        self._l1_off = None
        self._stop = False

    def cancel(self) -> None:
        """Ask the worker to stop. Checked between steps, not mid-recv."""
        self._stop = True

    # ---- socket ----------------------------------------------------------
    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=TIMEOUT_S)
        if self.token:
            s.sendall(self.token.encode("utf-8") + b"\n")
        return s

    @staticmethod
    def _line(s) -> str:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            c = s.recv(1)
            if not c:
                break
            buf.extend(c)
        return buf.decode("utf-8", "ignore").strip()

    def _ask(self, s, line: str) -> str:
        s.sendall(line.encode("utf-8") + b"\n")
        return self._line(s)

    def _replay(self, s, line: str) -> bytes:
        s.sendall(line.encode("utf-8") + b"\n")
        txt = self._line(s)
        if not txt.startswith("LEN "):
            raise RuntimeError(txt or "no reply")
        n = int(txt.split()[1])
        if n > MAX_REPLY_BYTES:
            raise RuntimeError(f"reply {n:,} B exceeds the {MAX_REPLY_BYTES:,} cap")
        out = bytearray()
        while len(out) < n:
            chunk = s.recv(min(1 << 20, n - len(out)))
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    # ---- the worker ------------------------------------------------------
    def run(self) -> None:
        t0 = time.perf_counter()
        trades: list[Trade] = []
        books: list[BookSnapshot] = []
        rep = {"symbol": self.symbol, "bytes": 0, "batches": 0}
        try:
            if not self.symbol:
                raise RuntimeError("no symbol")
            s = self._connect()
            try:
                ans = self._ask(s, "L1OFF -")
                if ans.startswith("OK "):
                    self._l1_off = int(ans.split()[1])
                for ch in self.channels:
                    if self._stop:
                        break
                    # THE SERVER RESOLVES THE TIMESTAMPS. Estimating a
                    # sequence from a rate fetches the wrong window and
                    # nothing in the reply would show it - volume is not
                    # linear in time.
                    ans = self._ask(s, f"RANGE {ch} {self.start_ms} {self.end_ms}")
                    if not ans.startswith("OK "):
                        log.info("history: no range for ch%d (%s)", ch, ans)
                        continue
                    lo, hi = (int(x) for x in ans.split()[1:3])
                    if hi < lo:
                        continue
                    raw = self._replay(
                        s, f"REPLAY {ch} {lo} {hi} - {self.symbol}")
                    rep["bytes"] += len(raw)
                    if raw:
                        rep["batches"] += self._decode(raw, trades, books)
            finally:
                try:
                    s.sendall(b"BYE\n")
                except OSError:
                    pass
                s.close()
        except Exception as e:                       # noqa: BLE001
            log.info("history fetch failed for %s: %s", self.symbol, e)
            self.failed.emit(self.symbol, str(e))
            return
        rep["trades"] = len(trades)
        rep["books"] = len(books)
        rep["ms"] = (time.perf_counter() - t0) * 1000
        self.ready.emit(self.symbol, trades, books, rep)

    def _decode(self, raw: bytes, trades: list, books: list) -> int:
        """Framed bytes -> model objects, HERE, on the worker.

        Decoding on the GUI thread would move the cost rather than remove it:
        a few hundred thousand records is tens of milliseconds, the whole
        frame budget. The decoder is a PRIVATE instance - it carries
        per-symbol state (last cumulative volume, the partial book) and
        sharing the live one would corrupt the live feed with replayed data.
        """
        dec = TakionDecoder(symbols=[self.symbol])
        # THE CLOCK OFFSET COMES FROM THE SERVER. L1 carries ms-since-midnight
        # in the exchange's timezone and the live decoder recovers the offset
        # by comparing arrivals against local wall clock. A replay calibrating
        # hours-old data against the clock right now would place every trade
        # at the wrong instant - and therefore in the wrong bar, where
        # rebuild_footprint would either skip it or fill the wrong cells.
        if self._l1_off is not None:
            dec._ts_offset = int(self._l1_off)
        dec.on_trade(trades.append)
        dec.on_book(books.append)
        n = 0
        off = 0
        end = len(raw)
        while off < end:
            hdr = wire.decode_header(raw[off:off + wire.HEADER_SIZE])
            if hdr is None:
                break
            _ch, count, _seq = hdr
            size = _batch_len(raw, off, count)
            if size < 0:
                break
            # Record by record, the same framing rule the live paths use.
            p = off + wire.HEADER_SIZE
            for _ in range(count):
                t = raw[p]
                if t == 1:
                    dec._on_l1(raw[p + 1:p + 1 + L1.size], 0)
                    p += 1 + L1.size
                elif t == 2:
                    dec._on_l2(raw[p + 1:p + 1 + L2.size], 0)
                    p += 1 + L2.size
                else:
                    break
            off += size
            n += 1
        return n


def _batch_len(buf: bytes, off: int, count: int) -> int:
    """Total framed length of the batch at `off`, or -1 if truncated."""
    from .takion_decode import L1, L2
    p = off + wire.HEADER_SIZE
    for _ in range(count):
        if p >= len(buf):
            return -1
        t = buf[p]
        n = L1.size if t == 1 else L2.size if t == 2 else -1
        if n < 0 or p + 1 + n > len(buf):
            return -1
        p += 1 + n
    return p - off
