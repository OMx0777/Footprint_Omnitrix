"""The design system: spacing, type, elevation and motion, in one place.

Applies Apple's interface principles to a dense, dark, data-first trading
terminal. Most of that guidance is written for consumer apps on the web, so
what follows is a TRANSLATION, and the places it deliberately departs are
called out rather than quietly dropped.

WHAT WAS WRONG BEFORE. Nothing here was systematic. Measured across the UI:
thirteen distinct padding values, four corner radii, font sizes chosen per
widget, and letter-spacing set in exactly zero places. Apple's craft principle
is that "nothing is random - every spacing, timing, alignment is deliberate and
defensible". None of it was defensible; it was just what each widget happened
to get written with.

THE THREE DEPARTURES, and why:

  * NO TRANSLUCENCY OR BACKDROP BLUR. The guidance builds chrome as blurred
    translucent layers. This app repaints a live chart against an 80 ms frame
    budget on one GUI thread, and a per-frame blur behind that chrome would
    cost more than the chart does. The principle underneath it - material
    weight encodes hierarchy - survives as OPAQUE ELEVATION: each layer is a
    measured step in luminance, so depth still reads, at zero per-frame cost.

  * THE DATA GRID KEEPS A MONOSPACE FACE. "Default to the platform's system
    font" is right for chrome and wrong for a price ladder: proportional
    figures make columns of numbers fail to line up, and a trader compares
    them vertically. Chrome moves to the system font; anything numeric stays
    on tabular figures.

  * SEMANTIC COLOUR IS NOT A DESIGN TOKEN. Bull/bear, imbalance and the
    bookmap heat ramp encode meaning and are sampled from real captures. They
    are not restyled here for visual consistency, because consistency is not
    what they are for.

Everything else - the spacing rhythm, size-specific tracking, weight-led
hierarchy, springs over scripted animation, interruptibility, symmetric
enter/exit paths, and honouring the reduced-motion setting - applies directly
and is implemented below.
"""

from __future__ import annotations

import logging
import math
import sys

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QFont

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- space
# One rhythm, base 4. Every margin, padding and gap in the app is one of these.
# The point is not that 4 is special - it is that a reader can see the SAME
# interval repeating, which is what makes a layout look composed rather than
# assembled. Two sub-steps below the base exist because dense chrome genuinely
# needs them; there are no others.
class SPACE:
    HAIR = 1        # a rule, a separator - never a gap
    XXS = 2
    XS = 4
    SM = 6
    MD = 8
    LG = 12
    XL = 16
    XXL = 24


# -------------------------------------------------------------------- radius
# Bigger surfaces get bigger radii. A 4 px radius on a dialog reads as a
# rectangle with damaged corners; a 10 px radius on a 20 px chip reads as a
# pill. Scaling the radius with the surface is what keeps both looking
# intentional.
class RADIUS:
    CHIP = 4        # combo boxes, small buttons, tags
    PANEL = 6       # docks, cards, toolbars
    SURFACE = 10    # dialogs, toasts, popovers


# ---------------------------------------------------------------------- type
# TRACKING IS SIZE-SPECIFIC. This is the single highest-value rule in the
# typography guidance and the one this app was most plainly missing (it set
# letter-spacing nowhere). Letters read as too far apart as they grow, and too
# close as they shrink, so large text takes NEGATIVE tracking and small text
# takes slightly positive. A single value across all sizes is wrong at every
# size except the one it was chosen for.
#
# Hierarchy is built from weight + size + tracking as a SET, not from size
# alone: on a terminal, screen space is the scarcest thing there is, and weight
# adds presence without taking any.
#
# Values are in em, converted to Qt's per-letter pixel spacing at use.
_UI = "Segoe UI" if sys.platform == "win32" else "system-ui"
_MONO = "Consolas" if sys.platform == "win32" else "monospace"


# The tracking CURVE. Tracking is a function of size and nothing else, so it is
# derived rather than typed per role - otherwise two roles at the same size can
# drift apart, which is exactly the inconsistency the rule exists to prevent.
# (They had: a 12px heading at -0.006 next to 12px body at 0.)
#
# Anchors, in em, interpolated between and clamped outside:
_TRACK = ((10, 0.018), (11, 0.012), (12, 0.004), (14, -0.004), (17, -0.016),
          (28, -0.024))


def tracking_for(px: float) -> float:
    """Letter-spacing in em for a given pixel size."""
    if px <= _TRACK[0][0]:
        return _TRACK[0][1]
    if px >= _TRACK[-1][0]:
        return _TRACK[-1][1]
    for (x0, y0), (x1, y1) in zip(_TRACK, _TRACK[1:]):
        if x0 <= px <= x1:
            return y0 + (y1 - y0) * (px - x0) / (x1 - x0)
    return 0.0


class Type:
    """A role. Tracking is DERIVED from the size unless the face is monospace.

    Monospace roles take zero: the face has already decided its advance width,
    and changing it breaks the column alignment that is the entire reason for
    using tabular figures in a price ladder.
    """

    __slots__ = ("family", "px", "weight", "tracking")

    def __init__(self, family, px, weight):
        self.family = family
        self.px = px
        self.weight = weight
        self.tracking = 0.0 if family == _MONO else tracking_for(px)


# Chrome - the system face, because it already ships optical sizing, tracking
# tables and legibility tuning that no hand-picked face here would match.
# Hierarchy comes from weight AND size together: weight adds presence without
# taking any more space, which on a terminal is the scarcest thing there is.
TITLE = Type(_UI, 15, QFont.Weight.DemiBold)
HEADING = Type(_UI, 12, QFont.Weight.DemiBold)
BODY = Type(_UI, 12, QFont.Weight.Normal)
CONTROL = Type(_UI, 12, QFont.Weight.DemiBold)
LABEL = Type(_UI, 11, QFont.Weight.DemiBold)
CAPTION = Type(_UI, 10, QFont.Weight.Normal)

# Data - monospace, compared vertically, so alignment beats refinement.
DATA = Type(_MONO, 12, QFont.Weight.Normal)
DATA_SM = Type(_MONO, 11, QFont.Weight.Normal)
DATA_STRONG = Type(_MONO, 12, QFont.Weight.DemiBold)

_font_cache: dict[tuple, QFont] = {}


def font(spec: Type) -> QFont:
    """A QFont with the role's tracking applied. Cached - QFont construction
    shows up in profiles when a painter builds one per row."""
    key = (spec.family, spec.px, int(spec.weight), spec.tracking)
    f = _font_cache.get(key)
    if f is not None:
        return f
    f = QFont(spec.family)
    f.setPixelSize(spec.px)
    f.setWeight(spec.weight)
    if spec.tracking:
        # Qt takes PercentageSpacing as a percentage of the character width,
        # where 100 means "unchanged". em tracking maps to px, and px to a
        # percentage of the size.
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing,
                           100.0 + spec.tracking * 100.0)
    # Tabular figures wherever the face offers them, so a changing price does
    # not make the column jitter.
    if spec.family == _MONO:
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
    _font_cache[key] = f
    return f


# ----------------------------------------------------------------- elevation
# Depth without translucency. Each level is a measured step in luminance
# against the dark base, so a raised surface reads as raised because it is
# LIGHTER, which is the same signal a blurred material gives - minus the
# per-frame cost this app cannot pay.
#
# Never more than three levels visible at once. Stacked surfaces of similar
# weight stop reading as a stack and start reading as noise, which is the
# opaque equivalent of "never stack light translucent surfaces".
def elevate(hex_color: str, level: int) -> str:
    """Lift a base colour by `level` elevation steps."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    # 6 points per step: enough to separate, small enough that three stacked
    # levels still read as one dark room rather than a grey staircase.
    k = 6 * max(0, level)
    return "#%02X%02X%02X" % (min(255, r + k), min(255, g + k), min(255, b + k))


# -------------------------------------------------------------------- motion
# BEHAVIOUR, NOT ANIMATION. A scripted curve cannot respond to input that
# arrives while it is running; a spring can, because new input only changes the
# target and the motion stays continuous. Everything that moves in this app
# moves on one of these.
#
# Parameterised the way Apple parameterises it - damping ratio and response -
# rather than by duration, because settle time is an OUTCOME of the physics and
# not something to be dialled in separately.
#
#   damping 1.0  critically damped, no overshoot. The default for UI.
#   damping 0.8  slight bounce. ONLY where a gesture carried momentum into it;
#                bounce on something that merely appeared feels wrong.
#   response     seconds to approach the target. Lower is snappier.
SPRING_UI = (1.0, 0.35)          # panels, toasts, anything that just appears
SPRING_MOVE = (1.0, 0.40)        # repositioning an existing element
SPRING_MOMENTUM = (0.8, 0.35)    # only after a drag or flick

# Below this, a spring is at rest. In pixels, so it is expressed in the unit
# the value is actually in.
_REST_EPS = 0.4
_REST_V_EPS = 2.0


def reduced_motion() -> bool:
    """Does the operating system ask for reduced motion?

    Reduced motion means gentler feedback, not NO feedback - the springs below
    collapse to an immediate settle, and callers cross-fade instead of sliding.
    What must not survive is travel across the screen and anything elastic.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            # SPI_GETCLIENTAREAANIMATION. Returns FALSE when the user has
            # turned animations off in Ease of Access.
            enabled = ctypes.c_bool(True)
            ok = ctypes.windll.user32.SystemParametersInfoW(
                0x1042, 0, ctypes.byref(enabled), 0)
            if ok:
                return not enabled.value
        except Exception:
            log.debug("could not read the system animation setting",
                      exc_info=True)
    return False


class Spring:
    """One critically-dampable spring over a scalar.

    INTERRUPTIBLE BY CONSTRUCTION. `target` can be changed at any instant and
    the motion continues from wherever it currently IS, carrying its current
    velocity - so a reversal blends rather than hitting a brick wall. That is
    the property a scripted curve cannot have, and the reason this exists at
    all rather than a QPropertyAnimation.
    """

    __slots__ = ("value", "target", "v", "_zeta", "_omega", "on_change",
                 "on_settle")

    def __init__(self, value: float, damping: float = 1.0,
                 response: float = 0.35):
        self.value = float(value)
        self.target = float(value)
        self.v = 0.0
        self._zeta = float(damping)
        # response is the natural period, so omega follows from it directly.
        self._omega = 2.0 * math.pi / max(1e-3, float(response))
        self.on_change = None
        self.on_settle = None

    def configure(self, damping: float, response: float) -> None:
        self._zeta = float(damping)
        self._omega = 2.0 * math.pi / max(1e-3, float(response))

    def set_target(self, target: float, velocity: float | None = None) -> None:
        """Retarget. Pass `velocity` to hand off a gesture's release speed, so
        the animation continues at the speed the finger was already moving
        rather than starting from a standstill."""
        self.target = float(target)
        if velocity is not None:
            self.v = float(velocity)

    def snap(self, value: float) -> None:
        """Jump there. Used under reduced motion, and to seed an initial state
        without animating in from wherever the widget happened to be."""
        self.value = self.target = float(value)
        self.v = 0.0

    def at_rest(self) -> bool:
        return (abs(self.target - self.value) < _REST_EPS
                and abs(self.v) < _REST_V_EPS)

    def step(self, dt: float) -> bool:
        """Advance by `dt` seconds. Returns True while still moving.

        Sub-stepped at a fixed interval: a semi-implicit integrator goes
        unstable when omega*dt approaches 1, and dt here comes from a timer
        that this application deliberately allows to run late. A frame that
        arrives 200 ms after the last one must not make the spring explode.
        """
        if self.at_rest():
            self.value = self.target
            self.v = 0.0
            return False
        steps = max(1, min(8, int(dt / 0.008) + 1))
        h = dt / steps
        w, z = self._omega, self._zeta
        for _ in range(steps):
            a = -2.0 * z * w * self.v - (w * w) * (self.value - self.target)
            self.v += a * h
            self.value += self.v * h
        if self.at_rest():
            self.value = self.target
            self.v = 0.0
            return False
        return True


class SpringDriver(QObject):
    """One timer for every spring in the process, running only when needed.

    A per-animation timer is how a UI ends up with a dozen of them firing
    forever. This one starts when the first spring becomes active and STOPS
    the moment the last settles, so an idle terminal pays nothing at all - the
    same discipline the frame governor applies to windows.
    """

    INTERVAL_MS = 16

    def __init__(self) -> None:
        super().__init__()
        self._springs: list[Spring] = []
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._last = 0.0

    def add(self, spring: Spring) -> None:
        if spring not in self._springs:
            self._springs.append(spring)
        if not self._timer.isActive():
            import time
            self._last = time.perf_counter()
            self._timer.start()

    def remove(self, spring: Spring) -> None:
        try:
            self._springs.remove(spring)
        except ValueError:
            pass
        if not self._springs:
            self._timer.stop()

    def _tick(self) -> None:
        import time
        now = time.perf_counter()
        dt = min(0.25, max(0.001, now - self._last))
        self._last = now
        self.advance(dt)

    def advance(self, dt: float) -> None:
        """Step every active spring by `dt` seconds.

        Split out from the timer so a test can drive real time rather than
        wall-clock time. Ticking in a tight loop advances the clock by
        microseconds per call, so a test that "ran 400 frames" actually
        simulated 0.4 s and then asserted things had settled - which is a
        property of the harness, not of the springs.
        """
        done = []
        for s in list(self._springs):
            try:
                moving = s.step(dt)
                if s.on_change is not None:
                    s.on_change(s.value)
                if not moving:
                    done.append(s)
            except Exception:
                # A callback that raises must not take the driver - and every
                # other animation in the app - down with it.
                log.exception("spring callback failed; dropping that spring")
                done.append(s)
        for s in done:
            self.remove(s)
            if s.on_settle is not None:
                try:
                    s.on_settle()
                except Exception:
                    log.exception("spring settle callback failed")


_driver: SpringDriver | None = None


def driver() -> SpringDriver:
    """The process-wide spring driver, built on first use so importing this
    module does not require a QApplication."""
    global _driver
    if _driver is None:
        _driver = SpringDriver()
    return _driver


def animate(spring: Spring, target: float, velocity: float | None = None,
            on_change=None, on_settle=None) -> None:
    """Retarget a spring and make sure it is being driven.

    Under reduced motion this settles immediately - the on_change callback
    still fires exactly once, so callers do not need a second code path and
    cannot accidentally leave a widget un-positioned.
    """
    if on_change is not None:
        spring.on_change = on_change
    if on_settle is not None:
        spring.on_settle = on_settle
    if reduced_motion():
        spring.snap(target)
        if spring.on_change is not None:
            spring.on_change(spring.value)
        if spring.on_settle is not None:
            spring.on_settle()
        return
    spring.set_target(target, velocity)
    driver().add(spring)


# ------------------------------------------------------------------ the qss
def qss(t) -> str:
    """The application stylesheet, generated from the tokens above.

    Every number in here is a token. That is the whole point: a padding value
    typed inline is a padding value nobody can justify later, and thirteen of
    them is what this app had.
    """
    s, r = SPACE, RADIUS
    l1 = elevate(t.panel, 1)      # raised chrome - controls at rest
    l2 = elevate(t.panel, 2)      # hover / open
    return f"""
QMainWindow {{ background:{t.bg}; }}
QWidget {{ font-family:"{_UI}"; }}

QToolBar {{ background:{t.panel}; border:none;
            padding:{s.SM}px {s.MD}px; spacing:{s.SM}px; }}
QToolBar::separator {{ background:{t.axis}; width:{s.HAIR}px;
                       height:{s.HAIR}px; margin:{s.XS}px {s.MD}px; }}

QLabel {{ color:{t.text}; font-size:{CONTROL.px}px;
          font-weight:{int(CONTROL.weight)}; }}
QCheckBox {{ color:{t.text}; font-size:{CONTROL.px}px;
             font-weight:{int(CONTROL.weight)};
             padding:0 {s.XS}px; spacing:{s.SM}px; }}

QComboBox {{ background:{l1}; color:{t.text};
             border:{s.HAIR}px solid {t.axis}; border-radius:{r.CHIP}px;
             padding:{s.XS}px {s.MD}px; font-size:{CONTROL.px}px; }}
QComboBox:hover {{ background:{l2}; }}
QComboBox::drop-down {{ border:none; width:{s.XL}px; }}
QComboBox QAbstractItemView {{ background:{l1}; color:{t.text};
             selection-background-color:{l2};
             border:{s.HAIR}px solid {t.axis}; border-radius:{r.PANEL}px;
             padding:{s.XXS}px; outline:none; }}

QPushButton {{ background:{l1}; color:{t.text};
               border:{s.HAIR}px solid {t.axis}; border-radius:{r.CHIP}px;
               padding:{s.XS}px {s.LG}px; font-size:{CONTROL.px}px;
               font-weight:{int(CONTROL.weight)}; }}
QPushButton:hover {{ background:{l2}; }}
/* Pressed state exists so feedback lands on PRESS, not on release. A control
   that only reacts when the button comes back up reads as laggy however fast
   the code behind it is. */
QPushButton:pressed {{ background:{t.axis}; }}
QPushButton:checked {{ background:{t.bull}; color:#FFFFFF;
                       border:{s.HAIR}px solid {t.bull}; }}

QDockWidget {{ color:{t.text}; font-size:{LABEL.px}px;
               font-weight:{int(LABEL.weight)}; }}
QDockWidget::title {{ background:{t.panel}; padding:{s.SM}px {s.MD}px;
                      border-bottom:{s.HAIR}px solid {t.axis}; }}

QTabBar::tab {{ background:{t.panel}; color:{t.text};
                padding:{s.SM}px {s.LG}px; border:none;
                font-size:{LABEL.px}px; }}
QTabBar::tab:selected {{ background:{l1};
                         border-bottom:{s.XXS}px solid {t.bull}; }}

/* Chrome Qt would otherwise draw from the default palette, which is a pale
   blue-grey however dark the rest of the app is. */
QMainWindow::separator {{ background:{t.panel}; width:{s.XXS}px;
                          height:{s.XXS}px; }}
QMainWindow::separator:hover {{ background:{t.axis}; }}
QSplitter {{ background:{t.bg}; }}
QSplitter::handle {{ background:{t.panel}; }}
QSplitter::handle:hover {{ background:{t.axis}; }}
QDockWidget > QWidget {{ background:{t.bg}; }}

QToolTip {{ background:{l2}; color:{t.text};
            border:{s.HAIR}px solid {t.axis}; border-radius:{r.CHIP}px;
            padding:{s.XS}px {s.MD}px; }}

QScrollBar:vertical {{ background:transparent; width:{s.XL}px; margin:0; }}
QScrollBar::handle:vertical {{ background:{t.axis};
                               border-radius:{r.CHIP}px;
                               min-height:{s.XXL}px; }}
QScrollBar::handle:vertical:hover {{ background:{elevate(t.axis, 3)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; width:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
""".strip()
