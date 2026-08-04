"""Gate: the frame governor must help when the desk is overloaded and do
NOTHING when it is not.

Pure arithmetic on FrameGovernor - no Qt, no windows, no wall-clock timing - so
it runs anywhere and cannot go flaky.

The second half of that sentence is the part with scars on it. Two earlier
versions of this governor made the application SLOWER than not governing at
all, and both times the unit test passed because it only checked that
throttling happened, never that it was warranted:

  * v1 modelled cost as paint-time per frame over the requested interval. Qt
    paints on its own schedule, not once per refresh, so demand read 125-190%
    and a single chart was throttled from 20.7 fps to 13.5 fps.
  * v2 measured lateness but only decayed the throttle below a second, lower
    threshold. Steady-state lateness sat BETWEEN the two thresholds, so the
    startup backlog ratcheted the scale up and nothing ever brought it back.

So the first thing this gate asserts is that an on-time desk is left alone.
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from omnitrix.ui.framegov import (
    FrameGovernor, LATE_HIGH, MAX_SCALE, MAX_INTERVAL_MS,
)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def build(*windows) -> FrameGovernor:
    """windows: (key, base_ms, priority)"""
    g = FrameGovernor()
    for key, base, prio in windows:
        g.register(key, base, prio)
    return g


def run(g: FrameGovernor, lateness: float, frames: int = 400) -> None:
    """Feed the loop a steady observed lateness and let it settle."""
    for _ in range(frames):
        for key in list(g._reg):
            granted = g.interval(key)
            g.note_gap(key, granted / 1000.0 * lateness)


print("frame governor")

# ---- 1. an on-time desk must be left completely alone ----------------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
run(g, lateness=1.0)
check("on time: the main chart keeps its full rate", g.interval(1) == 33,
      f"granted {g.interval(1)}ms, scale {g.scale():.2f}")
check("on time: the bookmap keeps its full rate", g.interval(2) == 80,
      f"granted {g.interval(2)}ms")

# ---- 2. normal slack is NOT overload --------------------------------------
# Our callback returns before Qt paints, so a healthy window still measures
# ~1.18. Reacting to that is what made v2 throttle an idle machine.
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
run(g, lateness=1.18)
check("routine slack does not trigger throttling", g.interval(1) == 33,
      f"at lateness 1.18 -> scale {g.scale():.2f}")

# ---- 3. genuine overload IS throttled -------------------------------------
g = build((1, 33, 0), *[(i, 80, 1) for i in range(2, 6)])
g.set_focus(1)
run(g, lateness=2.1)
check("real overload throttles", g.scale() > 1.2, f"scale {g.scale():.2f}")
main_stretch = g.interval(1) / 33
bm_stretch = g.interval(2) / 80
check("the focused chart is throttled LESS than the background",
      main_stretch < bm_stretch,
      f"main x{main_stretch:.2f} ({g.interval(1)}ms), "
      f"background x{bm_stretch:.2f} ({g.interval(2)}ms)")

# ---- 4. THE v2 BUG: the throttle must come back off ------------------------
run(g, lateness=1.0)
check("the throttle is released once frames are on time again",
      g.interval(1) == 33 and g.interval(2) == 80,
      f"scale {g.scale():.2f}, main {g.interval(1)}ms, bg {g.interval(2)}ms")

# ---- 5. a startup backlog must not leave a permanent throttle --------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
run(g, lateness=3.5, frames=200)      # prefill: genuinely far behind
stuck = g.scale()
run(g, lateness=1.15, frames=400)     # settled: normal slack
check("a startup backlog does not permanently throttle the desk",
      g.interval(1) == 33,
      f"scale {stuck:.2f} during backlog -> {g.scale():.2f} after")

# ---- 6. bounds -------------------------------------------------------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
run(g, lateness=4.0, frames=1000)
check("the throttle is bounded", g.scale() <= MAX_SCALE + 1e-9,
      f"scale {g.scale():.2f} vs ceiling {MAX_SCALE}")
check("no window is stretched past the floor",
      max(g.interval(k) for k in g._reg) <= MAX_INTERVAL_MS,
      f"worst {max(g.interval(k) for k in g._reg)}ms")
check("no window is ever made FASTER than it asked for",
      g.interval(1) >= 33 and g.interval(2) >= 80)

# ---- 7. one bad frame must not slam the desk ------------------------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
run(g, lateness=1.0)
g.note_gap(1, 5.0)                     # a 5-second GC pause / resize stall
check("a single pathological frame does not collapse the rate",
      g.interval(1) <= 40, f"granted {g.interval(1)}ms after one 5 s stall")

# ---- 8. hidden windows do not drive the loop ------------------------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
g.set_alive(2, False)
before = g.scale()
for _ in range(200):
    g.note_gap(2, 1.0)                 # a hidden window reporting nonsense
check("a hidden window cannot throttle the visible ones",
      abs(g.scale() - before) < 1e-9, f"scale {before:.2f} -> {g.scale():.2f}")

# ---- 9. bookkeeping --------------------------------------------------------
g = build((1, 33, 0), (2, 80, 1))
g.set_focus(1)
g.unregister(1)
check("closing the focused window clears focus", g._focus is None)
check("an unknown key still returns a usable interval", g.interval(999) == 33)

print()
if FAILS:
    print(f"FRAME GATE FAILED: {len(FAILS)}")
    for f in FAILS:
        print(f"   - {f}")
    sys.exit(1)
print("FRAME GATE OK")
