"""The signals dock has to PAINT, with rows in it.

This exists because the panel shipped with a NameError on the one path that
matters. `clock_label` was used and never imported, so the moment a signal was
detected the paint raised - and it raised inside paintEvent, where Qt prints
the traceback and carries on, so the window stayed up and the panel simply
stayed empty. Every check that only built the panel, or only ran its refresh,
passed. Nobody had ever painted it WITH EVENTS.

So the rule here: exercise the panel with real signals present, and treat any
exception during paint as a failure rather than letting Qt swallow it.

It also covers delta divergence, which had a detector, a test, and no caller
anywhere in the application - a feature that existed only in the test suite.
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments, BarSeries, BookmapBuffer
from omnitrix.engine.feed import Feed
from omnitrix.engine.model import Trade, Aggressor, BookSnapshot
from omnitrix.ui.main_window import OmnitrixWindow
from omnitrix.ui.signals_panel import SignalsPanel, KIND_TAG

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


app = QApplication.instance() or QApplication([])
inst = Instruments(default_tick=0.01)
SYM = "NVDA"


class NullFeed(Feed):
    def __init__(self):
        super().__init__()
        self.connected = {"multicast": True}
        self.symbols = [SYM]

    def start(self):
        pass

    def stop(self):
        pass


win = OmnitrixWindow(NullFeed(), inst)
win.resize(1400, 900)
win.active_symbol = SYM
win._panes[0].symbol = SYM

# ---- build flow that really produces signals -------------------------------
ser = win.series[SYM] = BarSeries(SYM, inst)
buf = win.bookmaps[SYM] = BookmapBuffer(SYM, inst)
T = 1_700_000_000_000
step = ser.base_tf_s * 1000


def leg(peak, share, base, b0):
    """One advance and pull-back, with a controlled aggressive-buy share."""
    up = [base + (peak - base) * (k + 1) / 6 for k in range(6)]
    down = [peak - (peak - base) * (k + 1) / 8 for k in range(4)]
    b = b0
    for px in up + down:
        for k in range(20):
            aggr = Aggressor.BUY if (k / 20.0) < share else Aggressor.SELL
            tr = Trade(SYM, round(px, 2), 100, aggr, T + b * step + k * 100)
            ser.add_trade(tr)
            buf.add_trade(tr)
        # Outsized prints so the block detector has something to find - as a
        # BALANCED PAIR, so they cancel in delta. A single 5,000-lot buy is
        # fifty times a normal print here and simply overwhelms the share that
        # is supposed to decide the sign of the leg: both legs came out net
        # positive and there was nothing to diverge. The block detector cares
        # about print SIZE, the divergence detector about the delta, and this
        # is how one fixture serves both without lying to either.
        for aggr in (Aggressor.BUY, Aggressor.SELL):
            tr = Trade(SYM, round(px, 2), 5000, aggr, T + b * step + 2100)
            ser.add_trade(tr)
            buf.add_trade(tr)
        buf.add_book(BookSnapshot(
            SYM, {round(px - i * 0.01, 2): 4000 for i in range(1, 12)},
            {round(px + i * 0.01, 2): 4000 for i in range(1, 12)},
            T + b * step))
        b += 1
    return down[-1], b


base, b = leg(101.0, 0.80, 100.0, 0)
base, b = leg(102.0, 0.20, base, b)          # higher high, weaker delta

panel = SignalsPanel(win)
panel.resize(300, 620)
panel._refresh()

check("the panel found signals to show", bool(panel._events),
      f"{len(panel._events)} events")
kinds = {e["kind"] for e in panel._events}
check("...including a delta divergence, which nothing in the app used to call",
      any(k.endswith("_div") for k in kinds), f"kinds={sorted(kinds)}")
check("every event carries the fields the painter reads",
      all({"kind", "ti", "size", "bucket"} <= set(e) for e in panel._events),
      f"{[sorted(e) for e in panel._events[:1]]}")
check("...and every kind has a tag and does not fall through to '?'",
      all(e["kind"] in KIND_TAG for e in panel._events),
      f"untagged={sorted(kinds - set(KIND_TAG))}")
check("the list is ordered newest first",
      all(panel._events[i]["bucket"] >= panel._events[i + 1]["bucket"]
          for i in range(len(panel._events) - 1)))

# ---- THE CHECK THAT WAS MISSING: paint it, and let nothing be swallowed ----
img = QImage(panel.size(), QImage.Format.Format_ARGB32)
err = []
_real_paint = panel.paintEvent


def guarded(ev):
    try:
        _real_paint(ev)
    except BaseException as e:                                  # noqa: BLE001
        err.append(f"{type(e).__name__}: {e}")


panel.paintEvent = guarded
panel.repaint()
p = QPainter(img)
p.end()
panel.render(img)
check("painting the panel WITH EVENTS raises nothing - this is the check that "
      "a NameError in paintEvent slipped past, because Qt prints and continues",
      not err, f"{err[:2]}")

# and the empty case must still be safe
empty = SignalsPanel(win)
empty.resize(300, 200)
empty._events = []
err2 = []
_rp2 = empty.paintEvent


def guarded2(ev):
    try:
        _rp2(ev)
    except BaseException as e:                                  # noqa: BLE001
        err2.append(f"{type(e).__name__}: {e}")


empty.paintEvent = guarded2
img2 = QImage(empty.size(), QImage.Format.Format_ARGB32)
empty.render(img2)
check("...and painting it empty is safe too", not err2, f"{err2[:2]}")

# ---- the refresh must stay bounded as the session ages ---------------------
# The divergence detector reads bars, and bars grow all session. If the panel
# passed the whole series it would get slower every hour - the exact failure
# this codebase keeps removing.
import time as _t
from omnitrix.ui.signals_panel import DIV_LOOKBACK

n_before = len(ser.bars)
b2 = b
for extra in range(1500):
    for k in range(4):
        ser.add_trade(Trade(SYM, round(100.0 + (extra % 40) * 0.01, 2), 100,
                            Aggressor.BUY if k & 1 else Aggressor.SELL,
                            T + b2 * step + k * 100))
    b2 += 1
a = _t.perf_counter()
for _ in range(5):
    panel._refresh()
grown = (_t.perf_counter() - a) / 5 * 1000
check("the refresh is bounded by a lookback, not by session length",
      DIV_LOOKBACK <= 400 and grown < 60,
      f"{n_before} -> {len(ser.bars)} bars, refresh {grown:.1f} ms, "
      f"lookback {DIV_LOOKBACK}")

# a symbol with no series at all must not raise
win.active_symbol = "GHOST"
err3 = None
try:
    panel._refresh()
except Exception as e:                                          # noqa: BLE001
    err3 = f"{type(e).__name__}: {e}"
check("a symbol with no data refreshes without raising", err3 is None, err3 or "")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("SIGNALS PANEL OK")
