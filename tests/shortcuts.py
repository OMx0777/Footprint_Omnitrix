"""The keyboard has to do what a trader's hands already expect.

TradingView's bindings, because those are the ones already in muscle memory.
The trap is not that a shortcut fails - it is that it does something ELSE:

  * bare letters open the ticker search, so any tool bound to a plain letter
    would make typing a symbol impossible. Every tool key is therefore on Alt,
    and this checks that typing a letter still reaches the search;

  * a key that pans must also turn auto-scroll OFF, or the next frame snaps
    the view back to the live edge and the chart appears not to have moved -
    which is exactly how the history test wasted an hour.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow, TF_CHOICES

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])
feed = SyntheticFeed(symbols=["NVDA", "QQQ"], start_price=220.0, tick=0.01,
                     trades_per_sec=150, prefill_minutes=5, seed=11)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1400, 880)
win.show()
win.start_feed()
for _ in range(140):
    app.processEvents()
    win._tick()
    time.sleep(0.005)


def key(k, mod=Qt.KeyboardModifier.NoModifier, text=None):
    """A key event WITH ITS TEXT, as Qt really delivers one.

    The ticker search opens on ev.text() being a letter, so an event built
    without text never reaches it - the three-argument QKeyEvent leaves text
    empty and the check silently tests nothing.
    """
    if text is None:
        text = ""
        if Qt.Key.Key_A <= k <= Qt.Key.Key_Z and not (
                mod & (Qt.KeyboardModifier.AltModifier
                       | Qt.KeyboardModifier.ControlModifier)):
            text = chr(k).lower()
        elif Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
            text = chr(k)
    win.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, k, mod, text))
    app.processEvents()
    win._tick()


ALT = Qt.KeyboardModifier.AltModifier
CTRL = Qt.KeyboardModifier.ControlModifier

# ---- 1. digits select a timeframe ------------------------------------------
bad = []
for k, want in win.TF_KEYS.items():
    if win.tf_combo.findText(want) < 0:
        bad.append((want, "not offered"))
        continue
    key(k)
    if win.tf_combo.currentText() != want:
        bad.append((want, win.tf_combo.currentText()))
check("every digit selects its timeframe", not bad, f"{bad}")
check("...and each one is a real timeframe",
      all(v in TF_CHOICES for v in win.TF_KEYS.values()))

# ---- 2. Alt+letter arms a tool, and the same key disarms it ----------------
bad = []
for k, tool in win.TOOL_KEYS.items():
    if tool not in win._tool_buttons:
        continue
    key(k, ALT)
    if win.active_drawing_tool != tool:
        bad.append((tool, win.active_drawing_tool))
    key(k, ALT)
    if win.active_drawing_tool is not None:
        bad.append((tool, "did not disarm"))
check("Alt+letter arms a drawing tool and toggles it off", not bad, f"{bad}")

# ---- 3. THE ONE THAT WOULD HURT: letters must still reach the search -------
win.sym_search.hide()
key(Qt.Key.Key_N)
check("a bare letter still opens the ticker search - tools are on Alt so "
      "typing a symbol is never captured",
      win.sym_search.isVisible(), f"visible={win.sym_search.isVisible()}")
win.sym_search.hide()
app.processEvents()
# ...and while it is open, a digit must type rather than switch timeframe
win._open_sym_search("NV")
tf_before = win.tf_combo.currentText()
key(Qt.Key.Key_5)
check("...and a digit typed into the search does NOT change the timeframe",
      win.tf_combo.currentText() == tf_before,
      f"{tf_before} -> {win.tf_combo.currentText()}")
win.sym_search.hide()
app.processEvents()

# ---- 4. navigation, and auto-scroll released with it -----------------------
pane = win._active_pane
pane.auto_scroll = True
vb = pane.price_plot.getViewBox()
left0 = vb.viewRect().left()
key(Qt.Key.Key_Left)
check("Left pans the chart back", vb.viewRect().left() < left0,
      f"{left0:.1f} -> {vb.viewRect().left():.1f}")
check("...and releases auto-scroll, or the next frame would snap it back",
      pane.auto_scroll is False)
w0 = vb.viewRect().width()
key(Qt.Key.Key_Plus)
check("+ zooms in", vb.viewRect().width() < w0,
      f"{w0:.1f} -> {vb.viewRect().width():.1f}")
w1 = vb.viewRect().width()
key(Qt.Key.Key_Minus)
check("- zooms out", vb.viewRect().width() > w1)
key(Qt.Key.Key_End)
check("End returns to the live edge and resumes following",
      pane.auto_scroll is True)

# ---- 5. panels ------------------------------------------------------------
cvd0 = win.chk_cvd.isChecked()
key(Qt.Key.Key_D, ALT)
check("Alt+D toggles the CVD pane", win.chk_cvd.isChecked() != cvd0)
lay0 = win.layout_combo.currentText()
key(Qt.Key.Key_G, ALT)
check("Alt+G steps the chart grid", win.layout_combo.currentText() != lay0,
      f"{lay0} -> {win.layout_combo.currentText()}")

# ---- 6. the ones that already existed must still work ----------------------
n0 = len(win.alerts.all())
win._last_cursor = (10.0, 220.5)
win.active_symbol = "NVDA"
key(Qt.Key.Key_A, ALT)
check("Alt+A still arms a price alert", len(win.alerts.all()) == n0 + 1,
      f"{n0} -> {len(win.alerts.all())}")
key(Qt.Key.Key_Escape)
check("Escape still unwinds cleanly", win.active_drawing_tool is None)

feed.stop()
win.close()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("SHORTCUTS OK")
