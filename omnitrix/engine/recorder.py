"""Capture and replay of the raw event stream.

`Recorder` taps any Feed and writes every Trade / BookSnapshot to a compact
binary file; `ReplayFeed` plays that file back through the same Feed interface,
at real speed or accelerated. Together they close the research loop: capture a
live Takion session once, then replay it as many times as you like with every
chart, profile and signal behaving exactly as it did live.

File format (little-endian, streamed):
    magic  b"OMNI\x01"
    trade  b'T' | sym[8] | price f64 | size u32 | aggr u8 | ts_ms u64
    book   b'B' | sym[8] | ts_ms u64 | n_bid u16 | n_ask u16
                | n_bid × (price f64, size u32)
                | n_ask × (price f64, size u32)
"""

from __future__ import annotations

import struct
import threading
import time

from .model import Trade, BookSnapshot, Aggressor
from .feed import Feed

MAGIC = b"OMNI\x01"
_T = struct.Struct("<c8sdIBQ")          # trade record
_BH = struct.Struct("<c8sQHH")          # book header
_LV = struct.Struct("<dI")              # one book level

_AGGR_ID = {Aggressor.BUY: 0, Aggressor.SELL: 1, Aggressor.UNKNOWN: 2}
_ID_AGGR = {0: Aggressor.BUY, 1: Aggressor.SELL, 2: Aggressor.UNKNOWN}


def _sym8(s: str) -> bytes:
    return s.encode("ascii", "ignore")[:8].ljust(8, b"\0")


def _unsym(b: bytes) -> str:
    return b.split(b"\0")[0].decode("ascii", "ignore")


class Recorder:
    """Taps a feed's emit path so recording is transparent to the app."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "wb")
        self._f.write(MAGIC)
        self._lock = threading.Lock()
        self.trades = 0
        self.books = 0
        self._detach = None

    def attach(self, feed: Feed) -> None:
        """Wrap the feed's current callbacks — call AFTER the app registers."""
        prev_t, prev_b = feed._on_trade, feed._on_book

        def on_trade(tr):
            self.write_trade(tr)
            if prev_t:
                prev_t(tr)

        def on_book(bk):
            self.write_book(bk)
            if prev_b:
                prev_b(bk)

        feed._on_trade = on_trade
        feed._on_book = on_book
        self._detach = lambda: (setattr(feed, "_on_trade", prev_t),
                                setattr(feed, "_on_book", prev_b))

    def write_trade(self, tr: Trade) -> None:
        buf = _T.pack(b"T", _sym8(tr.symbol), tr.price, int(tr.size),
                      _AGGR_ID[tr.aggressor], int(tr.ts_ms))
        with self._lock:
            self._f.write(buf)
            self.trades += 1

    def write_book(self, bk: BookSnapshot) -> None:
        bids = list(bk.bids.items())
        asks = list(bk.asks.items())
        parts = [_BH.pack(b"B", _sym8(bk.symbol), int(bk.ts_ms),
                          len(bids), len(asks))]
        for p, s in bids:
            parts.append(_LV.pack(p, int(s)))
        for p, s in asks:
            parts.append(_LV.pack(p, int(s)))
        with self._lock:
            self._f.write(b"".join(parts))
            self.books += 1

    def close(self) -> None:
        if self._detach:
            self._detach()
            self._detach = None
        with self._lock:
            if not self._f.closed:
                self._f.flush()
                self._f.close()


def read_events(path: str):
    """Yield ('T', Trade) / ('B', BookSnapshot) in recorded order."""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path}: not an Omnitrix capture file")
        while True:
            kind = f.read(1)
            if not kind:
                return
            if kind == b"T":
                rest = f.read(_T.size - 1)
                if len(rest) < _T.size - 1:
                    return
                _, sym, price, size, aggr, ts = _T.unpack(kind + rest)
                yield "T", Trade(_unsym(sym), price, size,
                                 _ID_AGGR.get(aggr, Aggressor.UNKNOWN), ts)
            elif kind == b"B":
                rest = f.read(_BH.size - 1)
                if len(rest) < _BH.size - 1:
                    return
                _, sym, ts, nb, na = _BH.unpack(kind + rest)
                need = (nb + na) * _LV.size
                blob = f.read(need)
                if len(blob) < need:
                    return
                bids, asks = {}, {}
                off = 0
                for i in range(nb + na):
                    p, s = _LV.unpack_from(blob, off)
                    off += _LV.size
                    (bids if i < nb else asks)[p] = s
                yield "B", BookSnapshot(_unsym(sym), bids, asks, ts)
            else:
                return                      # corrupt / truncated tail


class ReplayFeed(Feed):
    """Replays a capture file through the standard Feed interface.

    speed = 1.0 replays in real time; higher is faster; 0 = as fast as
    possible (useful for rebuilding a full session instantly).
    """

    def __init__(self, path: str, speed: float = 0.0, loop: bool = False):
        super().__init__()
        self.path = path
        self.speed = speed
        self.loop = loop
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            t0_wall = time.time()
            t0_evt = None
            for kind, ev in read_events(self.path):
                if not self._running:
                    return
                if self.speed > 0:
                    if t0_evt is None:
                        t0_evt = ev.ts_ms
                    target = (ev.ts_ms - t0_evt) / 1000.0 / self.speed
                    lag = target - (time.time() - t0_wall)
                    if lag > 0:
                        time.sleep(min(lag, 0.25))
                if kind == "T":
                    self._emit_trade(ev)
                else:
                    self._emit_book(ev)
            if not self.loop:
                return
