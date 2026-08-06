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

log = logging.getLogger(__name__)

# Minimum gap between sounds, whatever the alert rate.
SOUND_COOLDOWN_S = 1.5
# How long a toast stays up before it fades.
TOAST_SECONDS = 12.0


class _Sounder(QObject):
    """Beeps off the GUI thread, and refuses to queue them up."""

    def __init__(self) -> None:
        super().__init__()
        self._last = 0.0
        self._lock = threading.Lock()

    def play(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last < SOUND_COOLDOWN_S:
                return
            self._last = now
        threading.Thread(target=self._beep, daemon=True).start()

    @staticmethod
    def _beep() -> None:
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()
        self._rows: list[tuple[str, float, float]] = []   # sym, price, ts
        self._font = QFont("Segoe UI", 11, QFont.Weight.Bold)
        self._small = QFont("Segoe UI", 9)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self.setFixedWidth(330)

    def show_alerts(self, alerts) -> None:
        now = time.time()
        for a in alerts:
            self._rows.append((a.symbol, a.fired_price or a.price, now))
        # Keep the most recent handful; a toast is a notification, not a log.
        self._rows = self._rows[-6:]
        self.setFixedHeight(34 + 26 * len(self._rows))
        self._reposition()
        self.show()
        self.raise_()
        self.update()
        self._timer.start(int(TOAST_SECONDS * 1000))

    def _expire(self) -> None:
        self._rows.clear()
        self.hide()
        self.dismissed.emit()

    def _reposition(self) -> None:
        p = self.parentWidget()
        if p is not None:
            self.move(max(0, p.width() - self.width() - 24), 64)

    def mousePressEvent(self, ev) -> None:
        self._expire()

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
