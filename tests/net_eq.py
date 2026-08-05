"""The incrementally-maintained Column.net must EQUAL the old sum() of the
buy/sell dicts, for every column, including aggregated ones. A colour that
disagrees with the data is false data, however cheap it is to compute."""
import os, sys, time, logging
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from omnitrix.engine import Instruments, SyntheticFeed, BookmapBuffer
logging.basicConfig(level=logging.CRITICAL)
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

inst = Instruments(default_tick=0.01)
buf = BookmapBuffer("QQQ", inst)
feed = SyntheticFeed(symbols=["QQQ"], start_price=400.0, tick=0.01,
                     trades_per_sec=400, prefill_minutes=25, seed=17)
feed.on_trade(buf.add_trade)
feed.on_book(buf.add_book)
feed.start(); time.sleep(2.0); feed.stop()

base = buf.view(1)
bad = [(c.bucket, c.net, sum(c.buy.values())-sum(c.sell.values()))
       for c in base if c.net != sum(c.buy.values())-sum(c.sell.values())]
check(f"base columns agree ({len(base)} checked)", not bad, str(bad[:3]))

# Aggregated views fold columns together - the fold must carry net too.
for agg in (5, 10, 30, 60, 300):
    cols = buf.view(agg)
    bad = [(c.bucket, c.net, sum(c.buy.values())-sum(c.sell.values()))
           for c in cols if c.net != sum(c.buy.values())-sum(c.sell.values())]
    check(f"agg={agg:<4d} agrees ({len(cols)} cols)", not bad, str(bad[:2]))

# and the sign, which is all the renderer actually uses
signs = sum(1 for c in base if (c.net >= 0) != ((sum(c.buy.values())-sum(c.sell.values())) >= 0))
check("bar colour (sign of net) never differs", signs == 0, f"{signs} mismatches")
nz = sum(1 for c in base if c.net != 0)
check("the test actually exercised non-zero deltas", nz > 50, f"{nz} non-zero columns")
print()
if FAILS: print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("NET DELTA EQUIVALENCE OK")
