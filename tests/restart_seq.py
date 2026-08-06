"""A server restart must not reset the sequence: the day's file would then
hold two runs of 1..N, and a connected client would read the reset as
reordering rather than as the gap it actually is."""
import os, sys, shutil, tempfile, time
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Host_Omnitrix")
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
import recorder as rec_mod
from omnitrix.engine import wire
from omnitrix.engine.takion_decode import L2
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

L2_REC = b"\x02" + bytes(L2.size)
def batch(seq, n=3): return wire.encode(wire.CH_L2, seq, L2_REC*n, n)

root = tempfile.mkdtemp()
day = time.strftime("%Y-%m-%d")

# ---- run 1 -----------------------------------------------------------------
r1 = rec_mod.Recorder(root, retain_days=5)
for s in range(1, 5001): r1.write(wire.CH_L2, s, batch(s))
r1.flush()
check("run 1 recorded", rec_mod.seq_bounds(root, day, wire.CH_L2) == (1, 5000))

# ---- restart ---------------------------------------------------------------
r2 = rec_mod.Recorder(root, retain_days=5)
resume = r2.resume_from(wire.CH_L2)
check("the recorder reports where to resume", resume == 5000, f"resume_from -> {resume}")

for s in range(resume + 1, resume + 3001): r2.write(wire.CH_L2, s, batch(s))
r2.flush()
lo, hi = rec_mod.seq_bounds(root, day, wire.CH_L2)
check("the day's file is one continuous run", (lo, hi) == (1, 8000), f"{lo}..{hi}")

# every sequence must appear EXACTLY once
seen = {}
for b in rec_mod.read_range(root, day, wire.CH_L2, 1, None):
    h = wire.decode_header(b)
    seen[h[2]] = seen.get(h[2], 0) + 1
dupes = [s for s, c in seen.items() if c != 1]
check("no sequence appears twice", not dupes, f"{len(dupes)} duplicated")
check("all 8,000 are present", len(seen) == 8000, f"{len(seen)}")

# a seek across the restart boundary must land correctly
got = [wire.decode_header(b)[2]
       for b in rec_mod.read_range(root, day, wire.CH_L2, 4995, 5005)]
check("a seek across the restart boundary is exact",
      got == list(range(4995, 5006)), f"{got}")

# ---- and what the client sees ----------------------------------------------
g = wire.GapDetector()
g.observe(wire.CH_L2, 5000)
missed = g.observe(wire.CH_L2, 5001 + 40)     # 40 lost during the outage
check("the client sees the restart as a GAP, not as reordering",
      missed == 40 and g.reordered == 0,
      f"missed={missed} reordered={g.reordered}")

shutil.rmtree(root, ignore_errors=True)
print()
if FAILS: print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("RESTART SEQUENCE OK")
