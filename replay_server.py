"""TCP replay: historical backfill on open, and gap recovery on UDP loss.

ONE mechanism, two requirements, because they are the same request:

    "give me channel C from sequence A to sequence B"

  * a client opening the app asks for the session so far, so the chart is
    populated before the first live datagram arrives;
  * a client that missed datagrams asks for exactly the range it missed.

The second is what makes UDP safe to use at all. A lost datagram silently
corrupts a stateful book - the merge is additive, so a level that was pulled
reads as still resting. Replaying the exact missing bytes is not an
approximation of that data, it IS that data, so the book is restored rather
than patched.

TCP on purpose. Recovery must not itself be lossy, and it is rare and bursty -
the properties TCP is good at. The live stream is continuous and must not
multiply by client count, which is what multicast is good at. Using each for
what it is good at is the whole design.

PROTOCOL - newline-terminated ASCII commands, binary replies. Deliberately
trivial to read in a packet capture when something goes wrong at 3am.

    HELLO                       -> OK <day> <ch>:<first>:<last> ...
    REPLAY <ch> <from> <to>     -> LEN <nbytes>\\n then nbytes of framed batches
    REPLAY <ch> <from> -        -> everything from <from> to the newest
    DAYS                        -> OK <day> <day> ...
    BYE                         -> closes
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import recorder

log = logging.getLogger("replay")

# A single recovery request must not be able to monopolise the server or the
# client's memory. 256 MB is ~2.7 minutes of the full feed - far more than any
# real gap, and a client that needs more than this should be doing a fresh
# backfill rather than a recovery.
MAX_REPLY = 256 << 20

# Concurrent replays. Backfill is disk-bound and bursty; letting 100 clients
# all replay a session at once would thrash the disk and starve the live
# recorder, which shares it. Queueing is better than everyone going slowly.
MAX_CONCURRENT = 8


class ReplayServer:
    def __init__(self, root: str, host: str = "0.0.0.0", port: int = 9998,
                 token: str = ""):
        self.root = root
        self.host = host
        self.port = port
        self.token = token
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.served = 0
        self.bytes_served = 0

    async def start(self):
        srv = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("replay server on %s:%d serving %s",
                 self.host, self.port, self.root)
        return srv

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            if self.token:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line.decode("utf-8", "ignore").strip() != self.token:
                    log.warning("replay: bad token from %s", peer)
                    writer.close()
                    return
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=300.0)
                if not line:
                    return
                cmd = line.decode("utf-8", "ignore").strip().split()
                if not cmd:
                    continue
                op = cmd[0].upper()
                if op == "BYE":
                    return
                if op == "DAYS":
                    days = recorder.list_days(self.root)
                    writer.write(("OK " + " ".join(days) + "\n").encode())
                    await writer.drain()
                elif op == "HELLO":
                    await self._hello(writer)
                elif op == "REPLAY":
                    await self._replay(cmd, writer, peer)
                else:
                    writer.write(b"ERR unknown command\n")
                    await writer.drain()
        except asyncio.TimeoutError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("replay: client %s failed", peer)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _hello(self, writer):
        days = recorder.list_days(self.root)
        day = days[-1] if days else ""
        parts = [f"OK {day or '-'}"]
        if day:
            for ch in (1, 2):
                b = recorder.seq_bounds(self.root, day, ch)
                if b:
                    parts.append(f"{ch}:{b[0]}:{b[1]}")
        writer.write((" ".join(parts) + "\n").encode())
        await writer.drain()

    async def _replay(self, cmd, writer, peer):
        # REPLAY <ch> <from> <to|-> [day]
        try:
            ch = int(cmd[1])
            frm = int(cmd[2])
            to = None if cmd[3] == "-" else int(cmd[3])
            day = cmd[4] if len(cmd) > 4 else ""
        except (IndexError, ValueError):
            writer.write(b"ERR usage: REPLAY <ch> <from> <to|-> [day]\n")
            await writer.drain()
            return
        if not day:
            days = recorder.list_days(self.root)
            if not days:
                writer.write(b"LEN 0\n")
                await writer.drain()
                return
            day = days[-1]

        async with self._sem:
            t0 = time.perf_counter()
            # Collect first, then send a length prefix. The client needs to
            # know where the reply ends before it can go back to reading
            # commands, and a length is far simpler to get right than an
            # in-band terminator that could collide with binary payload.
            chunks = []
            total = 0
            loop = asyncio.get_running_loop()

            def _read():
                out = []
                n = 0
                for b in recorder.read_range(self.root, day, ch, frm, to,
                                             max_bytes=MAX_REPLY):
                    out.append(b)
                    n += len(b)
                return out, n

            # Disk reads are blocking; off the event loop or every other
            # client's replay stalls behind this one.
            chunks, total = await loop.run_in_executor(None, _read)

            writer.write(f"LEN {total}\n".encode())
            for c in chunks:
                writer.write(c)
                if writer.transport.get_write_buffer_size() > (4 << 20):
                    await writer.drain()
            await writer.drain()
            self.served += 1
            self.bytes_served += total
            log.info("replay %s ch%d %d..%s -> %.1f MB in %.2fs",
                     peer, ch, frm, to if to is not None else "live",
                     total / 1e6, time.perf_counter() - t0)
