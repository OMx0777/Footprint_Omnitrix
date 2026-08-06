"""Every epoch->text conversion on a paint path must survive a viewport that
extends past the data. This is the FOURTH place this bug has surfaced, so the
check is now exhaustive rather than per-site."""
import os, sys, math, logging
sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PyQt6.QtWidgets import QApplication
import pyqtgraph as pg
from omnitrix.render.crosshair import clock_label, safe_localtime
logging.basicConfig(level=logging.CRITICAL)
FAILS=[]
def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ")+n+(f"   {d}" if d else ""))
    if not ok: FAILS.append(n)

app=QApplication.instance() or QApplication([])

# The exact values a panned/zoomed viewport produces.
HOSTILE = [-1, -1e12, -2**40, 0, 1e18, 2**53, float("inf"), float("-inf"),
           float("nan"), 3.2e10, -0.0001, 1e300]
ok=True; bad=None
for v in HOSTILE:
    try:
        r = clock_label(v)
        assert isinstance(r, str)
    except Exception as e:
        ok=False; bad=(v,e); break
check("clock_label survives every hostile viewport value", ok, str(bad))
ok=True; bad=None
for v in HOSTILE:
    try:
        safe_localtime(v)
    except Exception as e:
        ok=False; bad=(v,e); break
check("safe_localtime survives them too", ok, str(bad))
check("a real timestamp still formats", clock_label(1_700_000_000) != "")

# Every axis in the app must tolerate them through its real tickStrings.
from omnitrix.ui.tape_window import _ClockAxis
from omnitrix.ui.bookmap_window import TimeAxisSecs
from omnitrix.render.axis import TimeAxis
axes = [("tape _ClockAxis", _ClockAxis(orientation="bottom")),
        ("footprint TimeAxis", TimeAxis(orientation="bottom"))]
try:
    axes.append(("bookmap TimeAxisSecs", TimeAxisSecs(orientation="bottom", win=None)))
except Exception:
    pass
bad=[]
for name, ax in axes:
    try:
        ax.tickStrings(HOSTILE, 1.0, 1.0)
    except Exception as e:
        bad.append((name, type(e).__name__, str(e)))
check("every time axis tolerates a viewport past the data", not bad, str(bad))

# and no unguarded localtime is left on a paint path
import subprocess, pathlib
root = pathlib.Path(r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix\omnitrix")
offenders=[]
for f in root.rglob("*.py"):
    if "__pycache__" in str(f): continue
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if "time.localtime(" in line and "def safe_localtime" not in line \
           and "crosshair.py" not in f.name and not line.strip().startswith("#") \
           and "time.localtime()" not in line:
            offenders.append(f"{f.relative_to(root)}:{i}")
check("no unguarded time.localtime() left outside the helper",
      not offenders, str(offenders))

# ---- no test may write the operator's real workspace ----------------------
# This has now cost two separate incidents. OmnitrixWindow.__init__ RESTORES
# ~/.omnitrix_workspace.json and closeEvent SAVES it, so any test that builds
# one is reading the operator's live layout, and any test that closes one
# overwrites it with four throwaway windows. tests/link_status.py did exactly
# that, silently, every run.
#
# It is checked by scanning rather than by fixing it once, because the trap is
# invisible at the call site: `win.close()` looks like tidy-up.
#
# The ordering rule is against the CONSTRUCTION, not the import. main_window
# does `from . import workspace` and then `workspace.save(self)`, so the
# attribute is looked up when it is called - replacing it any time before a
# window exists is enough. (Reassigning workspace.PATH instead would NOT be:
# `def save(win, path=PATH)` binds that default at import time.)
tests_dir = pathlib.Path(__file__).resolve().parent
bad = []
for f in sorted(tests_dir.glob("*.py")):
    # This file is the scanner: it contains the search literal itself and
    # would otherwise report itself as a window it never builds.
    if f.name.startswith("_") or f.name == pathlib.Path(__file__).name:
        continue
    src = f.read_text(encoding="utf-8")
    if "OmnitrixWindow(" not in src:
        continue
    lines = src.splitlines()
    save_at = next((i for i, l in enumerate(lines)
                    if "workspace.save" in l and "lambda" in l), None)
    built_at = next((i for i, l in enumerate(lines)
                     if "OmnitrixWindow(" in l and not l.lstrip().startswith("#")
                     and "import" not in l), None)
    if save_at is None:
        bad.append(f"{f.name}: builds a window without neutering workspace.save")
    elif built_at is not None and save_at > built_at:
        bad.append(f"{f.name}: neuters workspace.save only AFTER building a window")
check("no test can write the operator's real workspace", not bad,
      "; ".join(bad) if bad else f"{len(list(tests_dir.glob('*.py')))} test files scanned")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("CLOCK GUARD OK")
