import asyncio
import logging
import os
import struct
import threading
import time
import socket
from dataclasses import replace

from .model import Trade, BookSnapshot, Aggressor
from .feed import Feed
from .pipe_feed import to_epoch_ms, L1, L2, _cstr, _TS_MAX_WAIT_S, _TS_MIN_SAMPLES, _TS_PENDING_MAX, _TS_SAMPLES, _DAY_MS, _MIN_EPOCH_MS

log = logging.getLogger("omnitrix.network")

class NetworkFeed(Feed):
    """TCP feed reader for live remote Takion extension."""

    def __init__(self, host: str, port: int = 9999, symbols: list[str] | None = None,
                 lot_multiplier: int = 1, token: str | None = None):
        super().__init__()
        self.host = host
        self.port = port
        # Matches OMNITRIX_TOKEN on the broadcaster. Empty/None = no auth, which
        # is what the server also defaults to.
        self.token = token if token is not None else os.environ.get(
            "OMNITRIX_TOKEN", "")
        self.symbols = {s.upper() for s in symbols} if symbols else None
        self.lot_multiplier = lot_multiplier
        self._thread: threading.Thread | None = None
        
        self._ts_offset: int | None = None
        self._ts_samples: list[int] = []
        self._ts_first_at: float | None = None
        self._pending: list[tuple[Trade, int]] = []
        
        self._last_vol: dict[str, int] = {}
        self._bids: dict[str, dict[float, int]] = {}
        self._asks: dict[str, dict[float, int]] = {}
        
        self.connected = {"network": False}
        self.sweep_clock: str | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._network_loop, daemon=True)
        self._thread.start()

    def _wanted(self, sym: str) -> bool:
        return self.symbols is None or sym in self.symbols

    def _align_ts(self, raw_ms: int) -> int:
        t = to_epoch_ms(raw_ms)
        if raw_ms <= 0:
            return t
        if self._ts_offset is None:
            now = time.time()
            if self._ts_first_at is None:
                self._ts_first_at = now
            self._ts_samples.append(int(now * 1000) - t)
            enough = len(self._ts_samples) >= _TS_SAMPLES
            waited = (len(self._ts_samples) >= _TS_MIN_SAMPLES
                      and now - self._ts_first_at >= _TS_MAX_WAIT_S)
            if not (enough or waited):
                return t
            self._ts_samples.sort()
            diff = self._ts_samples[len(self._ts_samples) // 2]
            quarter = 15 * 60 * 1000
            self._ts_offset = int(round(diff / quarter)) * quarter
            log.info("L1 clock offset locked at %+d min (from %d samples)",
                     self._ts_offset // 60000, len(self._ts_samples))
        return t + self._ts_offset

    def _sweep_ts(self, marker_price: float) -> int:
        if marker_price >= _MIN_EPOCH_MS:
            if self.sweep_clock is None:
                self.sweep_clock = "dll"
                log.info("sweep timestamps: from DLL (batch smear removed)")
            return int(marker_price)
        if self.sweep_clock is None:
            self.sweep_clock = "receipt"
        return int(time.time() * 1000)

    def _network_loop(self) -> None:
        while self._running:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                log.info(f"Connecting to {self.host}:{self.port}...")
                sock.connect((self.host, self.port))
                if self.token:
                    # Newline-terminated: the server reads one line before it
                    # sends anything.
                    sock.sendall(self.token.encode("utf-8") + b"\n")
                self.connected["network"] = True
                log.info(f"Connected to {self.host}:{self.port}")

                buf = bytearray()
                while self._running:
                    data = sock.recv(1 << 16)
                    if not data:
                        break
                    buf.extend(data)
                    
                    while True:
                        if not buf:
                            break
                        msg_type = buf[0]
                        if msg_type == 1:
                            if len(buf) < 1 + L1.size:
                                break
                            chunk = bytes(buf[1:1+L1.size])
                            self._on_l1(chunk, 0)
                            del buf[:1+L1.size]
                        elif msg_type == 2:
                            if len(buf) < 1 + L2.size:
                                break
                            chunk = bytes(buf[1:1+L2.size])
                            self._on_l2(chunk, 0)
                            del buf[:1+L2.size]
                        else:
                            # Bad type, resync by clearing buffer
                            log.warning(f"Unknown message type: {msg_type}, clearing buffer")
                            buf.clear()
                            break
                            
            except ConnectionRefusedError:
                log.warning(f"Connection refused to {self.host}:{self.port}")
            except Exception as e:
                log.exception("Network reader crashed")
            finally:
                self.connected["network"] = False
                sock.close()
                if self._running:
                    time.sleep(1.0)

    def _on_l1(self, chunk: bytes, off: int) -> None:
        (sym_b, _o, _h, _l, last, bid, ask, cum_vol, time_ms,
         _pos, _bsz, _asz) = L1.unpack_from(chunk, off)
        sym = _cstr(sym_b)
        if not sym or not self._wanted(sym):
            return

        prev = self._last_vol.get(sym)
        self._last_vol[sym] = cum_vol
        if prev is None or cum_vol <= prev:
            return
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

        if side == "C":
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
