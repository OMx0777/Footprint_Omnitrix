"""The SERVER's encoder and the CLIENT's decoder must agree. They are two
programs; the only thing keeping them compatible is that both import the same
wire module, so that is what this checks - end to end, with the server's own
batching code path."""
import sys, importlib.util, os
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from omnitrix.engine import wire
from omnitrix.engine.network_feed import NetworkFeed
from omnitrix.engine.takion_decode import L1, L2
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

# the server module imports pywin32; load only what we need from its source
src = open(r"C:\Users\ADMIN\Desktop\Host_Omnitrix\broadcaster_server.py",
           encoding="utf-8").read()
check("server imports the shared wire module, not its own copy",
      "from omnitrix.engine import wire" in src)
check("server sequences per channel",
      "next_seq(channel)" in src and "_seq_lock" in src)
check("server splits into datagram-sized batches",
      "wire.split_payload(records)" in src)

# Reproduce the server's exact emit path and feed it to a real client decoder.
seq = {wire.CH_L1: 0, wire.CH_L2: 0}
def server_emit(type_id, rec_size, channel, n_records):
    chunk = bytes(rec_size * n_records)
    records = [type_id + chunk[o:o+rec_size]
               for o in range(0, len(chunk), rec_size)]
    out = bytearray()
    for payload, count in wire.split_payload(records):
        seq[channel] += 1
        out.extend(wire.encode(channel, seq[channel], payload, count))
    return bytes(out)

feed = NetworkFeed("127.0.0.1", 1)
got = {"l1":0, "l2":0}
feed._on_l1 = lambda c,o: got.__setitem__("l1", got["l1"]+1)
feed._on_l2 = lambda c,o: got.__setitem__("l2", got["l2"]+1)

N_L2, N_L1 = 5000, 400
buf = bytearray()
buf.extend(server_emit(b"\x02", L2.size, wire.CH_L2, N_L2))
buf.extend(server_emit(b"\x01", L1.size, wire.CH_L1, N_L1))
feed._drain(buf)
check("every server record reaches the client decoder",
      got == {"l1": N_L1, "l2": N_L2}, f"{got} vs expected {{'l1': {N_L1}, 'l2': {N_L2}}}")
check("nothing is left stranded in the buffer", len(buf)==0, f"{len(buf)} B")
check("no phantom gaps on a clean stream", feed.gaps.lost==0, str(feed.gaps.stats()))

# TCP delivers a byte stream, not batches: the client must survive arbitrary
# chunking of the same bytes.
import random
rng = random.Random(7)
feed2 = NetworkFeed("127.0.0.1", 1)
got2 = {"l1":0,"l2":0}
feed2._on_l1 = lambda c,o: got2.__setitem__("l1", got2["l1"]+1)
feed2._on_l2 = lambda c,o: got2.__setitem__("l2", got2["l2"]+1)
seq[wire.CH_L1]=seq[wire.CH_L2]=0
stream = server_emit(b"\x02", L2.size, wire.CH_L2, N_L2) + \
         server_emit(b"\x01", L1.size, wire.CH_L1, N_L1)
buf2 = bytearray(); i = 0
while i < len(stream):
    n = rng.randint(1, 3000)
    buf2.extend(stream[i:i+n]); i += n
    feed2._drain(buf2)
check("arbitrary TCP chunking loses nothing",
      got2 == {"l1": N_L1, "l2": N_L2}, f"{got2}")
check("arbitrary chunking invents no gaps", feed2.gaps.lost==0,
      str(feed2.gaps.stats()))

# And the datagram count tells us the multicast packet rate.
recs = 173*256
parts = wire.split_payload([b"\x02"+bytes(L2.size)]*recs)
print(f"\n  at the live census: {len(parts)} datagrams/sec on the L2 channel "
      f"({len(parts)*1400/1e6:.2f} MB/s wire)")
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("SERVER/CLIENT INTEROP OK")
