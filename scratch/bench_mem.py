import sys, os, time, psutil
import numpy as np
from collections import deque

sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from omnitrix.engine.model import BookSnapshot, EMPTY_LADDER
from omnitrix.engine import Instruments
from omnitrix.engine.bookmap import BookmapBuffer
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainter, QImage

app = QApplication(sys.argv)
inst = Instruments(default_tick=0.01)

def make_book(sym, mid, n_levels, ts):
    bids, asks = {}, {}
    for i in range(1, n_levels+1):
        bids[mid - i * 0.01] = i * 10
        asks[mid + i * 0.01] = i * 10
    return BookSnapshot(sym, bids, asks, ts)

def run_bench(bound=None):
    process = psutil.Process(os.getpid())
    mem0 = process.memory_info().rss / 1024 / 1024
    
    buffers = [BookmapBuffer(f"SYM{i}", inst, max_cols=1400) for i in range(100)]
    
    # Fill with 1400 columns
    # We'll just do a subset to save time if 1400 is too slow for 100 syms
    cols = 1400
    for c in range(cols):
        for b in buffers:
            bk = make_book(b.symbol, 100.0, 400, c * 1000)
            if bound:
                # manual bound
                mid_ti = 10000
                keep_b = {k: v for k, v in bk.bids.items() if round(k/0.01) >= mid_ti - bound}
                keep_a = {k: v for k, v in bk.asks.items() if round(k/0.01) <= mid_ti + bound}
                bk = BookSnapshot(bk.symbol, keep_b, keep_a, bk.ts_ms)
            b.add_book(bk)
            
    mem1 = process.memory_info().rss / 1024 / 1024
    
    print(f"Memory (MB): {mem0:.1f} -> {mem1:.1f}")

if __name__ == "__main__":
    bound = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run_bench(bound)
