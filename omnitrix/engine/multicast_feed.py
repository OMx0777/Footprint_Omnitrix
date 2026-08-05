"""UDP multicast client feed, with TCP backfill and gap recovery.

WHY MULTICAST. Measured: the feed is 1.59 MB/s, and unicasting it to 100 LAN
clients is 1.27 Gbit/s - more than a 1 GbE link can carry. Multicast sends one
copy and lets the switch replicate it, so the server's egress is 1.59 MB/s
whatever the client count. That is the only way 100 desks fit on gigabit.

WHY THAT IS DANGEROUS WITHOUT THE REST OF THIS FILE. The L2 stream is stateful:
a book is assembled from per-level records, published on its 'C' marker, and
merged ADDITIVELY. Lose one datagram and nothing fails loudly - a level that
was pulled reads as still resting, indefinitely. That is a phantom wall on the
heatmap, which is the exact class of false data this codebase exists to refuse.

So every datagram is sequenced (see wire.py), every gap is detected, and every
gap is repaired by replaying the exact missing bytes over TCP. Not a snapshot,
not an approximation - the same bytes that were lost.

THE JOIN ORDER MATTERS, and getting it wrong leaves a hole that nothing later
can detect:

    WRONG   backfill to sequence N, then join the group
            -> everything published between N and the join is simply gone, and
               the first live datagram looks perfectly continuous because the
               client never saw the sequence before it

    RIGHT   join the group FIRST and buffer what arrives, then backfill up to
            the first buffered sequence, then drain the buffer
            -> the two ranges meet exactly, and if they do not, the gap
               detector says so

That is what start() does, in that order, deliberately.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from . import wire
from .takion_decode import TakionDecoder, L1, L2

log = logging.getLogger(__name__)


def local_interfaces() -> list[str]:
    """Every IPv4 address this host could join a group on."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def pick_interface(peer_ip: str) -> str:
    """The local address on the same subnet as `peer_ip`, or 0.0.0.0.

    Rolling out to 100 desks by hand-editing an interface address on each one
    is a hundred chances to get it wrong, and getting it wrong produces a chart
    that silently never updates. The server's address is already known, and the
    right interface is simply the local one that can reach it - so derive it.

    Matching on the first three octets is a /24 test, which is narrower than
    this site's actual /23. That is deliberate: a wrong match here sends the
    join out the wrong adapter, so the test errs toward returning 0.0.0.0 and
    letting the OS decide rather than confidently choosing something bogus.
    Virtual adapters (172.x from Hyper-V and WSL) never match a 192.168.x
    server, which is exactly the case this exists to disarm.
    """
    if not peer_ip:
        return "0.0.0.0"
    want = peer_ip.rsplit(".", 1)[0]
    for ip in local_interfaces():
        if ip.startswith("127."):
            continue
        if ip.rsplit(".", 1)[0] == want:
            return ip
    return "0.0.0.0"


def check_interface(iface: str) -> str:
    """Warn about the two interface mistakes that produce a silent dead feed.

    MEASURED on Windows: joining a group on 127.0.0.1 receives NOTHING - zero
    of twenty datagrams - while 0.0.0.0 and the real LAN address both work. It
    does not error; it simply never delivers, which looks exactly like a server
    that is not publishing.

    The second trap is a multi-homed machine. A box running Hyper-V, WSL or
    VMware has extra adapters (172.x here), and with 0.0.0.0 the OS picks the
    interface by routing metric - which can be a virtual adapter no other desk
    is on. Naming the LAN interface explicitly is the fix, and on a server with
    virtual adapters it is not optional.
    """
    if iface == "127.0.0.1":
        return ("iface=127.0.0.1 will receive nothing: multicast is not "
                "delivered over loopback on Windows. Use 0.0.0.0 or the LAN "
                "address.")
    ips = [i for i in local_interfaces() if not i.startswith("127.")]
    virt = [i for i in ips if i.startswith(("172.1", "172.2", "172.3", "192.168.56."))]
    if iface == "0.0.0.0" and virt and len(ips) > 1:
        return (f"iface=0.0.0.0 on a multi-homed host {ips}: the OS may bind "
                f"the group to a virtual adapter ({', '.join(virt)}) that no "
                f"other desk can see. Set OMNITRIX_MCAST_IF to the LAN address.")
    return ""

_L1_TYPE = 1
_L2_TYPE = 2

# Kernel receive buffer. The default is tens of kilobytes; a burst that
# overflows it is dropped by the OS before Python ever sees it, and shows up
# only as a sequence gap. 8 MB is ~5 seconds of the full feed.
RCVBUF = 8 << 20

# How long a gap may go unrepaired before we ask for it. Batching a moment's
# worth of gaps into one request avoids hammering the replay server when a
# burst of loss produces many small holes.
REPAIR_DELAY_S = 0.35


class MulticastFeed(TakionDecoder):
    """Live multicast stream + TCP backfill/recovery."""

    def __init__(self, group: str = "239.7.7.7", port: int = 9997,
                 iface: str = "0.0.0.0",
                 replay_host: str = "", replay_port: int = 9998,
                 backfill_seconds: float = 0.0,
                 symbols: list[str] | None = None, lot_multiplier: int = 1,
                 token: str = ""):
        super().__init__(symbols=symbols, lot_multiplier=lot_multiplier)
        self.group = group
        self.port = port
        self.iface = iface
        self.replay_host = replay_host
        self.replay_port = replay_port
        self.backfill_seconds = backfill_seconds
        self.token = token
        self.gaps = wire.GapDetector()
        self.connected = {"multicast": False, "replay": False}
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._pending: list[tuple[int, int, int]] = []   # (ch, from, to)
        self._pending_lock = threading.Lock()
        self.repairs = 0
        self.repair_bytes = 0
        self.unrepaired = 0

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            log.warning("could not raise SO_RCVBUF; bursts may be dropped by "
                        "the kernel and appear as sequence gaps")
        warn = check_interface(self.iface)
        if warn:
            log.warning("multicast interface: %s", warn)
        s.bind(("", self.port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.group),
                           socket.inet_aton(self.iface))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.settimeout(1.0)
        return s

    # ---- main loop -------------------------------------------------------
    def _run(self) -> None:
        try:
            self._sock = self._open_socket()
            self.connected["multicast"] = True
            log.info("joined %s:%d on %s", self.group, self.port, self.iface)
        except OSError:
            log.exception("could not join the multicast group")
            self.connected["multicast"] = False
            return

        # JOIN FIRST, buffer, THEN backfill - see the module docstring.
        buffered: list[bytes] = []
        first_seq: dict[int, int] = {}
        t_end = time.perf_counter() + 1.0
        while self._running and time.perf_counter() < t_end:
            dg = self._recv()
            if dg is None:
                continue
            hdr = wire.decode_header(dg)
            if hdr is None:
                continue
            ch, _count, seq = hdr
            first_seq.setdefault(ch, seq)
            buffered.append(dg)

        if self.replay_host and first_seq:
            self._backfill(first_seq)

        for dg in buffered:
            self._on_datagram(dg)
        buffered.clear()

        last_repair = time.perf_counter()
        while self._running:
            dg = self._recv()
            if dg is not None:
                self._on_datagram(dg)
            now = time.perf_counter()
            if now - last_repair >= REPAIR_DELAY_S:
                last_repair = now
                self._repair_pending()

    def _recv(self):
        try:
            return self._sock.recv(65535)
        except socket.timeout:
            return None
        except OSError:
            if self._running:
                log.warning("multicast socket error")
            return None

    def _on_datagram(self, dg: bytes) -> None:
        hdr = wire.decode_header(dg)
        if hdr is None:
            return                      # not ours; multicast groups are shared
        ch, count, seq = hdr
        missed = self.gaps.observe(ch, seq)
        if missed:
            # Queue the exact range for repair. The book is NOT invalidated
            # here - the repair is about to restore precisely those bytes, and
            # throwing the book away first would discard state the replay is
            # going to rebuild on top of.
            with self._pending_lock:
                self._pending.append((ch, seq - missed, seq - 1))
            log.warning("multicast gap: ch%d missing %d..%d (%d batches)",
                        ch, seq - missed, seq - 1, missed)
        body = bytearray(dg[wire.HEADER_SIZE:])
        self._drain_records(body)

    # ---- record framing (same rules as the TCP client) -------------------
    def _drain_records(self, buf: bytearray) -> None:
        while buf:
            t = buf[0]
            if t == _L1_TYPE:
                n, handler = L1.size, self._on_l1
            elif t == _L2_TYPE:
                n, handler = L2.size, self._on_l2
            else:
                # Inside a datagram this cannot be a framing slip - a datagram
                # is delivered whole or not at all - so it is a foreign packet
                # on the group. Drop the rest of it rather than guess.
                return
            if len(buf) < 1 + n:
                return
            handler(bytes(buf[1:1 + n]), 0)
            del buf[:1 + n]

    # ---- backfill and repair --------------------------------------------
    def _replay_socket(self):
        s = socket.create_connection((self.replay_host, self.replay_port),
                                     timeout=20.0)
        if self.token:
            s.sendall(self.token.encode("utf-8") + b"\n")
        return s

    def _request(self, s, line: str) -> bytes:
        s.sendall(line.encode("utf-8") + b"\n")
        head = bytearray()
        while not head.endswith(b"\n"):
            c = s.recv(1)
            if not c:
                return b""
            head.extend(c)
        txt = head.decode("utf-8", "ignore").strip()
        if not txt.startswith("LEN "):
            return b""
        n = int(txt.split()[1])
        out = bytearray()
        while len(out) < n:
            chunk = s.recv(min(1 << 20, n - len(out)))
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    def _backfill(self, first_seq: dict[int, int]) -> None:
        """Fetch history up to the first sequence we already hold.

        The upper bound is `first_live - 1` on each channel, so the replayed
        range and the buffered live range meet exactly - no hole, and nothing
        applied twice.
        """
        try:
            s = self._replay_socket()
            self.connected["replay"] = True
        except OSError as e:
            log.warning("no replay server at %s:%d (%s); starting without "
                        "history", self.replay_host, self.replay_port, e)
            self.connected["replay"] = False
            return
        try:
            for ch, live_from in sorted(first_seq.items()):
                lo = 0
                if self.backfill_seconds > 0:
                    # Sequence numbers are not time, so an exact "last N
                    # minutes" needs the server's help. Until that exists,
                    # asking from 0 and letting the server cap the reply is
                    # honest and bounded - it returns the most it will serve.
                    lo = 0
                data = self._request(s, f"REPLAY {ch} {lo} {live_from - 1}")
                if not data:
                    continue
                log.info("backfill ch%d: %.1f MB up to seq %d",
                         ch, len(data) / 1e6, live_from - 1)
                self._apply_replay(data, ch)
            try:
                s.sendall(b"BYE\n")
            except OSError:
                pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _apply_replay(self, data: bytes, channel: int) -> None:
        """Feed replayed batches through the same path as live ones.

        Sequence numbers are NOT fed to the gap detector here: the detector
        tracks the LIVE stream's continuity, and replaying an older range
        would look like a huge reordering and reset its high-water mark.
        """
        buf = bytearray(data)
        while buf:
            hdr = wire.decode_header(buf)
            if hdr is None:
                return
            ch, count, seq = hdr
            end = self._batch_end(buf, count)
            if end < 0:
                return
            body = bytearray(buf[wire.HEADER_SIZE:end])
            del buf[:end]
            self._drain_records(body)

    @staticmethod
    def _batch_end(buf: bytearray, count: int) -> int:
        off = wire.HEADER_SIZE
        for _ in range(count):
            if off >= len(buf):
                return -1
            t = buf[off]
            n = L1.size if t == _L1_TYPE else L2.size if t == _L2_TYPE else -1
            if n < 0 or off + 1 + n > len(buf):
                return -1
            off += 1 + n
        return off

    def _repair_pending(self) -> None:
        with self._pending_lock:
            if not self._pending:
                return
            todo, self._pending = self._pending, []
        if not self.replay_host:
            # Nothing can repair it, so the state it invalidated must go.
            # An honest hole beats a phantom wall.
            self.unrepaired += len(todo)
            for ch, _a, _b in todo:
                self.on_disconnect() if ch == wire.CH_L2 else self.on_l1_disconnect()
            return
        try:
            s = self._replay_socket()
        except OSError:
            self.unrepaired += len(todo)
            for ch, _a, _b in todo:
                self.on_disconnect() if ch == wire.CH_L2 else self.on_l1_disconnect()
            log.warning("gap repair unavailable; dropped book state for %d gap(s)",
                        len(todo))
            return
        try:
            for ch, a, b in todo:
                data = self._request(s, f"REPLAY {ch} {a} {b}")
                if data:
                    self._apply_replay(data, ch)
                    self.repairs += 1
                    self.repair_bytes += len(data)
                    log.info("repaired ch%d %d..%d (%.0f kB)",
                             ch, a, b, len(data) / 1e3)
                else:
                    # The server could not supply it - too old, or never
                    # recorded. The book cannot be trusted, so it goes.
                    self.unrepaired += 1
                    (self.on_disconnect() if ch == wire.CH_L2
                     else self.on_l1_disconnect())
                    log.warning("ch%d %d..%d could not be replayed; dropped "
                                "book state rather than keep stale depth", ch, a, b)
            try:
                s.sendall(b"BYE\n")
            except OSError:
                pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    def health(self) -> dict:
        st = self.gaps.stats()
        st.update({"repairs": self.repairs, "unrepaired": self.unrepaired,
                   "repair_bytes": self.repair_bytes})
        return st
