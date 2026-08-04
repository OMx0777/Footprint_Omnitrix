"""Gate: the frame budget must actually bound total UI demand.

This is pure arithmetic on FrameGovernor - no Qt, no windows, no timing - so it
runs anywhere and cannot go flaky. What it protects is the one property the
whole design rests on:

    whatever the mix of windows, the demand the UI PLACES on the GUI thread
    after throttling must not exceed TARGET.

If that ever stops holding, frames get dropped by accident again.
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from omnitrix.ui.framegov import FrameGovernor, TARGET, MAX_INTERVAL_MS

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def actual_demand(g: FrameGovernor) -> float:
    """Demand once every window is running at the interval it was GRANTED."""
    total = 0.0
    for key, e in g._reg.items():
        if not e.alive:
            continue
        total += e.cost_s / (g.interval(key) / 1000.0)
    return total


def build(*windows) -> FrameGovernor:
    """windows: (key, base_ms, cost_ms, priority)"""
    g = FrameGovernor()
    for key, base, cost, prio in windows:
        g.register(key, base, prio)
        e = g._reg[key]
        e.refresh_s = 0.0
        e.paint_s = cost / 1000.0
    return g


print("frame budget")

# ---- 1. under budget: nobody is touched ------------------------------------
g = build((1, 33, 5, 0), (2, 80, 5, 1))
g.set_focus(1)
check("under budget the main window keeps its rate", g.interval(1) == 33,
      f"got {g.interval(1)}ms")
check("under budget a bookmap keeps its rate", g.interval(2) == 80,
      f"got {g.interval(2)}ms")

# ---- 2. THE measured case: main + 4 bookmaps -------------------------------
g = build((1, 33, 26, 0), *[(i, 80, 11, 1) for i in range(2, 6)])
g.set_focus(1)
d0 = g.demand()
d1 = actual_demand(g)
# The MAX_INTERVAL_MS floor is deliberate and irreducible, so the guarantee is
# "within the budget plus whatever the floored windows cost at the floor" - we
# would rather overshoot a little than freeze a window that is on screen.
floor_cost = sum(e.cost_s / (MAX_INTERVAL_MS / 1000.0)
                 for k, e in g._reg.items() if k != 1)
check("main + 4 bookmaps is over budget before throttling", d0 > TARGET,
      f"raw demand {d0*100:.0f}%")
check("throttled demand is inside the budget", d1 <= TARGET + 1e-6,
      f"{d1*100:.0f}% vs target {TARGET*100:.0f}% "
      f"(floor accounts for {floor_cost*100:.0f}%)")
# The main window costs 26 ms every 33 ms - 79% - so it is over budget ON ITS
# OWN and cannot keep its full rate no matter what else is open. The invariant
# that matters is not "the chart is never throttled", it is that the chart is
# throttled PROPORTIONALLY LESS than the background windows.
main_stretch = g.interval(1) / 33
bm_stretch = g.interval(2) / 80
check("the focused chart is throttled less than the background",
      main_stretch < bm_stretch,
      f"main x{main_stretch:.2f} ({g.interval(1)}ms), "
      f"bookmap x{bm_stretch:.2f} ({g.interval(2)}ms)")

# ---- 3. hiding windows must GIVE BACK budget -------------------------------
# Hiding background windows must buy the FOCUSED window a faster rate - that
# is the whole point of the visibility guard feeding the budget.
before = g.interval(1)
for i in (3, 4, 5):
    g.set_alive(i, False)
after = g.interval(1)
check("hiding background windows speeds the focused one up", after < before,
      f"main {before}ms -> {after}ms with 3 of 4 bookmaps hidden")

# ---- 4. protected set alone over budget: it is throttled too ---------------
g = build((1, 33, 60, 0), (2, 80, 11, 1))
g.set_focus(1)
check("an unaffordable main window is throttled as well", g.interval(1) > 33,
      f"granted {g.interval(1)}ms")
check("and the others are pushed to the floor", g.interval(2) == MAX_INTERVAL_MS,
      f"granted {g.interval(2)}ms")

# ---- 5. nothing is ever starved completely ---------------------------------
g = build((1, 33, 200, 0), *[(i, 80, 50, 1) for i in range(2, 10)])
g.set_focus(1)
worst = max(g.interval(k) for k in g._reg)
check("no window is stretched past the floor", worst <= MAX_INTERVAL_MS,
      f"worst granted {worst}ms")

# ---- 6. a window is never made FASTER than it asked for --------------------
g = build((1, 33, 1, 0), (2, 80, 1, 1))
g.set_focus(1)
check("throttling never speeds a window up",
      g.interval(1) >= 33 and g.interval(2) >= 80)

# ---- 7. unregistering is clean --------------------------------------------
g = build((1, 33, 26, 0), (2, 80, 11, 1))
g.set_focus(1)
g.unregister(2)
check("closing a window removes its demand", abs(g.demand() - 26 / 33) < 1e-6,
      f"demand {g.demand()*100:.0f}%")
check("closing the focused window clears focus", g.unregister(1) is None
      and g._focus is None)

print()
if FAILS:
    print(f"FRAME GATE FAILED: {len(FAILS)}")
    for f in FAILS:
        print(f"   - {f}")
    sys.exit(1)
print("FRAME GATE OK")
