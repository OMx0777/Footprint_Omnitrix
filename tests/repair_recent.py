"""A gap repair asks for RECENT sequences. Those are the ones still in RAM.

Reproduced from a live server log:

    replay ('192.168.2.112', 49894) ch2 10452287..10452296 -> 0.0 MB
    replay ('192.168.2.112', 49895) ch2 10452287..10452296 -> 0.0 MB

Ten batches that certainly existed, requested twice, both times zero bytes.
The recorder buffers to 1 MB or 2 seconds before it touches the disk, so the
newest batches are in memory - and a gap repair is ALWAYS for recent
sequences, because that is the only kind of gap there is. Every repair failed,
and each failure cost the client its book state instead of a round trip.

The second half of this file covers the freeze that followed: repair used to
run on the receive thread, so while it waited on a TCP reply the multicast
socket went unread, the kernel dropped more datagrams, and those queued more
repairs. A loop whose input is its own output.
"""

import os
import sys
import asyncio
import socket
import tempfile
import threading
import time
import shutil
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Host_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import wire
from omnitrix.engine.takion_decode import L2
from omnitrix.engine.multicast_feed import MulticastFeed, MAX_PENDING_REPAIRS

import recorder as rec_mod
import replay_server

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


L2_REC = b"\x02" + bytes(L2.size)


def batch(seq, n=4):
    return wire.encode(wire.CH_L2, seq, L2_REC * n, n)


root = tempfile.mkdtemp()
rec = rec_mod.Recorder(root, retain_days=5)
day = time.strftime("%Y-%m-%d")

# Write a handful of batches and DO NOT flush - exactly the live situation
# when a datagram is lost a moment ago.
for seq in range(1, 11):
    rec.write(wire.CH_L2, seq, batch(seq))

on_disk = list(rec_mod.read_range(root, day, wire.CH_L2, 1, 10))
check("the batches are NOT on disk yet - this is the live condition",
      len(on_disk) == 0,
      f"{len(on_disk)} readable before a flush; the recorder buffers to "
      f"{rec_mod.FLUSH_BYTES // 1024} kB or {rec_mod.FLUSH_SECONDS} s")

PORT = 9977
ready = threading.Event()


def run_server(with_recorder):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    rs = replay_server.ReplayServer(root, "127.0.0.1", PORT, "",
                                    rec if with_recorder else None)

    async def go():
        await rs.start()
        ready.set()
        await asyncio.Event().wait()
    try:
        loop.run_until_complete(go())
    except Exception:
        pass


threading.Thread(target=run_server, args=(True,), daemon=True).start()
ready.wait(15)
time.sleep(0.4)


def replay(a, b):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall(f"REPLAY {wire.CH_L2} {a} {b}\n".encode())
    head = bytearray()
    while not head.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        head.extend(c)
    n = int(head.decode().strip().split()[1])
    out = bytearray()
    while len(out) < n:
        chunk = s.recv(min(65536, n - len(out)))
        if not chunk:
            break
        out.extend(chunk)
    s.close()
    return bytes(out)


data = replay(1, 10)
check("a repair for buffered sequences now returns them", len(data) > 0,
      f"{len(data)} bytes - this returned 0.0 MB on the live server")
check("it returns ALL ten batches",
      data == b"".join(batch(s) for s in range(1, 11)),
      f"{len(data)} bytes vs {len(b''.join(batch(s) for s in range(1,11)))} expected")

# ---- the freeze: repair must not run on the receive thread -----------------
print()
feed = MulticastFeed(group="239.7.7.77", port=9976, iface="0.0.0.0",
                     replay_host="127.0.0.1", replay_port=PORT)
src = open(os.path.join(os.path.dirname(__file__), "..", "omnitrix",
                        "engine", "multicast_feed.py"), encoding="utf-8").read()
check("repair runs on its own thread, not the receive loop",
      "_repair_thread" in src and "def _repair_loop" in src,
      "a blocking TCP call inside the receive loop stops the socket being "
      "read, which causes the loss it is trying to repair")
check("the receive loop contains nothing that can block on the network",
      "self._repair_pending()" not in src.split("def _repair_loop")[0]
      .split("while self._running:")[-1],
      "the receive loop must do exactly one thing: get datagrams off the socket")
check("the repair connect timeout is LAN-sized, not 20 s",
      "timeout=3.0" in src,
      "every second spent waiting is a second of gaps piling up behind it")

# ---- the pending queue must be bounded -------------------------------------
feed._on_l2 = lambda c, o: None
feed._on_l1 = lambda c, o: None
dropped = {"n": 0}
feed.on_disconnect = lambda: dropped.__setitem__("n", dropped["n"] + 1)
feed.gaps.observe(wire.CH_L2, 1)
for i in range(MAX_PENDING_REPAIRS + 20):
    # every other sequence, so each one is a fresh gap
    feed._on_datagram(batch(3 + i * 2))
check("the pending-repair queue is bounded",
      len(feed._repair_q) <= MAX_PENDING_REPAIRS,
      f"{len(feed._repair_q)} queued, ceiling {MAX_PENDING_REPAIRS}")
check("...and hitting the ceiling drops book state rather than queueing more",
      dropped["n"] >= 1,
      "unbounded queueing turns sustained loss into an unbounded memory problem")

shutil.rmtree(root, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("RECENT-GAP REPAIR OK")
