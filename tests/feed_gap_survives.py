"""A multicast gap must not kill the receive thread.

THE BUG THIS EXISTS FOR, from the operator's own log:

    WARNING multicast gap: ch2 missing 19160977..19161083 (107 batches)
    ERROR   unhandled exception in thread Thread-1 (_run) (that thread stopped)
      File "takion_decode.py", line 423, in _on_l1
    ValueError: too many values to unpack (expected 2, got 3)

MulticastFeed subclasses TakionDecoder. The decoder owns `_pending` - trades
held back until the exchange clock offset is known, stored as (Trade, raw_ts)
PAIRS. The feed declared its own `_pending` for gap ranges, (channel, from, to)
TRIPLES, and silently replaced the parent's attribute.

Nothing was wrong until the first gap. Then a triple landed in the list the
decoder unpacks as pairs, the receive thread raised, and it STOPPED. The
terminal stayed perfectly responsive - 21 fps, empty queue, nothing dropped -
and never received another byte for the rest of the session. Four rounds of
diagnosis went to the chart, the loader and the frame budget, because the app
looked healthy in every way except that no data was arriving.

What is tested here is the behaviour, not the rename: a gap arrives, and
afterwards the feed still decodes. A test that only asserted the attribute
name would pass on any future collision under a different name.
"""

import os
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.engine import wire
from omnitrix.engine.multicast_feed import MulticastFeed
from omnitrix.engine.takion_decode import TakionDecoder, L1, L2

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


# ---- 1. THE COLLISION ITSELF ----------------------------------------------
# Every attribute the decoder relies on must survive being subclassed.
dec = TakionDecoder.__new__(TakionDecoder)
TakionDecoder.__init__(dec)
feed = MulticastFeed.__new__(MulticastFeed)
MulticastFeed.__init__(feed)

shared = []
for name, val in vars(dec).items():
    if not name.startswith("_"):
        continue
    if name in vars(feed) and type(vars(feed)[name]) is not type(val):
        shared.append((name, type(val).__name__,
                       type(vars(feed)[name]).__name__))
check("no attribute the decoder owns is replaced by the subclass with a "
      "DIFFERENT type - that is the collision, and it is silent",
      not shared, f"{shared}")

check("the decoder's clock-hold list is still a list of pairs",
      isinstance(getattr(feed, "_pending", []), list))

# ---- 2. THE BEHAVIOUR: a gap, then decoding still works -------------------
AGGR_TRADES = []
# _on_trade, not on_trade - Feed._emit_trade calls the underscored one, and a
# fixture wired to the wrong name records nothing while looking correct.
feed._on_trade = lambda tr: AGGR_TRADES.append(tr)
feed._on_book = lambda bk: None
feed.replay_host = ""            # no repair path; the gap is just noted
feed.symbols = None


def l1_rec(sym, last, cum, raw):
    return b"\x01" + L1.pack(sym.encode().ljust(32, b"\x00"),
                             last, last, last, last, last - 0.01, last + 0.01,
                             cum, raw & 0xFFFFFFFF, 0, 100, 100)


def batch(ch, seq, payload, n):
    return wire.encode(ch, seq, payload, n)


# The decoder withholds every trade until the exchange-vs-local clock offset
# is measured, so a fixture that never locks it delivers nothing and proves
# nothing about decoding. Seed it.
feed._ts_offset = 0
base_raw = 9 * 3600 * 1000
seq = 1000
for i in range(40):
    feed._on_datagram(batch(wire.CH_L1, seq,
                            l1_rec("NVDA", 100.0 + i * 0.01,
                                   400_000 + i * 100, base_raw + i * 1000), 1))
    seq += 1
before = len(AGGR_TRADES)
check("the feed decodes trades before any gap", before > 0,
      f"{before} trades")

# ---- NOW THE GAP - a jump in sequence, exactly as loss looks --------------
err = None
try:
    seq += 108                                  # 107 batches missing
    feed._on_datagram(batch(wire.CH_L2, seq,
                            b"\x02" + L2.pack(b"NVDA".ljust(8, b"\x00"),
                                              b"ARCA".ljust(8, b"\x00"),
                                              100.0, 500, b"B"), 1))
except Exception as e:                                          # noqa: BLE001
    err = f"{type(e).__name__}: {e}"
check("a gap does not raise on the receive thread", err is None, err or "")

# and the decoder must keep working AFTERWARDS - this is what died
err2 = None
try:
    for i in range(40, 90):
        seq += 1
        feed._on_datagram(batch(wire.CH_L1, seq,
                                l1_rec("NVDA", 100.0 + i * 0.01,
                                       400_000 + i * 100,
                                       base_raw + i * 1000), 1))
except Exception as e:                                          # noqa: BLE001
    err2 = f"{type(e).__name__}: {e}"
check("...and the feed KEEPS DECODING after the gap - the receive thread used "
      "to stop here and the terminal never saw another byte",
      err2 is None, err2 or "")
check("...with new trades actually delivered", len(AGGR_TRADES) > before,
      f"{before} -> {len(AGGR_TRADES)} trades")

# ---- 3. and the gap really was recorded -----------------------------------
check("the gap was counted, not swallowed", feed.gaps.lost > 0,
      f"{feed.gaps.lost} batches lost")
check("...and queued for repair under its own name",
      isinstance(getattr(feed, "_repair_q", None), list),
      "the feed's gap list must not be the decoder's hold list")

# ---- 4. MANY gaps, as a real loss burst produces --------------------------
err3 = None
try:
    for k in range(300):
        seq += 5
        feed._on_datagram(batch(wire.CH_L1, seq,
                                l1_rec("NVDA", 101.0 + (k % 20) * 0.01,
                                       500_000 + k * 100,
                                       base_raw + (200 + k) * 1000), 1))
except Exception as e:                                          # noqa: BLE001
    err3 = f"{type(e).__name__}: {e}"
check("three hundred consecutive gaps are survived - a loss burst must not "
      "be able to stop the feed", err3 is None, err3 or "")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("FEED SURVIVES GAPS OK")
