r"""Remote Takion feed — reads the broadcaster server over TCP.

Transport only. The decoding, clock alignment and aggressor classification are
shared with `PipeFeed` via `takion_decode`, so a remote client and a local one
produce byte-for-byte identical events. Keeping a second copy of that logic
here is how the two silently diverge - and they had already: 107 of this
module's 169 lines were duplicated from pipe_feed.

Wire framing (from Host_Omnitrix/broadcaster_server.py): one type byte
(\x01 = L1, \x02 = L2) followed by the raw Takion record.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

from .takion_decode import TakionDecoder, L1, L2

log = logging.getLogger("omnitrix.network")

_L1_TYPE = 1
_L2_TYPE = 2


class NetworkFeed(TakionDecoder):
    """TCP reader for a remote Takion broadcaster."""

    def __init__(self, host: str, port: int = 9999,
                 symbols: list[str] | None = None, lot_multiplier: int = 1,
                 token: str | None = None):
        super().__init__(symbols=symbols, lot_multiplier=lot_multiplier)
        self.host = host
        self.port = port
        # Matches OMNITRIX_TOKEN on the broadcaster. Empty = no auth, which is
        # what the server also defaults to.
        self.token = token if token is not None else os.environ.get(
            "OMNITRIX_TOKEN", "")
        self._thread: threading.Thread | None = None
        self.connected = {"network": False}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._network_loop, daemon=True)
        self._thread.start()

    def _network_loop(self) -> None:
        while self._running:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                log.info("Connecting to %s:%d ...", self.host, self.port)
                sock.connect((self.host, self.port))
                if self.token:
                    # Newline-terminated: the server reads one line before it
                    # sends anything.
                    sock.sendall(self.token.encode("utf-8") + b"\n")
                self.connected["network"] = True
                log.info("Connected to %s:%d", self.host, self.port)

                buf = bytearray()
                while self._running:
                    data = sock.recv(1 << 16)
                    if not data:
                        break
                    buf.extend(data)
                    self._drain(buf)

            except ConnectionRefusedError:
                log.warning("Connection refused to %s:%d", self.host, self.port)
            except Exception:
                log.exception("Network reader crashed")
            finally:
                self.connected["network"] = False
                try:
                    sock.close()
                except Exception:
                    pass
                if self._running:
                    time.sleep(1.0)

    def _drain(self, buf: bytearray) -> None:
        """Consume every whole framed record in `buf`, leaving any partial."""
        while buf:
            t = buf[0]
            if t == _L1_TYPE:
                n = L1.size
                handler = self._on_l1
            elif t == _L2_TYPE:
                n = L2.size
                handler = self._on_l2
            else:
                # TCP does not lose or reorder bytes, so a bad type means the
                # stream is genuinely desynchronised. Resync by scanning for
                # the next plausible type byte rather than clearing the buffer:
                # dropping everything buffered threw away the good records
                # sitting behind the bad byte as well.
                nxt = -1
                for i in range(1, len(buf)):
                    if buf[i] in (_L1_TYPE, _L2_TYPE):
                        nxt = i
                        break
                log.warning("bad frame type %d; resyncing (%d bytes dropped)",
                            t, len(buf) if nxt < 0 else nxt)
                if nxt < 0:
                    buf.clear()
                else:
                    del buf[:nxt]
                continue
            if len(buf) < 1 + n:
                return                       # partial record: wait for more
            handler(bytes(buf[1:1 + n]), 0)
            del buf[:1 + n]
