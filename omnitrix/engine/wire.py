"""Sequenced batch framing, shared by the broadcaster and every client.

WHY THIS EXISTS. The feed is about to be served to ~100 LAN clients, and the
measured egress makes the transport decision for us:

    feed rate                          1.59 MB/s
    100 clients, one copy each       158.8 MB/s  = 1.27 Gbit/s   over 1 GbE
    100 clients, multicast             1.59 MB/s = 0.013 Gbit/s

So the copies have to stop, which means multicast, which means UDP - and UDP is
where this codebase's central rule gets dangerous.

THE TRAP. The L2 stream is STATEFUL: a book is assembled from per-level records
and only published on its 'C' marker, and the merge is additive. Drop one UDP
datagram and the book does not fail loudly - it silently reports a level that
was pulled as still resting, forever, until something else happens to overwrite
it. That is a phantom wall on the heatmap, which is precisely the false data
this project spent months removing (see TakionDecoder.on_disconnect). TCP hides
this problem by never losing anything; UDP hands it straight to the renderer.

So the transport cannot be changed first. What has to exist first is the
ability to KNOW a message was lost, and this module is that: every batch
carries a monotonic sequence number, so a receiver can tell exactly how many
batches went missing and react honestly instead of drawing a corrupted book.

Sequencing per BATCH rather than per record is deliberate. A per-record
sequence would add 8 bytes to a 33-byte L2 record - a 24% bandwidth tax on the
one thing we are trying to shrink - while a batch header is amortised over
however many records travelled together. On UDP one datagram is one batch, so
the batch sequence IS the datagram sequence, which is what a receiver needs.

The format is defined ONCE, here, and imported by both sides. Two copies of a
wire format is two formats that will eventually disagree.
"""

from __future__ import annotations

import struct

MAGIC = 0x4F                      # 'O'
VERSION = 1

# magic(1) version(1) channel(1) count(2) seq(8)
HEADER = struct.Struct("<BBBHQ")
HEADER_SIZE = HEADER.size         # 13

# Channels are sequenced INDEPENDENTLY so a gap on one does not implicate the
# other: losing depth must not throw away good trade data.
CH_L1 = 1
CH_L2 = 2

# A datagram must fit one Ethernet frame or the IP layer fragments it, and a
# fragmented datagram is lost entirely if ANY fragment is lost - which converts
# one dropped packet into a much larger hole. 1400 leaves room for IP+UDP
# headers inside a 1500-byte MTU without relying on path MTU discovery.
MAX_DATAGRAM = 1400
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_SIZE


def encode(channel: int, seq: int, payload: bytes, count: int) -> bytes:
    """One framed batch. `count` is the number of records inside `payload`."""
    return HEADER.pack(MAGIC, VERSION, channel, count, seq & 0xFFFFFFFFFFFFFFFF) + payload


def split_payload(records: list[bytes]) -> list[tuple[bytes, int]]:
    """Pack whole records into datagram-sized payloads.

    Records are never split across datagrams. A half record in a lost datagram
    would desynchronise the receiver's parse, turning one lost batch into a
    resync - and the resync would silently discard good records sitting behind
    the damaged one.
    """
    out: list[tuple[bytes, int]] = []
    buf = bytearray()
    n = 0
    for rec in records:
        if len(rec) > MAX_PAYLOAD:
            # Cannot happen with the current 105/33-byte records, but a future
            # record type that does not fit must fail loudly here rather than
            # be silently truncated onto the wire.
            raise ValueError(f"record of {len(rec)} B exceeds the {MAX_PAYLOAD} B "
                             f"datagram payload; the wire format needs a "
                             f"fragmentation scheme before such a record ships")
        if len(buf) + len(rec) > MAX_PAYLOAD:
            out.append((bytes(buf), n))
            buf.clear()
            n = 0
        buf.extend(rec)
        n += 1
    if buf:
        out.append((bytes(buf), n))
    return out


class GapDetector:
    """Tracks per-channel sequence continuity for one receiver.

    Reports gaps; it deliberately does NOT try to repair them. Repair means
    either buffering out-of-order arrivals (which delays every good message to
    wait for one that may never come) or requesting retransmission (which needs
    the recovery channel). What the renderer needs first is simply to be told,
    so it can drop the state it can no longer trust instead of drawing it.
    """

    __slots__ = ("expected", "gaps", "lost", "reordered", "duplicates")

    def __init__(self) -> None:
        self.expected: dict[int, int] = {}
        self.gaps = 0            # discontinuities observed
        self.lost = 0            # batches missing, summed
        self.reordered = 0       # arrived older than expected (UDP only)
        self.duplicates = 0

    def observe(self, channel: int, seq: int) -> int:
        """Feed one batch's sequence. Returns how many batches were MISSED.

        0 means the stream is continuous. Anything else means the receiver's
        book state for that channel is no longer trustworthy.
        """
        exp = self.expected.get(channel)
        self.expected[channel] = seq + 1
        if exp is None:
            return 0                     # first batch on this channel
        if seq == exp:
            return 0
        if seq < exp:
            # Out of order or a duplicate. Neither loses data by itself, but a
            # reordered batch applied late would replay stale depth over newer
            # depth, so it is counted and reported.
            if seq == exp - 1:
                self.duplicates += 1
            else:
                self.reordered += 1
            self.expected[channel] = exp  # keep the high-water mark
            return 0
        missed = seq - exp
        self.gaps += 1
        self.lost += missed
        return missed

    def stats(self) -> dict:
        return {"gaps": self.gaps, "lost": self.lost,
                "reordered": self.reordered, "duplicates": self.duplicates}


def decode_header(buf: bytes, off: int = 0):
    """(channel, count, seq) or None if `buf` does not start with a valid header.

    Returning None rather than raising lets a receiver fall back to the legacy
    unsequenced framing, which is what makes a mixed-version rollout safe.
    """
    if len(buf) - off < HEADER_SIZE:
        return None
    magic, ver, channel, count, seq = HEADER.unpack_from(buf, off)
    if magic != MAGIC or ver != VERSION:
        return None
    return channel, count, seq
