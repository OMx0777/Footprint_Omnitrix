import sys, os, time
import numpy as np

sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from omnitrix.engine.model import BookSnapshot, EMPTY_LADDER

def make_book(sym, mid, n_levels):
    bids, asks = {}, {}
    for i in range(1, n_levels+1):
        bids[mid - i * 0.01] = i * 10
        asks[mid + i * 0.01] = i * 10
    return BookSnapshot(sym, bids, asks, 0)

b = make_book("TEST", 100.0, 400)
t0 = time.perf_counter()
for _ in range(1000):
    b._cache.clear()
    lad = b.ladder(0.01)
t1 = time.perf_counter()
print(f"Time for 1000 ladders: {(t1-t0)*1000:.1f} ms")
print(f"Ladder size: {len(lad)}")
