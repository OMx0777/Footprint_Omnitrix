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
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); sys.exit(1)
print("CLOCK GUARD OK")
