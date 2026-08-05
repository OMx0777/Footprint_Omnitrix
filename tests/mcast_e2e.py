"""End-to-end: record -> replay -> multicast, with real sockets and real loss.

The three things that decide whether UDP is safe to run in production:

  1. RECORD/REPLAY IS EXACT. Replayed bytes must be the bytes that were
     recorded, in order, or a repaired book is a differently-wrong book.

  2. THE BACKFILL/LIVE JOIN HAS NO HOLE. Join the group first, buffer, backfill
     up to the first buffered sequence, drain. If that ordering is wrong there
     is a hole nothing can ever detect - the client never saw the sequence
     before the one it starts on, so its gap detector is perfectly happy.

  3. A LOST DATAGRAM IS REPAIRED, and if it cannot be, the book state it
     invalidated is DROPPED. Silent partial data is the failure this whole
     design exists to prevent.
"""

import os
import sys
import shutil
import socket
import struct
import tempfile
import threading
import time
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
HOST_DIR = r"C:\Users\ADMIN\Desktop\Host_Omnitrix"
sys.path.insert(0, HOST_DIR)

from omnitrix.engine import wire
from omnitrix.engine.takion_decode import L1, L2

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


L2_REC = b"\x02" + bytes(L2.size)
L1_REC = b"\x01" + bytes(L1.size)


def make_batch(ch, seq, n=4):
    rec = L2_REC if ch == wire.CH_L2 else L1_REC
    payload = rec * n
    return wire.encode(ch, seq, payload, n)


# ============================================================ 1. recorder
print("recorder")
import recorder as rec_mod

root = tempfile.mkdtemp()
r = rec_mod.Recorder(root, retain_days=5)
written = {}
N = 3000
for seq in range(1, N + 1):
    b = make_batch(wire.CH_L2, seq)
    written[seq] = b
    r.write(wire.CH_L2, seq, b)
for seq in range(1, 501):
    r.write(wire.CH_L1, seq, make_batch(wire.CH_L1, seq, 2))
r.flush()
day = time.strftime("%Y-%m-%d")

got = list(rec_mod.read_range(root, day, wire.CH_L2, 1, None))
check("every recorded batch reads back", len(got) == N, f"{len(got)} of {N}")
check("recorded bytes are IDENTICAL to what went on the wire",
      all(got[i] == written[i + 1] for i in range(len(got))),
      "a replayed batch must be the batch that was lost, not a re-encoding")

sub = list(rec_mod.read_range(root, day, wire.CH_L2, 1500, 1600))
check("a sequence range seeks exactly", len(sub) == 101,
      f"{len(sub)} batches for 1500..1600")
check("the range starts on the right batch",
      wire.decode_header(sub[0])[2] == 1500 if sub else False)
check("the range ends on the right batch",
      wire.decode_header(sub[-1])[2] == 1600 if sub else False)
check("channels do not bleed into each other",
      len(list(rec_mod.read_range(root, day, wire.CH_L1, 1, None))) == 500)
b = rec_mod.seq_bounds(root, day, wire.CH_L2)
check("bounds are reported", b == (1, N), str(b))

# a mid-index seek must not overshoot
sub2 = list(rec_mod.read_range(root, day, wire.CH_L2, 2999, None))
check("seeking near the end works", len(sub2) == 2, f"{len(sub2)}")

# ============================================================ 2. replay server
print("\nreplay server")
import asyncio
import replay_server

REPLAY_PORT = 9981
srv_ready = threading.Event()
loop_holder = {}


def run_replay():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop_holder["loop"] = loop
    rs = replay_server.ReplayServer(root, "127.0.0.1", REPLAY_PORT, "")

    async def go():
        s = await rs.start()
        srv_ready.set()
        await asyncio.Event().wait()
    try:
        loop.run_until_complete(go())
    except Exception:
        pass


threading.Thread(target=run_replay, daemon=True).start()
srv_ready.wait(15)
time.sleep(0.4)


def request(line):
    s = socket.create_connection(("127.0.0.1", REPLAY_PORT), timeout=15)
    s.sendall(line.encode() + b"\n")
    head = bytearray()
    while not head.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        head.extend(c)
    txt = head.decode().strip()
    if not txt.startswith("LEN "):
        s.close()
        return txt, b""
    n = int(txt.split()[1])
    out = bytearray()
    while len(out) < n:
        ch = s.recv(min(1 << 20, n - len(out)))
        if not ch:
            break
        out.extend(ch)
    s.close()
    return txt, bytes(out)


_, data = request(f"REPLAY {wire.CH_L2} 100 199")
n_batches = 0
buf = bytearray(data)
seqs = []
while buf:
    h = wire.decode_header(buf)
    if h is None:
        break
    end = len(make_batch(wire.CH_L2, h[2]))
    seqs.append(h[2])
    del buf[:end]
    n_batches += 1
check("the server replays the requested range", n_batches == 100, f"{n_batches}")
check("replayed sequences are contiguous and correct",
      seqs == list(range(100, 200)), f"{seqs[:3]}..{seqs[-3:]}" if seqs else "none")
check("replayed bytes match the recording exactly",
      data == b"".join(written[s] for s in range(100, 200)))

_, tail = request(f"REPLAY {wire.CH_L2} 2990 -")
check("an open-ended range runs to the newest batch",
      len(tail) == sum(len(written[s]) for s in range(2990, N + 1)))

# ============================================================ 3. multicast e2e
print("\nmulticast, backfill join, and repair")
from omnitrix.engine.multicast_feed import MulticastFeed

GROUP, MPORT = "239.7.7.31", 9982

pub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
pub.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
pub.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
# NOT 127.0.0.1. Measured on Windows: joining a group on the loopback address
# silently receives nothing (0 of 20 datagrams), while 0.0.0.0 and the real LAN
# address both work. A test that used loopback would "prove" multicast worked
# on a machine where it does not.

live_seq = [N]
stop = threading.Event()
DROP = {"at": set()}
published = []


def publisher():
    while not stop.is_set():
        live_seq[0] += 1
        s = live_seq[0]
        b = make_batch(wire.CH_L2, s)
        r.write(wire.CH_L2, s, b)        # recorded even when not published
        r.flush()
        published.append(s)
        if s not in DROP["at"]:
            try:
                pub.sendto(b, (GROUP, MPORT))
            except OSError:
                pass
        time.sleep(0.01)


# lose four datagrams once the client is running
DROP["at"] = {N + 40, N + 41, N + 42, N + 43}
threading.Thread(target=publisher, daemon=True).start()
time.sleep(0.3)

feed = MulticastFeed(group=GROUP, port=MPORT, iface="0.0.0.0",
                     replay_host="127.0.0.1", replay_port=REPLAY_PORT)
applied = []
feed._on_l2 = lambda c, o: applied.append(1)
feed._on_l1 = lambda c, o: None
dropped_state = {"n": 0}
feed.on_disconnect = lambda: dropped_state.__setitem__("n", dropped_state["n"] + 1)
feed.start()
time.sleep(3.5)
stop.set()
time.sleep(0.4)
feed.stop()

check("the client joined the group and received live data",
      feed.connected["multicast"] and len(applied) > 0,
      f"{len(applied)} records applied")
check("backfill ran against the replay server", feed.connected["replay"])
check("the backfill/live join left no undetectable hole",
      len(applied) >= N * 4,
      f"{len(applied)} records applied, history alone is {N*4}")
check("the dropped datagrams were DETECTED",
      feed.gaps.lost >= 4, f"detector saw {feed.gaps.lost} lost")
check("and REPAIRED from the recording",
      feed.repairs >= 1 and feed.unrepaired == 0,
      f"repairs={feed.repairs} unrepaired={feed.unrepaired} "
      f"bytes={feed.repair_bytes}")
check("a repaired gap does NOT discard the book",
      dropped_state["n"] == 0,
      "the replay restores exactly those bytes, so throwing state away first "
      "would discard what the replay rebuilds on")

# ---- 4. no replay server: the book MUST be dropped, not kept stale --------
feed2 = MulticastFeed(group=GROUP, port=MPORT, iface="0.0.0.0",
                      replay_host="127.0.0.1", replay_port=1)   # nothing there
feed2._on_l2 = lambda c, o: None
feed2._on_l1 = lambda c, o: None
lost_state = {"n": 0}
feed2.on_disconnect = lambda: lost_state.__setitem__("n", lost_state["n"] + 1)
feed2.gaps.observe(wire.CH_L2, 10)
with feed2._pending_lock:
    feed2._pending.append((wire.CH_L2, 11, 14))
feed2._repair_pending()
check("with no repair available the book state is DROPPED",
      lost_state["n"] == 1 and feed2.unrepaired == 1,
      "an honest hole beats a phantom wall")

pub.close()
shutil.rmtree(root, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("MULTICAST END-TO-END OK")
