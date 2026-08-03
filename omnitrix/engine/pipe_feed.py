r"""Live Takion feed — reads the C++ extension's two Windows named pipes.

Transport only. Every byte-level decision (record layout, clock alignment,
aggressor classification, sweep timestamps) lives in `takion_decode`, shared
with `NetworkFeed`; this file just supplies bytes.

This process is the pipe *server*: it creates the pipes and waits for the DLL
to connect, reconnecting automatically if Takion restarts.
"""

from __future__ import annotations

import logging
import threading
import time

from .takion_decode import (TakionDecoder, L1, L2, to_epoch_ms, _cstr,
                            _DAY_MS, _MIN_EPOCH_MS, _TS_SAMPLES,
                            _TS_MIN_SAMPLES, _TS_MAX_WAIT_S, _TS_PENDING_MAX)

log = logging.getLogger("omnitrix.pipe")

L1_PIPE = r"\\.\pipe\TakionOHLCV"
L2_PIPE = r"\\.\pipe\TakionData"


class PipeFeed(TakionDecoder):
    """Dual named-pipe reader for the live Takion extension."""

    def __init__(self, symbols: list[str] | None = None,
                 lot_multiplier: int = 1):
        super().__init__(symbols=symbols, lot_multiplier=lot_multiplier)
        self._threads: list[threading.Thread] = []
        self.connected = {"l1": False, "l2": False}

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for target in (self._l1_loop, self._l2_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

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
