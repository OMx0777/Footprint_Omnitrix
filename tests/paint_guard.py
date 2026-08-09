"""Two independent defences against a bad paint, each checked for what it does.

Under PyQt6 an unhandled Python exception in code Qt calls from C++ reaches
qFatal() and aborts the process - exit 0xC0000409, no traceback anywhere - but
ONLY while sys.excepthook is the default one. So:

  * app._install_excepthook is what keeps the process ALIVE, and it covers
    every Qt callback, not just paint. That is the load-bearing defence and
    the first checks below are that it is installed and that it works;

  * @safe_paint is what keeps a repeating fault CONTAINED. The excepthook
    survives the fault but formats and writes the whole traceback every frame,
    on the GUI thread, forever - so one bug becomes a permanent log stream.
    The guard reports a site once per QUIET_S and blanks the widget after.

An earlier version of this file asserted the guard was what stopped the app
dying, which was wrong: the subprocess it tested had no excepthook installed,
so it measured raw PyQt6 rather than this application. The check below now
runs BOTH configurations so the difference is explicit and cannot be misread
again.
"""

import os
import subprocess
import sys
import textwrap

ROOT = __file__.rsplit("tests", 1)[0]
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


# ---- 1. what actually keeps the process alive ------------------------------
SRC = textwrap.dedent("""
    import sys, logging
    sys.path.insert(0, {root!r})
    logging.basicConfig(level=logging.CRITICAL)
    {hook}
    from PyQt6.QtWidgets import QApplication, QWidget
    from PyQt6.QtGui import QImage
    {imp}
    app = QApplication([])

    class W(QWidget):
        {dec}
        def paintEvent(self, ev):
            raise NameError("name 'clock_label' is not defined")

    w = W(); w.resize(60, 40)
    img = QImage(w.size(), QImage.Format.Format_ARGB32)
    for _ in range(5):
        w.render(img)
    print("ALIVE")
""")

HOOK = ("from omnitrix.app import _install_excepthook\n"
        "_install_excepthook()")


def run(hook: bool, dec: bool):
    src = SRC.format(
        root=ROOT,
        hook=HOOK if hook else "",
        imp="from omnitrix.paintguard import safe_paint" if dec else "",
        dec="@safe_paint" if dec else "")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, env=env, timeout=180)
    return p.returncode, (p.stdout or "")


rc, out = run(hook=False, dec=False)
check("with the DEFAULT excepthook a paint exception aborts the process - "
      "this is the raw PyQt6 behaviour the app has to defend against",
      rc != 0 and "ALIVE" not in out, f"exit {rc}, stdout {out.strip()!r}")

rc, out = run(hook=True, dec=False)
check("app._install_excepthook alone is what keeps it alive - the guard is "
      "NOT load-bearing for survival, and saying so was the earlier mistake",
      rc == 0 and "ALIVE" in out, f"exit {rc}, stdout {out.strip()!r}")

rc, out = run(hook=False, dec=True)
check("@safe_paint alone also survives, since the exception never reaches Qt",
      rc == 0 and "ALIVE" in out, f"exit {rc}, stdout {out.strip()!r}")

rc, out = run(hook=True, dec=True)
check("...and the shipped combination of both survives",
      rc == 0 and "ALIVE" in out, f"exit {rc}, stdout {out.strip()!r}")

# the app must really install it - this is the defence for every OTHER Qt
# callback (slots, resize, mouse), which no decorator covers
import inspect
from omnitrix import app as omni_app
src_main = inspect.getsource(omni_app.main)
check("main() installs the excepthook BEFORE QApplication is constructed",
      "_install_excepthook()" in src_main
      and src_main.index("_install_excepthook()")
      < src_main.index("QApplication("),
      "order matters: a slot can fire during construction")

# ---- the fault is counted, not silently discarded --------------------------
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QWidget
from omnitrix.paintguard import (safe_paint, PAINT_FAULTS, paint_fault_count,
                                 reset_paint_faults)

app = QApplication.instance() or QApplication([])
reset_paint_faults()


class Bad(QWidget):
    @safe_paint
    def paintEvent(self, ev):
        raise ValueError("boom")


b = Bad()
b.resize(40, 30)
for _ in range(3):
    b.render(QImage(b.size(), QImage.Format.Format_ARGB32))
check("a swallowed fault is COUNTED, so it cannot hide from the tests",
      paint_fault_count() == 3, f"{paint_fault_count()} faults {dict(PAINT_FAULTS)}")
reset_paint_faults()

# ---- 2. WHAT THE GUARD IS FOR: a repeating fault stays quiet ---------------
# The excepthook survives, but it formats and writes the whole traceback every
# frame, on the GUI thread. A paint that fails once fails forever, so that is a
# permanent log stream, not a one-off. This is the property worth having.
import logging


class Count(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.n = 0

    def emit(self, rec):
        self.n += 1


cap = Count()
pg_log = logging.getLogger("omnitrix.paintguard")
pg_log.addHandler(cap)
pg_log.setLevel(logging.ERROR)
reset_paint_faults()
b2 = Bad()
b2.resize(200, 150)
img2 = QImage(b2.size(), QImage.Format.Format_ARGB32)
for _ in range(200):
    b2.render(img2)
check("200 consecutive failing paints produce ONE log line, not 200 - a "
      "backstop that re-reports every frame turns one bug into a log flood",
      cap.n == 1, f"{cap.n} log lines for {paint_fault_count()} faults")
check("...and every one of them is still counted",
      paint_fault_count() == 200, f"{paint_fault_count()}")
pg_log.removeHandler(cap)
reset_paint_faults()

# ---- 3. every paint site in the app is guarded -----------------------------
import ast
import pathlib

unguarded = []
for p in (sorted(pathlib.Path(ROOT, "omnitrix", "render").rglob("*.py"))
          + sorted(pathlib.Path(ROOT, "omnitrix", "ui").rglob("*.py"))):
    tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in ("paint", "paintEvent"):
            continue
        names = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        if "safe_paint" not in names:
            unguarded.append(f"{p.name}:{node.lineno} {node.name}")
check("EVERY paint override in the app is guarded - a new one added without "
      "the decorator is a new way for the terminal to vanish",
      not unguarded, f"{len(unguarded)} unguarded: {unguarded[:4]}")

# ---- 4. and nothing in the real app is relying on it ------------------------
from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.ui.main_window import OmnitrixWindow

reset_paint_faults()
feed = SyntheticFeed(symbols=["NVDA", "QQQ"], start_price=220.0, tick=0.01,
                     trades_per_sec=200, prefill_minutes=3, seed=5)
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1400, 900)
win.show()
win.start_feed()
import time as _t
for _ in range(120):
    app.processEvents()
    win._tick()
    _t.sleep(0.004)

# every chart mode, plus the bookmap, so the render items really paint
win.active_symbol = "NVDA"
win._open_bookmap()
for mode in ("Footprint", "Cluster", "Profile", "Delta", "Heatmap"):
    win.mode_combo.setCurrentText(mode)
    for _ in range(25):
        app.processEvents()
        win._tick()
        _t.sleep(0.004)

check("running the real app through every chart mode produces NO paint fault "
      "- the guard is a safety net, not a crutch the app is hanging from",
      paint_fault_count() == 0, f"{dict(PAINT_FAULTS)}")

feed.stop()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("PAINT GUARD OK")
