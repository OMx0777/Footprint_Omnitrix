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
from . import wire

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
        # Sequence continuity. Present on TCP too, where it should never fire -
        # if it does, the stream was truncated and we want to know rather than
        # assume TCP made that impossible.
        self.gaps = wire.GapDetector()

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
                # Both record types share this ONE link, so a drop interrupts
                # both a sweep and the volume baseline. See TakionDecoder.
                self.on_disconnect()
                self.on_l1_disconnect()
                try:
                    sock.close()
                except Exception:
                    pass
                if self._running:
                    time.sleep(1.0)

    def _drain(self, buf: bytearray) -> None:
        """Consume every whole framed record in `buf`, leaving any partial.

        Accepts BOTH framings. A batch header means a sequenced server; a bare
        type byte means the legacy stream. Supporting both is what lets the
        server be upgraded before all 100 clients are, and vice versa - a flag
        day across that many machines is not a deployment plan.
        """
        while buf:
            hdr = wire.decode_header(buf)
            if hdr is not None:
                channel, count, seq = hdr
                # Wait for the WHOLE batch before consuming any of it: applied
                # in halves it would publish a partial book.
                end = self._batch_end(buf, count)
                if end < 0:
                    return
                missed = self.gaps.observe(channel, seq)
                if missed:
                    self._on_gap(channel, missed)
                body = bytearray(buf[wire.HEADER_SIZE:end])
                del buf[:end]
                self._drain_records(body)
                continue
            if buf[0] == wire.MAGIC and len(buf) < wire.HEADER_SIZE:
                # A batch header split across two TCP reads. decode_header
                # cannot tell "not a header" from "not enough bytes yet", and
                # the legacy fallback below would eat the magic byte as a
                # record type and resync past it - discarding a header, and
                # with it the sequence number that proves nothing was lost.
                # Legacy records are type 1 or 2, never 0x4F, so this cannot
                # stall an unsequenced stream.
                return
            if not self._drain_one(buf):
                return

    def _batch_end(self, buf: bytearray, count: int) -> int:
        """Offset just past `count` records, or -1 if they have not all arrived."""
        off = wire.HEADER_SIZE
        for _ in range(count):
            if off >= len(buf):
                return -1
            t = buf[off]
            n = L1.size if t == _L1_TYPE else L2.size if t == _L2_TYPE else -1
            if n < 0:
                log.warning("bad record type %d inside a framed batch; "
                            "discarding the batch header", t)
                return wire.HEADER_SIZE
            if off + 1 + n > len(buf):
                return -1
            off += 1 + n
        return off

    def _on_gap(self, channel: int, missed: int) -> None:
        """A batch never arrived. Drop whatever it invalidated.

        Same response as a link drop, for the same reason: the book merge is
        additive and a sweep is only published on its 'C' marker, so a hole
        leaves half-assembled depth that would be merged into the next sweep
        and drawn as liquidity that is not there. Clearing makes the gap an
        honest absence instead of a phantom wall.
        """
        log.warning("feed gap: %d batch(es) missing on channel %d "
                    "(%d gaps, %d batches lost this session)",
                    missed, channel, self.gaps.gaps, self.gaps.lost)
        if channel == wire.CH_L2:
            self.on_disconnect()
        else:
            self.on_l1_disconnect()

    def _drain_records(self, buf: bytearray) -> None:
        """Drain a batch body, which contains records and no headers."""
        while buf and self._drain_one(buf):
            pass

    def _drain_one(self, buf: bytearray) -> bool:
        """Consume one record. False means 'need more bytes' - stop draining."""
        t = buf[0]
        if t == _L1_TYPE:
            n, handler = L1.size, self._on_l1
        elif t == _L2_TYPE:
            n, handler = L2.size, self._on_l2
        else:
            # TCP does not lose or reorder bytes, so a bad type means the
            # stream is genuinely desynchronised. Resync by scanning for the
            # next plausible type byte rather than clearing the buffer:
            # dropping everything buffered threw away the good records sitting
            # behind the bad byte as well.
            nxt = -1
            for i in range(1, len(buf)):
                if buf[i] in (_L1_TYPE, _L2_TYPE):
                    nxt = i
                    break
            log.warning("bad frame type %d; resyncing (%d bytes dropped)",
                        t, len(buf) if nxt < 0 else nxt)
            if nxt < 0:
                buf.clear()
                return False
            del buf[:nxt]
            return True
        if len(buf) < 1 + n:
            return False                     # partial record: wait for more
        handler(bytes(buf[1:1 + n]), 0)
        del buf[:1 + n]
        return True
