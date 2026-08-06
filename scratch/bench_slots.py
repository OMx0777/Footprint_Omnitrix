import sys, os, time, psutil

class BarNoSlots:
    def __init__(self):
        self.start_ts = 0
        self.open = 0
        self.high = 0
        self.low = 0
        self.close = 0
        self.volume = 0
        self.delta = 0
        self.cells = None
        self._dirty = True
        self._cache = {}
        self._agg = None
        self._ti = None
        self._sell = None
        self._buy = None
        self._imb = None

class BarSlots:
    __slots__ = ['start_ts', 'open', 'high', 'low', 'close', 'volume', 'delta', 'cells',
                 '_dirty', '_cache', '_agg', '_ti', '_sell', '_buy', '_imb']
    def __init__(self):
        self.start_ts = 0
        self.open = 0
        self.high = 0
        self.low = 0
        self.close = 0
        self.volume = 0
        self.delta = 0
        self.cells = None
        self._dirty = True
        self._cache = {}
        self._agg = None
        self._ti = None
        self._sell = None
        self._buy = None
        self._imb = None

def bench(cls, name):
    mem0 = psutil.Process().memory_info().rss / 1024 / 1024
    objs = [cls() for _ in range(1200000)]
    mem1 = psutil.Process().memory_info().rss / 1024 / 1024
    print(f"{name}: {mem1-mem0:.1f} MB")
    
bench(BarNoSlots, "No slots")
bench(BarSlots, "Slots")
