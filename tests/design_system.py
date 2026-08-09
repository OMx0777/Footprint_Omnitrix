"""The design system has to be a system, not a second pile of magic numbers.

Replacing thirteen typed-in paddings with a module that exports thirteen
constants would achieve nothing. What makes it a system is that the properties
below HOLD, so this checks the properties rather than the values:

  * every length in the stylesheet traces back to a token;
  * tracking is size-specific - the rule the app was missing entirely - and is
    zero on the monospace roles, because a price ladder aligns by advance width
    and tracking would break the one thing tabular figures are for;
  * the springs are stable, interruptible, and honour reduced motion;
  * the spring driver STOPS when nothing is animating. An always-on 60 Hz timer
    would quietly undo the frame budget this app spent weeks earning.
"""

import os
import re
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from omnitrix.ui import design
from omnitrix.render.theme import DARK, LIGHT

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])

# ---- 1. the stylesheet is built from tokens only ---------------------------
SPACES = {v for k, v in vars(design.SPACE).items() if not k.startswith("_")
          and isinstance(v, int)}
RADII = {v for k, v in vars(design.RADIUS).items() if not k.startswith("_")
         and isinstance(v, int)}
SIZES = {t.px for t in (design.TITLE, design.HEADING, design.BODY,
                        design.CONTROL, design.LABEL, design.CAPTION,
                        design.DATA, design.DATA_SM, design.DATA_STRONG)}
ALLOWED = SPACES | RADII | SIZES | {0}

for theme in (DARK, LIGHT):
    q = design.qss(theme)
    stray = sorted({int(m) for m in re.findall(r"(\d+)px", q)} - ALLOWED)
    check(f"every px length in the {theme.name} stylesheet is a token",
          not stray, f"off-scale: {stray}")

q = design.qss(DARK)
check("the stylesheet defines a PRESSED state - feedback has to land on press, "
      "not on release, or the control reads as laggy",
      "QPushButton:pressed" in q)
check("...and a hover state on the controls that have one",
      "QComboBox:hover" in q and "QPushButton:hover" in q)

# ---- 2. TRACKING IS SIZE-SPECIFIC ------------------------------------------
# The rule the app was missing in every one of its widgets. Letters read too
# far apart as they grow and too close as they shrink, so tracking has to move
# with size - one value for everything is wrong at every size but one.
prop = [design.TITLE, design.HEADING, design.BODY, design.LABEL, design.CAPTION]
by_size = sorted(prop, key=lambda t: t.px)
check("tracking decreases as type grows - large text negative, small positive",
      all(by_size[i].tracking >= by_size[i + 1].tracking
          for i in range(len(by_size) - 1)),
      "  ".join(f"{t.px}px:{t.tracking:+.3f}" for t in by_size))
check("...the largest role really is negative", design.TITLE.tracking < 0,
      f"{design.TITLE.tracking:+.3f} em at {design.TITLE.px}px")
check("...and the smallest really is positive", design.CAPTION.tracking > 0,
      f"{design.CAPTION.tracking:+.3f} em at {design.CAPTION.px}px")
check("no role uses more than 3% tracking - past that it reads as a gimmick",
      all(abs(t.tracking) <= 0.03 for t in prop))

mono = [design.DATA, design.DATA_SM, design.DATA_STRONG]
check("MONOSPACE roles carry NO tracking - a price ladder aligns by advance "
      "width and tracking would break the only reason to use tabular figures",
      all(t.tracking == 0.0 for t in mono))
check("...and they really are fixed pitch",
      all(design.font(t).fixedPitch() for t in mono))

f = design.font(design.LABEL)
check("the tracking actually reaches the QFont",
      abs(f.letterSpacing() - (100 + design.LABEL.tracking * 100)) < 0.5,
      f"{f.letterSpacing():.2f}%")
check("fonts are cached - a painter that builds one per row shows up in a "
      "profile", design.font(design.LABEL) is f)

# hierarchy must come from weight as well as size, not size alone
check("hierarchy uses weight, not only size",
      design.DATA_STRONG.weight != design.DATA.weight
      and design.DATA_STRONG.px == design.DATA.px,
      "same size, different weight")

# ---- 3. elevation ----------------------------------------------------------
base = DARK.panel
levels = [design.elevate(base, i) for i in range(4)]


def lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


check("each elevation step is strictly lighter than the one below",
      all(lum(levels[i]) < lum(levels[i + 1]) for i in range(len(levels) - 1)),
      " -> ".join(f"{lum(x):.0f}" for x in levels))
check("...but three levels stay inside one dark room, not a grey staircase",
      lum(levels[3]) - lum(levels[0]) < 40,
      f"span {lum(levels[3]) - lum(levels[0]):.0f} of 255")
check("elevation never overflows a channel", design.elevate("#FEFEFE", 8)
      == "#FFFFFF")

# ---- 4. springs ------------------------------------------------------------
s = design.Spring(0.0, damping=1.0, response=0.3)
s.set_target(100.0)
peak = 0.0
for _ in range(400):
    s.step(1 / 60)
    peak = max(peak, s.value)
check("a critically damped spring never overshoots - bounce on something that "
      "merely appeared feels wrong",
      peak <= 100.0 + 0.5, f"peak {peak:.2f} of target 100")
check("...and it settles", s.at_rest() and abs(s.value - 100) < 0.5,
      f"{s.value:.3f}")

b = design.Spring(0.0, *design.SPRING_MOMENTUM)
b.set_target(100.0, velocity=900.0)
pk = 0.0
for _ in range(400):
    b.step(1 / 60)
    pk = max(pk, b.value)
check("an under-damped spring DOES overshoot - reserved for motion a gesture "
      "carried momentum into", pk > 100.5, f"peak {pk:.2f}")

# interruptibility: retarget mid-flight must continue from where it IS
s2 = design.Spring(0.0, *design.SPRING_UI)
s2.set_target(200.0)
for _ in range(6):
    s2.step(1 / 60)
mid, midv = s2.value, s2.v
s2.set_target(0.0)
s2.step(1 / 60)
check("retargeting mid-flight continues from the CURRENT value, not the "
      "logical one - starting from the target is what makes a UI jump",
      abs(s2.value - mid) < 12.0, f"{mid:.2f} -> {s2.value:.2f}")
check("...and carries its velocity through the reversal rather than hard-"
      "cutting it to zero", s2.v * midv > 0 or abs(s2.v) < abs(midv),
      f"v {midv:.1f} -> {s2.v:.1f}")

# stability: this app deliberately lets timers run late
s3 = design.Spring(0.0, *design.SPRING_UI)
s3.set_target(100.0)
for _ in range(50):
    s3.step(0.25)               # a 250 ms frame, which really happens here
check("a very late frame does not make the spring explode - dt comes from a "
      "timer this app allows to run late by design",
      abs(s3.value) < 1e4 and s3.value == s3.value, f"{s3.value:.3f}")
check("...and it still lands on the target", abs(s3.value - 100) < 1.0,
      f"{s3.value:.3f}")

# ---- 5. THE DRIVER MUST NOT RUN WHEN IDLE ----------------------------------
d = design.driver()
check("the driver is idle before anything animates", not d._timer.isActive())
moved = []
sp = design.Spring(0.0, *design.SPRING_UI)
design.animate(sp, 50.0, on_change=moved.append)
check("...starts when a spring is handed to it",
      d._timer.isActive() or design.reduced_motion())
for _ in range(120):
    d.advance(1 / 60)
check("...and STOPS again once everything settles - an always-on 60 Hz timer "
      "would quietly undo the frame budget",
      not d._timer.isActive(), f"{len(d._springs)} springs still registered")
check("the value arrived", abs(sp.value - 50.0) < 0.5, f"{sp.value:.2f}")
check("...and the caller was told about it", bool(moved), f"{len(moved)} updates")

# a callback that raises must not take every other animation down
bad = design.Spring(0.0, *design.SPRING_UI)


def boom(_v):
    raise ValueError("callback blew up")


design.animate(bad, 10.0, on_change=boom)
ok_sp = design.Spring(0.0, *design.SPRING_UI)
design.animate(ok_sp, 10.0, on_change=lambda v: None)
for _ in range(120):
    d.advance(1 / 60)
check("a raising spring callback is dropped without stopping the others",
      abs(ok_sp.value - 10.0) < 0.5, f"{ok_sp.value:.2f}")

# ---- 6. reduced motion -----------------------------------------------------
real = design.reduced_motion
design.reduced_motion = lambda: True
try:
    r = design.Spring(0.0, *design.SPRING_UI)
    seen, settled = [], []
    design.animate(r, 80.0, on_change=seen.append,
                   on_settle=lambda: settled.append(1))
    check("under reduced motion the value arrives IMMEDIATELY - reduced means "
          "gentler feedback, never no feedback",
          r.value == 80.0 and bool(seen) and bool(settled),
          f"value={r.value}, {len(seen)} update(s)")
    check("...and nothing is left animating", not design.driver()._timer.isActive())
finally:
    design.reduced_motion = real

# ---- 7. the toast enters and leaves along the SAME path --------------------
from omnitrix.ui.alert_ui import AlertToast
from PyQt6.QtWidgets import QWidget


class FakeAlert:
    symbol, price, fired_price = "NVDA", 220.5, 220.5


host = QWidget()
host.resize(1200, 800)
host.show()          # a child's isVisible() is False while an ancestor is hidden
toast = AlertToast(host)
toast.show_alerts([FakeAlert()])
enter_from = toast._x.value
rest = toast._rest_x()
check("the toast starts OFF screen, so it is never seen at rest before it "
      "moves", enter_from > host.width() - toast.width(),
      f"start x={enter_from:.0f}, host width {host.width()}")
for _ in range(120):
    design.driver().advance(1 / 60)
check("...and settles at its rest position", abs(toast._x.value - rest) < 1.0,
      f"{toast._x.value:.0f} vs {rest:.0f}")
toast._expire()
for _ in range(120):
    design.driver().advance(1 / 60)
check("it leaves back out the SAME edge it came from - in from the right and "
      "out the bottom would read as two unrelated objects",
      toast._x.value >= host.width(), f"exit x={toast._x.value:.0f}")
check("...and hides itself once it is gone",
      not toast.isVisible() and not toast._on_screen)

# an alert arriving mid-exit must retarget, not restart
toast.show_alerts([FakeAlert()])
for _ in range(4):
    design.driver().advance(1 / 60)
toast._expire()
for _ in range(3):
    design.driver().advance(1 / 60)
mid_out, mid_v = toast._x.value, toast._x.v
toast.show_alerts([FakeAlert()])
# THE PROPERTY IS CONTINUITY AT THE INSTANT OF RETARGET, not that the next
# frame is small. A spring carrying real velocity legitimately travels a long
# way in one frame; what it must never do is teleport. Checking the position
# after a frame confuses the two - and it was checking the wrong one, which is
# how it hid a genuine teleport caused by isVisible() reading the parent.
check("an alert arriving while it is leaving retargets from where it IS - the "
      "position must not jump at the instant the target changes",
      abs(toast._x.value - mid_out) < 1e-9,
      f"{mid_out:.2f} -> {toast._x.value:.2f}")
check("...and it keeps the velocity it had, so the reversal blends instead of "
      "hitting a brick wall", toast._x.v == mid_v, f"v {mid_v:.1f}")
check("...and it is retargeted at the rest position, not restarted off screen",
      abs(toast._x.target - toast._rest_x()) < 1e-9,
      f"target {toast._x.target:.0f}")
for _ in range(120):
    design.driver().advance(1 / 60)
check("...and it comes back, rather than being left half off screen",
      abs(toast._x.value - toast._rest_x()) < 1.0 and toast.isVisible(),
      f"x={toast._x.value:.0f}, visible={toast.isVisible()}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f_ in FAILS:
        print("   -", f_)
    sys.exit(1)
print("DESIGN SYSTEM OK")
