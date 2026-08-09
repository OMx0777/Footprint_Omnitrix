"""A whole session of heat, small enough to keep for every symbol.

The live ring holds 23 minutes. This checks the archive that keeps the rest of
the day, and the things that could make it worthless without raising:

  * TOTALS MUST SURVIVE EXACTLY. Volume and delta are data-truth quantities -
    the footprint and the monitor report the same session and have to agree. If
    compression touched them the two views would disagree by an amount nobody
    could account for. Only the per-bin SHAPE is allowed to be lossy.

  * A WALL MUST STILL LOOK LIKE A WALL. The whole point is finding where the
    size was, so a genuine wall has to remain visibly larger than the ordinary
    book after time-averaging, price-summing and 8-bit quantisation. A
    compression that made everything the same brightness would pass every
    memory check and be completely useless.

  * IT MUST NOT SILENTLY DOUBLE-COUNT OR LOSE COLUMNS at the fold seam.

  * IT MUST BE BOUNDED. The reason the live ring cannot hold the day is memory;
    an archive that also grows without limit has solved nothing.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from omnitrix.engine import Instruments, BookmapBuffer
from omnitrix.engine.heatarchive import (SessionArchive, quantise, dequantise,
                                         HEAT_MAX, ARCH_COL_S, ARCH_TICK_STEP)
from omnitrix.engine.model import Trade, BookSnapshot, Aggressor

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


inst = Instruments(default_tick=0.01)

# ---- 1. the quantiser ------------------------------------------------------
errs = []
for v in (1, 5, 25, 100, 500, 2_000, 10_000, 75_000, 400_000, 3_000_000,
          9_999_999):
    back = float(dequantise(quantise(v)))
    errs.append(abs(back - v) / v)
check("log quantisation keeps every size within 4% - finer than the 256-entry "
      "colour ramp it feeds, so nothing visible is lost",
      max(errs) < 0.04, f"worst {max(errs) * 100:.2f}%")
check("...and it is monotonic, so ordering by heat is ordering by size",
      all(quantise(a) <= quantise(b)
          for a, b in zip([1, 10, 100, 1e3, 1e4, 1e5, 1e6],
                          [10, 100, 1e3, 1e4, 1e5, 1e6, 1e7])))
check("zero stays zero - an empty band must not glow", quantise(0) == 0
      and float(dequantise(0)) == 0.0)
check("the range covers a real book's biggest orders",
      quantise(HEAT_MAX) == 255 and quantise(HEAT_MAX * 10) == 255)

# ---- 2. totals survive EXACTLY ---------------------------------------------
buf = BookmapBuffer("AR", inst, max_cols=60)
T0 = 1_700_000_000_000
rng = np.random.default_rng(5)
sent_vol = 0
sent_net = 0
mid = 400.0
for k in range(3000):                      # 3,000 seconds = 50 minutes
    ts = T0 + k * 1000
    mid += float(rng.normal(0, 0.01))
    for _ in range(4):
        px = round(mid + rng.integers(-3, 4) * 0.01, 2)
        sz = int(rng.integers(50, 900))
        ag = (Aggressor.BUY, Aggressor.SELL)[int(rng.integers(0, 2))]
        buf.add_trade(Trade("AR", px, sz, ag, ts))
        sent_vol += sz
        sent_net += sz if ag is Aggressor.BUY else -sz
    bids = {round(mid - i * 0.01, 2): 400 + i * 3 for i in range(1, 41)}
    asks = {round(mid + i * 0.01, 2): 400 + i * 3 for i in range(1, 41)}
    # TWO walls, deliberately different in DURATION - that is the property
    # time-averaging encodes and the reason a single-column wall is not a
    # useful fixture. A wall that stands for two minutes is real size; one
    # that shows for a single second is a flash and should read as one.
    if 1500 <= k < 1620:                    # a wall that STANDS - 2 minutes
        bids[round(mid - 0.10, 2)] = 250_000
    if k == 900:                            # a flash - one second only
        bids[round(mid - 0.12, 2)] = 250_000
    buf.add_book(BookSnapshot("AR", bids, asks, ts))

arch = buf.archive
arch.flush()
live_vol = sum(c.vol for c in buf.columns())
arch_vol = sum(s.vol for s in arch.slots())
live_net = sum(c.net for c in buf.columns())
arch_net = sum(s.net for s in arch.slots())

check("every print is accounted for - archived plus still-live equals what was "
      "sent, exactly", arch_vol + live_vol == sent_vol,
      f"{arch_vol:,} archived + {live_vol:,} live = {arch_vol + live_vol:,} "
      f"vs {sent_vol:,} sent")
check("...and signed delta too, exactly",
      arch_net + live_net == sent_net,
      f"{arch_net + live_net:,} vs {sent_net:,}")
check("nothing was folded out of order and dropped",
      arch.stats()["dropped_late"] == 0, f"{arch.stats()}")

# ---- 3. A WALL STILL READS AS A WALL ---------------------------------------
# The one that matters. Compression that made the wall indistinguishable from
# the surrounding book would pass every memory test and be useless.
def slot_at(k):
    """The slot covering loop step `k`.

    Column buckets are ABSOLUTE (epoch seconds // col_dt), not relative to the
    start of the fixture, so the loop index has to be put back on the epoch
    before it can be turned into an archive bucket.
    """
    b = int((T0 / 1000.0 + k) // arch.col_s)
    return next((s for s in arch.slots() if s.bucket == b), None)


standing = slot_at(1530)          # inside the two-minute wall
flash = slot_at(900)              # the one-second flash
quiet = slot_at(300)              # ordinary book


def peak_and_typical(s):
    z = s.sizes()
    nz = z[z > 0]
    return float(z.max()), float(np.median(nz)) if nz.size else 0.0


check("a slot exists for each of the three regions", 
      standing is not None and flash is not None and quiet is not None,
      f"standing={standing is not None} flash={flash is not None} "
      f"quiet={quiet is not None}")
if standing and flash and quiet:
    sp, st = peak_and_typical(standing)
    fp, ft = peak_and_typical(flash)
    qp, qt = peak_and_typical(quiet)
    check("a wall that STOOD for two minutes is far brighter than the "
          "ordinary book around it - this is the whole reason to keep the "
          "session at all",
          sp > st * 20, f"peak {sp:,.0f} vs median {st:,.0f} "
                        f"({sp / max(st, 1):.0f}x)")
    check("...and far brighter than a quiet slot's peak",
          sp > qp * 8, f"{sp:,.0f} vs {qp:,.0f}")
    check("a one-second FLASH of the same size reads much dimmer, because it "
          "really was only there for a moment - time-averaging is honest "
          "about duration rather than flattering a spoof",
          fp < sp / 8, f"flash {fp:,.0f} vs standing {sp:,.0f} "
                       f"({sp / max(fp, 1):.1f}x apart)")

# ---- 4. it is BOUNDED ------------------------------------------------------
a = SessionArchive(col_s=30.0, tick_step=4, max_slots=100)


class FakeCol:
    __slots__ = ("bucket", "book", "buy", "sell", "bid_ti", "ask_ti", "vol",
                 "net", "sweeps")

    def __init__(self, bucket, book, vol=0, net=0):
        self.bucket = bucket
        self.book = book
        self.buy = {}
        self.sell = {}
        self.bid_ti = None
        self.ask_ti = None
        self.vol = vol
        self.net = net
        self.sweeps = 1


from omnitrix.engine.model import PriceLadder
ladder = PriceLadder(np.arange(40000, 40200, dtype=np.int32),
                     np.full(200, 500, dtype=np.int32))
for k in range(30 * 400):                  # 400 slots' worth
    a.fold(FakeCol(k, ladder, vol=10, net=2))
check("the archive is hard-capped - it cannot become the thing it replaced",
      len(a.slots()) <= 100, f"{len(a.slots())} slots, cap 100")

# ---- 5. the size claim, measured on a real session shape -------------------
# 6.5 hours at the shipped defaults, with a realistic 100-level book.
full = SessionArchive()
lad = PriceLadder(np.arange(40000, 40200, dtype=np.int32),
                  np.full(200, 700, dtype=np.int32))
SESSION_S = int(6.5 * 3600)
for k in range(SESSION_S):
    c = FakeCol(k, lad, vol=400, net=20)
    c.buy = {40100 + (k % 7): 200}
    c.sell = {40090 + (k % 5): 180}
    a2 = full.fold(c)
full.flush()
per_sym = full.nbytes()
check("a whole 6.5-hour session fits in well under 300 kB a symbol",
      per_sym < 300_000,
      f"{per_sym / 1024:.0f} kB for {len(full.slots())} slots "
      f"({SESSION_S:,} live columns)")
check("...so 200 symbols is tens of megabytes, not gigabytes",
      per_sym * 200 < 80e6,
      f"{per_sym * 200 / 1e6:.0f} MB across 200 symbols")
check("the session really is covered end to end", full.span_s() is not None
      and full.span_s()[1] - full.span_s()[0] >= SESSION_S - ARCH_COL_S,
      f"span {full.span_s()}")

# ---- 6. reading a window back ----------------------------------------------
sp = full.span_s()
mid_t = (sp[0] + sp[1]) / 2
w = full.view(mid_t, mid_t + 600)
check("a time window reads back the slots that cover it",
      bool(w) and all(mid_t - ARCH_COL_S <= s.bucket * full.col_s
                      <= mid_t + 600 + ARCH_COL_S for s in w),
      f"{len(w)} slots for a 10-minute window")
check("...in ascending time order, like every other series in the app",
      all(w[i].bucket < w[i + 1].bucket for i in range(len(w) - 1)))
check("an empty window returns nothing rather than raising",
      full.view(sp[1] + 10_000, sp[1] + 20_000) == [])

# ---- 7. the compression ratios are what the docstring claims ---------------
check("time is folded by the documented factor",
      abs(full.col_s / 1.0 - ARCH_COL_S) < 1e-9, f"{full.col_s}s per slot")
check("price is folded by the documented factor",
      full.tick_step == ARCH_TICK_STEP, f"{full.tick_step} ticks per bin")
one = full.slots()[len(full.slots()) // 2]
check("a slot stores three bytes per bin and nothing per-tick",
      one.heat.dtype == np.uint8 and one.buyq.dtype == np.uint8
      and one.sellq.dtype == np.uint8,
      f"{one.heat.size} bins, {one.nbytes()} B")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("SESSION ARCHIVE OK")
