import sys, os, time, psutil
import numpy as np

sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
from omnitrix.engine.bars import BarSeries, Bar
from omnitrix.engine import Instruments, Trade
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
inst = Instruments(default_tick=0.01)

def run_bench():
    process = psutil.Process(os.getpid())
    mem0 = process.memory_info().rss / 1024 / 1024
    
    series_list = [BarSeries(f"SYM{i}", inst, base_tf_s=10, max_bars=12000) for i in range(100)]
    
    # 12000 bars per symbol, each with 50 levels of footprint
    for b_idx in range(1200):
        ts = b_idx * 10000
        for s in series_list:
            for lvl in range(50):
                s.add_trade(Trade(s.symbol, 100.0 + lvl*0.01, 100, 1, ts + lvl*10))
                
    mem1 = process.memory_info().rss / 1024 / 1024
    print(f"Memory (MB) for 100 symbols x 1200 bars (1/10th max): {mem0:.1f} -> {mem1:.1f}")

if __name__ == "__main__":
    run_bench()
