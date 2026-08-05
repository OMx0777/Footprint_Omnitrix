"""The sequenced wire protocol, and what a client does when a batch is lost.

This exists because of a specific planned change: serving ~100 LAN clients
needs multicast (measured: 100 unicast copies is 1.27 Gbit/s, over 1 GbE),
multicast means UDP, and UDP loses datagrams. The L2 stream is stateful and the
book merge is additive, so a silently lost datagram becomes a level that reads
as still resting forever - a phantom wall on the heatmap.

So the requirement is not "handle loss gracefully". It is: a client must be
able to TELL that something was lost, and must throw away the state it can no
longer vouch for. These checks are what make the transport safe to change.
"""

import os
import sys
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import wire
from omnitrix.engine.network_feed import NetworkFeed
from omnitrix.engine.takion_decode import L1, L2

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


L2_REC = b"\x02" + b"\x00" * L2.size
L1_REC = b"\x01" + b"\x00" * L1.size


def batch(channel, seq, records):
    payload = b"".join(records)
    return wire.encode(channel, seq, payload, len(records))


print("framing")
# ---- 1. datagrams must fit one Ethernet frame ------------------------------
recs = [L2_REC] * 200
parts = wire.split_payload(recs)
check("payloads never exceed the datagram limit",
      all(len(p) <= wire.MAX_PAYLOAD for p, _ in parts),
      f"{len(parts)} datagrams, largest {max(len(p) for p, _ in parts)} B "
      f"(limit {wire.MAX_PAYLOAD})")
check("no record is split across datagrams",
      all(len(p) % len(L2_REC) == 0 for p, _ in parts))
check("every record survives the split",
      sum(n for _, n in parts) == len(recs),
      f"{sum(n for _, n in parts)} of {len(recs)}")
try:
    wire.split_payload([b"x" * (wire.MAX_PAYLOAD + 1)])
    check("an oversized record raises rather than truncating", False)
except ValueError:
    check("an oversized record raises rather than truncating", True)

# ---- 2. header round-trip ---------------------------------------------------
b = batch(wire.CH_L2, 12345, [L2_REC, L2_REC])
got = wire.decode_header(b)
check("header round-trips", got == (wire.CH_L2, 2, 12345), str(got))
check("legacy framing is not mistaken for a header",
      wire.decode_header(L2_REC) is None)
check("a short buffer is not mistaken for a header",
      wire.decode_header(b[:5]) is None)

# ---- 3. gap detection -------------------------------------------------------
print("\ngap detection")
g = wire.GapDetector()
check("first batch on a channel is never a gap", g.observe(wire.CH_L2, 500) == 0)
check("consecutive batches are continuous", g.observe(wire.CH_L2, 501) == 0)
check("one missing batch is reported as one", g.observe(wire.CH_L2, 503) == 1)
check("a burst loss reports the true count", g.observe(wire.CH_L2, 1000) == 496,
      f"{g.lost} lost in total")
check("channels are sequenced independently",
      g.observe(wire.CH_L1, 7) == 0,
      "an L2 gap must not implicate L1")
g2 = wire.GapDetector()
g2.observe(wire.CH_L2, 10)
g2.observe(wire.CH_L2, 11)
check("a duplicate is not counted as loss",
      g2.observe(wire.CH_L2, 11) == 0 and g2.lost == 0,
      f"duplicates={g2.duplicates}")
check("a reordered batch is not counted as loss",
      g2.observe(wire.CH_L2, 5) == 0 and g2.lost == 0,
      f"reordered={g2.reordered}")
check("reordering does not rewind the high-water mark",
      g2.observe(wire.CH_L2, 12) == 0, "12 must still be the next expected")

print("\nclient behaviour")
# ---- 4. a client parses sequenced batches -----------------------------------
feed = NetworkFeed("127.0.0.1", 1)
seen = {"l1": 0, "l2": 0}
feed._on_l1 = lambda c, o: seen.__setitem__("l1", seen["l1"] + 1)
feed._on_l2 = lambda c, o: seen.__setitem__("l2", seen["l2"] + 1)

buf = bytearray()
buf.extend(batch(wire.CH_L2, 1, [L2_REC] * 3))
buf.extend(batch(wire.CH_L1, 1, [L1_REC] * 2))
feed._drain(buf)
check("sequenced batches are decoded", seen == {"l1": 2, "l2": 3}, str(seen))
check("the buffer is fully consumed", len(buf) == 0, f"{len(buf)} B left")

# ---- 5. a partial batch waits instead of publishing half of it -------------
whole = batch(wire.CH_L2, 2, [L2_REC] * 4)
buf = bytearray(whole[:len(whole) - 10])
before = dict(seen)
feed._drain(buf)
check("a partial batch publishes NOTHING", seen == before, str(seen))
check("the partial batch stays buffered", len(buf) > 0)
buf.extend(whole[len(whole) - 10:])
feed._drain(buf)
check("the batch is published once it is complete",
      seen["l2"] == before["l2"] + 4, str(seen))

# ---- 6. THE POINT: a lost batch must invalidate book state -----------------
feed2 = NetworkFeed("127.0.0.1", 1)
feed2._on_l1 = lambda c, o: None
feed2._on_l2 = lambda c, o: None
dropped = {"book": 0, "l1": 0}
feed2.on_disconnect = lambda: dropped.__setitem__("book", dropped["book"] + 1)
feed2.on_l1_disconnect = lambda: dropped.__setitem__("l1", dropped["l1"] + 1)

buf = bytearray()
buf.extend(batch(wire.CH_L2, 1, [L2_REC]))
buf.extend(batch(wire.CH_L2, 2, [L2_REC]))
feed2._drain(buf)
check("no loss, no state thrown away", dropped["book"] == 0, str(dropped))

buf.extend(batch(wire.CH_L2, 9, [L2_REC]))       # 3..8 never arrived
feed2._drain(buf)
check("a lost L2 batch DROPS the half-assembled book",
      dropped["book"] == 1,
      "otherwise the additive merge reports pulled liquidity as still resting")
check("the loss is counted", feed2.gaps.lost == 6, f"{feed2.gaps.lost}")

buf.extend(batch(wire.CH_L1, 1, [L1_REC]))
buf.extend(batch(wire.CH_L1, 4, [L1_REC]))       # 2..3 never arrived
feed2._drain(buf)
check("a lost L1 batch forgets cumulative volume",
      dropped["l1"] == 1,
      "otherwise the next record emits the whole gap as one fabricated print")
check("an L1 gap does not also discard the book",
      dropped["book"] == 1, f"{dropped}")

# ---- 6b. a header split across TCP reads must not be eaten ----------------
# decode_header cannot distinguish "not a header" from "not enough bytes yet",
# and the legacy fallback would consume the magic byte as a record type and
# resync past it - discarding the sequence number that proves nothing was lost.
feed4 = NetworkFeed("127.0.0.1", 1)
n4 = {"l2": 0}
feed4._on_l1 = lambda c, o: None
feed4._on_l2 = lambda c, o: n4.__setitem__("l2", n4["l2"] + 1)
whole = batch(wire.CH_L2, 1, [L2_REC] * 3) + batch(wire.CH_L2, 2, [L2_REC] * 3)
buf = bytearray()
for i in range(0, len(whole), 5):          # 5-byte reads: headers always split
    buf.extend(whole[i:i + 5])
    feed4._drain(buf)
check("a header split across reads is not consumed as a record",
      n4["l2"] == 6 and feed4.gaps.lost == 0,
      f"{n4['l2']} of 6 records, {feed4.gaps.lost} phantom losses")

# ---- 7. legacy framing still works (mixed-version rollout) -----------------
feed3 = NetworkFeed("127.0.0.1", 1)
cnt = {"l1": 0, "l2": 0}
feed3._on_l1 = lambda c, o: cnt.__setitem__("l1", cnt["l1"] + 1)
feed3._on_l2 = lambda c, o: cnt.__setitem__("l2", cnt["l2"] + 1)
buf = bytearray(L2_REC * 3 + L1_REC * 2)
feed3._drain(buf)
check("an UNSEQUENCED server still parses", cnt == {"l1": 2, "l2": 3}, str(cnt))
check("legacy stream reports no phantom gaps", feed3.gaps.lost == 0)

# ---- 8. sequence numbers must not wrap in a session ------------------------
per_sec = 1.59e6 / 1400          # feed bytes/s over one datagram
years = (2 ** 64) / per_sec / 3600 / 24 / 365
check("the 64-bit sequence cannot wrap in any realistic session",
      years > 1e6, f"{years:.3g} years of continuous streaming")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("WIRE SEQUENCING OK")
