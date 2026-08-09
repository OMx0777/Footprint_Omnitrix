"""Alert sound and the on-screen notification.

TWO RULES, both learned from alerts being ignorable:

  * the beep must NEVER run on the GUI thread. winsound.Beep blocks for its
    full duration, so a 400 ms beep is 400 ms of frozen chart - five dropped
    frames to tell you about one price. It runs on a worker.

  * a burst must not become a siren. If four levels trigger in the same second
    the sound plays once and the toast lists all four. An alert you learn to
    mute is worse than no alert.
"""

from __future__ import annotations

import logging
import threading
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QPainter, QFont
from PyQt6.QtWidgets import QWidget

from ..render.crosshair import clock_label
from ..paintguard import safe_paint
from . import design
from .design import SPACE

log = logging.getLogger(__name__)

# Minimum gap between sounds, whatever the alert rate.
SOUND_COOLDOWN_S = 1.5
# How long a toast stays up before it fades.
TOAST_SECONDS = 12.0


class _Sounder(QObject):
    """Beeps off the GUI thread, and refuses to queue them up.

    THE FALLBACK GOES BACK THROUGH A SIGNAL. QApplication.beep() is a GUI
    call, and Qt's rule is that GUI classes are touched only from the thread
    that owns them. The worker therefore emits, and Qt delivers the beep to
    the GUI thread through the event loop.

    Being straight about what this does and does not fix: it was proposed as
    the cause of a freeze, and that does not hold up - called directly from a
    worker on the real windows platform, with winsound forced to fail so the
    fallback is the path taken, QApplication.beep() returns and the event loop
    keeps running (60 timer ticks in 600 ms). It is still wrong to call it
    there. Undefined behaviour that happens to work on one Qt build and one
    audio stack is not a guarantee, and routing it correctly costs nothing.

    The freeze that DID happen when an alert was armed was AlertBook.__bool__
    - see the measurement there.
    """

    _fallback = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._last = 0.0
        self._lock = threading.Lock()
        # AutoConnection: emitted from the worker it queues to this object's
        # thread, which is the one that imported the module - the GUI thread.
        self._fallback.connect(self._system_beep)

    def play(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last < SOUND_COOLDOWN_S:
                return
            self._last = now
        threading.Thread(target=self._beep, daemon=True).start()

    def _beep(self) -> None:
        try:
            import winsound
            # Two short rising tones. Distinct from every Windows system
            # sound, which is the point - it has to be recognisable across a
            # room without looking at the screen.
            winsound.Beep(880, 140)
            winsound.Beep(1320, 180)
        except Exception:
            # No winsound, no audio device, or a locked-down session. An alert
            # that cannot beep must still show its toast rather than raise.
            self._fallback.emit()

    @staticmethod
    def _system_beep() -> None:
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.beep()
        except Exception:
            log.debug("no audible alert available", exc_info=True)


SOUNDER = _Sounder()


class AlertToast(QWidget):
    """The banner that says which symbol hit which price.

    A child of the main window rather than a separate window: a popup that can
    end up behind the terminal is an alert you do not see, and one that steals
    focus while you are typing a ticker is worse than that.
    """

    dismissed = pyqtSignal()

    # How far off the right edge the toast sits when hidden. It enters from
    # there and leaves back to there - see _slide_to.
    OFFSCREEN_PAD = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()
        self._rows: list[tuple[str, float, float]] = []   # sym, price, ts
        self._font = design.font(design.HEADING)
        self._small = design.font(design.DATA_SM)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self.setFixedWidth(330)
        # ONE SPRING OVER x. The toast lives at the right edge, so the only
        # axis it travels on is x - and a spring per axis is the rule anyway: a
        # single 2D spring desyncs the moment the two axes carry different
        # velocities.
        self._x = design.Spring(0.0, *design.SPRING_UI)
        self._x.on_change = self._on_x
        self._leaving = False
        # OWN STATE, not Qt's. isVisible() is False whenever an ANCESTOR is
        # hidden, so using it as "am I already on screen" made show_alerts
        # re-snap to the hidden position on every alert - which teleported the
        # toast instead of retargeting it, defeating the whole point of a
        # spring. A widget's animation state has to be the widget's own.
        self._on_screen = False

    # ---- motion ---------------------------------------------------------
    def _rest_x(self) -> float:
        p = self.parentWidget()
        w = p.width() if p is not None else self.width()
        return max(0.0, w - self.width() - SPACE.XXL)

    def _hidden_x(self) -> float:
        p = self.parentWidget()
        return float(p.width() + self.OFFSCREEN_PAD) if p is not None else 0.0

    def _on_x(self, x: float) -> None:
        self.move(int(round(x)), SPACE.XXL * 2 + SPACE.XL)

    def _slide_to(self, x: float, leaving: bool) -> None:
        """Move along the ONE path this toast owns.

        It enters from beyond the right edge and it leaves back out the same
        way. Entering from the right and dismissing downward would read as two
        unrelated objects; a thing that returns the way it came is a thing the
        eye can keep track of.
        """
        self._leaving = leaving
        design.animate(self._x, x,
                       on_settle=self._after_leave if leaving else None)

    def _after_leave(self) -> None:
        if self._leaving:
            self.hide()
            self._on_screen = False
            self._rows.clear()
            self.dismissed.emit()

    def show_alerts(self, alerts) -> None:
        now = time.time()
        for a in alerts:
            self._rows.append((a.symbol, a.fired_price or a.price, now))
        # Keep the most recent handful; a toast is a notification, not a log.
        self._rows = self._rows[-6:]
        self.setFixedHeight(34 + 26 * len(self._rows))
        if not self._on_screen:
            # Seed OFF screen so the first frame does not flash at the rest
            # position before the spring has moved anything.
            self._x.snap(self._hidden_x())
            self._on_x(self._x.value)
            self.show()
            self._on_screen = True
        self.raise_()
        # INTERRUPTIBLE: a second alert arriving while the first is sliding out
        # simply retargets the same spring from wherever it currently is,
        # carrying its velocity. It does not restart, and it does not jump.
        self._slide_to(self._rest_x(), leaving=False)
        self.update()
        self._timer.start(int(TOAST_SECONDS * 1000))

    def _expire(self) -> None:
        self._slide_to(self._hidden_x(), leaving=True)

    def _reposition(self) -> None:
        """Re-anchor after the parent resizes, without animating."""
        if self._on_screen and not self._leaving:
            self._x.snap(self._rest_x())
            self._on_x(self._x.value)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._reposition()

    def mousePressEvent(self, ev) -> None:
        # Dismiss on PRESS, not release. Feedback that waits for the button to
        # come back up reads as lag however fast the code behind it is.
        self._expire()

    @safe_paint
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 26, 36, 242))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        p.setBrush(QColor(255, 179, 0))
        p.drawRoundedRect(0, 0, 4, self.height(), 2, 2)

        p.setFont(self._font)
        p.setPen(QColor(255, 196, 60))
        p.drawText(16, 24, "PRICE ALERT")
        p.setFont(self._small)
        y = 46
        for sym, price, ts in self._rows:
            p.setPen(QColor(232, 236, 244))
            p.drawText(16, y, f"{sym}")
            p.setPen(QColor(120, 210, 180))
            p.drawText(96, y, f"{price:,.2f}")
            p.setPen(QColor(130, 140, 155))
            p.drawText(180, y, clock_label(ts))
            y += 26
        p.setPen(QColor(110, 118, 132))
        p.drawText(16, self.height() - 8, "click to dismiss")
