"""A bad paint must not kill the terminal.

THE MEASUREMENT THAT MOTIVATES THIS. Under PyQt6 an unhandled Python exception
inside a virtual override does not propagate - Qt calls qFatal() and the
process dies. Measured on this machine: exit code 127, no traceback on stderr,
nothing in the log, nothing in the faulthandler file. The window is simply
gone.

That is not hypothetical. The signals dock used clock_label without importing
it, so the first time a block print was detected the next paint raised
NameError and took the whole application down - which is what "it crashed in
ten minutes" looks like from the inside.

So this checks three separate things:

  1. the fatality is REAL, by running an undecorated paint in a subprocess and
     confirming the process dies. Without this the other two prove nothing -
     a guard against a danger that does not exist is just noise;
  2. the SAME paint, decorated, leaves the process alive;
  3. nothing in the shipped app is quietly relying on the guard. The counter
     must be zero after exercising every widget, or a real bug is being
     swallowed - which is the failure mode a broad try/except invites.
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


# ---- 1 & 2. the fatality, and the guard, in real subprocesses --------------
SRC = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    from PyQt6.QtWidgets import QApplication, QWidget
    from PyQt6.QtGui import QImage
    {imp}
    app = QApplication([])

    class W(QWidget):
        {dec}
        def paintEvent(self, ev):
            raise NameError("name 'clock_label' is not defined")

    w = W(); w.resize(60, 40)
    w.render(QImage(w.size(), QImage.Format.Format_ARGB32))
    print("ALIVE")
""")


def run(dec: bool):
    src = SRC.format(
        root=ROOT,
        imp="from omnitrix.paintguard import safe_paint" if dec else "",
        dec="@safe_paint" if dec else "")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, env=env, timeout=120)
    return p.returncode, (p.stdout or "")


rc_raw, out_raw = run(dec=False)
check("an UNGUARDED paint exception really does kill the process - this is "
      "the danger the guard exists for, not a hypothetical",
      rc_raw != 0 and "ALIVE" not in out_raw,
      f"exit {rc_raw}, stdout {out_raw.strip()!r}")

rc_ok, out_ok = run(dec=True)
check("the SAME exception under @safe_paint leaves the process alive",
      rc_ok == 0 and "ALIVE" in out_ok,
      f"exit {rc_ok}, stdout {out_ok.strip()!r}")

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
