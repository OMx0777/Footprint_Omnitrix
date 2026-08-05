"""What does 100 clients actually cost the broadcaster, and what does a day of
data actually weigh? Measured, because both numbers decide the architecture."""
import asyncio, os, struct, sys, time, statistics

sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")

L1_SIZE, L2_SIZE = 104, 32
# Measured census from the live DLL: 173 book sweeps/sec across 100 symbols,
# each sweep up to 256 levels; L1 arrives per symbol per change.
SWEEPS_PER_SEC = 173
LEVELS_PER_SWEEP = 256
L1_PER_SEC = 100 * 12          # 100 symbols, ~12 L1 updates/sec each

l2_bytes = SWEEPS_PER_SEC * LEVELS_PER_SWEEP * (L2_SIZE + 1)
l1_bytes = L1_PER_SEC * (L1_SIZE + 1)
feed = l2_bytes + l1_bytes
print("=== FEED RATE (from the measured live census) ===")
print(f"  L2  {SWEEPS_PER_SEC}/s x {LEVELS_PER_SWEEP} levels x {L2_SIZE+1} B "
      f"= {l2_bytes/1e6:6.2f} MB/s")
print(f"  L1  {L1_PER_SEC}/s x {L1_SIZE+1} B                = {l1_bytes/1e6:6.2f} MB/s")
print(f"  TOTAL                                    = {feed/1e6:6.2f} MB/s")

print("\n=== EGRESS, one copy per client (what TCP unicast does) ===")
for n in (1, 10, 50, 100):
    bps = feed * n * 8
    print(f"  {n:3d} clients: {feed*n/1e6:8.1f} MB/s = {bps/1e9:5.2f} Gbit/s"
          f"   {'OK on 1GbE' if bps < 0.7e9 else 'EXCEEDS 1GbE' if bps < 9e9 else 'EXCEEDS 10GbE'}")
print(f"\n  multicast: {feed/1e6:.2f} MB/s regardless of client count "
      f"({feed*8/1e9:.3f} Gbit/s)")

print("\n=== SERVER CPU: the per-client fanout loop ===")
# The real cost in broadcast(): one call_soon_threadsafe per client per batch.
loop = asyncio.new_event_loop()
class C:
    __slots__ = ("queue", "dropped")
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=2000)
        self.dropped = 0
    def offer(self, data):
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1

async def bench(n_clients, batches):
    cs = [C() for _ in range(n_clients)]
    payload = b"x" * 8192
    t = time.perf_counter()
    for _ in range(batches):
        for c in cs:
            loop.call_soon_threadsafe(c.offer, payload)
        await asyncio.sleep(0)
        # drain so the queues do not fill and change the measurement
        for c in cs:
            while not c.queue.empty():
                c.queue.get_nowait()
    return time.perf_counter() - t

BATCHES = 400
for n in (1, 10, 50, 100):
    dt = loop.run_until_complete(bench(n, BATCHES))
    per_batch_ms = dt / BATCHES * 1000
    # batches/sec the feed actually produces (one per pipe read, ~64 kB)
    feed_batches = feed / 65536
    load = per_batch_ms / 1000 * feed_batches
    print(f"  {n:3d} clients: {per_batch_ms:6.3f} ms/batch  ->  "
          f"{load*100:5.1f}% of one core at the live feed rate")
loop.close()

print("\n=== STORAGE, one trading day ===")
SESSION_H = 6.5
raw = feed * 3600 * SESSION_H
print(f"  raw wire bytes, 6.5 h session      : {raw/1e9:6.1f} GB")
print(f"  free on the server (466-70)        :  396   GB")
print(f"  days retained at raw               : {396e9/raw:6.1f}")
