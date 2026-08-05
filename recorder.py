"""Append the raw framed stream to disk, so history can be replayed later.

WHAT IS STORED. The exact bytes that went on the wire - framed, sequenced
batches, unmodified. Not a decoded form, not a database. Two reasons:

  * replay is then a byte-for-byte re-send, so a client cannot tell recorded
    data from live data. Anything that re-encodes introduces a second code path
    that will eventually disagree with the first;
  * it is the cheapest thing the writer can possibly do, and the writer sits
    behind the live feed. A recorder that stalls is a feed that stalls.

WHY THIS ALSO SOLVES GAP RECOVERY. Moving to UDP multicast means datagrams get
lost, and a lost datagram silently corrupts a stateful book. The usual fix is a
server that maintains every symbol's book and can hand out a snapshot - but
this server does not decode anything, it forwards bytes, and giving it 100
symbols' worth of book state would be a large new surface with its own bugs.

Recording the stream gives something better for free: a client that detects a
gap asks for exactly the sequence range it missed and replays it. That is not
an approximation of the lost data, it IS the lost data. The same mechanism
serves "open the app and yesterday's session is already there".

LAYOUT, one directory per session day:

    data/2026-08-05/l2.bin     framed batches, appended in order
    data/2026-08-05/l2.idx     sampled (seq, offset) pairs for seeking
    data/2026-08-05/l1.bin
    data/2026-08-05/l1.idx

Channels are separate files so a replay of one does not have to skip over the
other, and so an L1 request never has to read L2 bytes off the disk.
"""

from __future__ import annotations

import os
import struct
import threading
import time
import logging

log = logging.getLogger("recorder")

# (seq, offset) every INDEX_EVERY batches. One entry per batch would be exact
# but costs ~300 MB/day at the measured rate; sampling makes the index 1.2 MB
# and a seek reads at most INDEX_EVERY batches to find its target.
INDEX_EVERY = 256
IDX = struct.Struct("<QQ")

# The writer batches before touching the disk. At 173 sweeps/sec a per-batch
# write is 173 syscalls a second competing with the feed; buffering to 1 MB
# makes it about two.
FLUSH_BYTES = 1 << 20
FLUSH_SECONDS = 2.0


class ChannelRecorder:
    """One channel's .bin/.idx pair. Not thread-safe by itself; Recorder locks."""

    def __init__(self, path_base: str):
        self.bin_path = path_base + ".bin"
        self.idx_path = path_base + ".idx"
        self._bin = open(self.bin_path, "ab")
        self._idx = open(self.idx_path, "ab")
        self._offset = self._bin.tell()
        self._buf = bytearray()
        self._since_index = 0
        self._last_flush = time.perf_counter()
        self.batches = 0
        self.bytes = 0

    def write(self, seq: int, data: bytes) -> None:
        if self._since_index == 0:
            # Index BEFORE buffering, against the offset this batch will land
            # at - which is the current file size plus what is still buffered.
            self._idx.write(IDX.pack(seq, self._offset + len(self._buf)))
        self._since_index = (self._since_index + 1) % INDEX_EVERY
        self._buf.extend(data)
        self.batches += 1
        self.bytes += len(data)
        now = time.perf_counter()
        if len(self._buf) >= FLUSH_BYTES or now - self._last_flush >= FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self._bin.write(self._buf)
            self._offset += len(self._buf)
            self._buf.clear()
        self._bin.flush()
        self._idx.flush()
        self._last_flush = time.perf_counter()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._bin.close()
            self._idx.close()


class Recorder:
    """Day-partitioned recorder for every channel."""

    def __init__(self, root: str, retain_days: int = 5):
        self.root = root
        self.retain_days = retain_days
        self._lock = threading.Lock()
        self._day = ""
        self._chans: dict[int, ChannelRecorder] = {}
        os.makedirs(root, exist_ok=True)

    # ---- writing ---------------------------------------------------------
    def write(self, channel: int, seq: int, data: bytes) -> None:
        """Record one framed batch. Called from the pipe-reader threads.

        Never raises into the feed: a disk that is full or a file that cannot
        be opened must degrade to "no recording" rather than stop the live
        stream. Losing history is bad; losing the live feed is worse.
        """
        try:
            with self._lock:
                self._roll_if_needed()
                ch = self._chans.get(channel)
                if ch is None:
                    ch = self._chans[channel] = ChannelRecorder(
                        os.path.join(self._dir(), f"ch{channel}"))
                ch.write(seq, data)
        except Exception:
            log.exception("recorder write failed; continuing without recording")

    def _dir(self) -> str:
        return os.path.join(self.root, self._day)

    def _roll_if_needed(self) -> None:
        day = time.strftime("%Y-%m-%d")
        if day == self._day:
            return
        for ch in self._chans.values():
            ch.close()
        self._chans.clear()
        self._day = day
        os.makedirs(self._dir(), exist_ok=True)
        log.info("recording to %s", self._dir())
        self._purge()

    def _purge(self) -> None:
        """Drop days beyond the retention window.

        The measured session is ~37 GB, so the default five days is ~185 GB
        against 396 GB free. Retention is not optional: a recorder with no
        purge fills the disk, and a full disk on the machine running Takion is
        a trading outage, not a storage problem.
        """
        try:
            days = sorted(d for d in os.listdir(self.root)
                          if os.path.isdir(os.path.join(self.root, d))
                          and len(d) == 10 and d[4] == "-")
        except FileNotFoundError:
            return
        for old in days[:-self.retain_days] if len(days) > self.retain_days else []:
            path = os.path.join(self.root, old)
            try:
                for f in os.listdir(path):
                    os.remove(os.path.join(path, f))
                os.rmdir(path)
                log.info("retention: removed %s", old)
            except OSError as e:
                log.warning("retention: could not remove %s: %s", old, e)

    def flush(self) -> None:
        with self._lock:
            for ch in self._chans.values():
                ch.flush()

    def close(self) -> None:
        with self._lock:
            for ch in self._chans.values():
                ch.close()
            self._chans.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"day": self._day,
                    "channels": {c: {"batches": r.batches, "bytes": r.bytes}
                                 for c, r in self._chans.items()}}


# ---------------------------------------------------------------- reading


def list_days(root: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d))
                      and len(d) == 10 and d[4] == "-")
    except FileNotFoundError:
        return []


def _load_index(idx_path: str) -> list[tuple[int, int]]:
    try:
        raw = open(idx_path, "rb").read()
    except FileNotFoundError:
        return []
    n = len(raw) // IDX.size
    return [IDX.unpack_from(raw, i * IDX.size) for i in range(n)]


def seek_offset(root: str, day: str, channel: int, from_seq: int) -> int:
    """Byte offset of the last indexed batch at or before `from_seq`.

    Returns 0 when the requested sequence is at or before the start of the
    file. The caller scans forward from here - the index is sampled, so this
    is a lower bound, never a guess past the target.
    """
    idx = _load_index(os.path.join(root, day, f"ch{channel}.idx"))
    if not idx:
        return 0
    lo, hi = 0, len(idx) - 1
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if idx[mid][0] <= from_seq:
            best = idx[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def read_range(root: str, day: str, channel: int, from_seq: int,
               to_seq: int | None, max_bytes: int = 64 << 20):
    """Yield recorded batches with from_seq <= seq <= to_seq, in order.

    Yields the raw framed bytes, unchanged, so a replayed batch is
    indistinguishable from the live one that was recorded.
    """
    from omnitrix.engine import wire            # local: server may run headless
    path = os.path.join(root, day, f"ch{channel}.bin")
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        f.seek(seek_offset(root, day, channel, from_seq))
        sent = 0
        buf = bytearray()
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                hdr = wire.decode_header(buf)
                if hdr is None:
                    break
                ch, count, seq = hdr
                end = _batch_len(buf, count)
                if end < 0:
                    break
                if to_seq is not None and seq > to_seq:
                    return
                if seq >= from_seq:
                    yield bytes(buf[:end])
                    sent += end
                    if sent >= max_bytes:
                        return
                del buf[:end]


def _batch_len(buf: bytearray, count: int) -> int:
    """Total length of a framed batch, or -1 if it is not all present."""
    from omnitrix.engine import wire
    from omnitrix.engine.takion_decode import L1, L2
    off = wire.HEADER_SIZE
    for _ in range(count):
        if off >= len(buf):
            return -1
        t = buf[off]
        n = L1.size if t == 1 else L2.size if t == 2 else -1
        if n < 0:
            return -1
        if off + 1 + n > len(buf):
            return -1
        off += 1 + n
    return off


def seq_bounds(root: str, day: str, channel: int):
    """(first_seq, last_seq) recorded for a channel, or None."""
    idx = _load_index(os.path.join(root, day, f"ch{channel}.idx"))
    if not idx:
        return None
    first = idx[0][0]
    last = first
    for b in read_range(root, day, channel, idx[-1][0], None):
        from omnitrix.engine import wire
        hdr = wire.decode_header(b)
        if hdr:
            last = hdr[2]
    return first, last
