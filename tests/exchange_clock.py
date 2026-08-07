"""Every clock in the app reads the EXCHANGE, not the desk.

A trader works in market time - the open, the close, 09:30 and 16:00, the
session high. Measured on the live feed the desk is 9.5 hours ahead of the
exchange, so a print at 07:17 ET was drawn at 16:47 and every glance at the
chart needed a subtraction in the head.

THE THING THIS MUST NOT DO is shift a stored timestamp. The sequence seam, gap
repair, alert crossings, bar buckets and the recording all depend on one
unambiguous absolute clock; a second clock that disagrees is the failure this
codebase keeps removing. So only the conversion TO TEXT moves, and these
checks are mostly about proving the rest did not.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.render import crosshair
from omnitrix.render.crosshair import (clock_label, safe_localtime,
                                       set_display_offset, display_offset_ms)
from omnitrix.engine import Instruments, BarSeries
from omnitrix.engine.model import Trade, Aggressor

FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


HALF_H = 9.5 * 3600 * 1000          # the measured ET/IST gap
T = 1_786_101_426.544               # a real epoch from the live recording

# ---- 1. no offset means no change -----------------------------------------
set_display_offset(0)
local_txt = clock_label(T)
check("with no offset the clock is unchanged - an app with no feed does not "
      "guess a timezone", local_txt == time.strftime("%H:%M:%S", time.localtime(T)),
      f"{local_txt}")

# ---- 2. the offset shifts the LABEL by exactly that much -------------------
set_display_offset(HALF_H)
ex_txt = clock_label(T)
check("the offset is published and read back", display_offset_ms() == HALF_H,
      f"{display_offset_ms()} ms")
shifted = time.strftime("%H:%M:%S", time.localtime(T - HALF_H / 1000))
check("the label moves by exactly the offset", ex_txt == shifted,
      f"desk {local_txt} -> exchange {ex_txt}")
def _secs(txt):
    h, m, sec = (int(x) for x in txt.split(":"))
    return h * 3600 + m * 60 + sec


delta = (_secs(local_txt) - _secs(ex_txt)) % 86400
check("...and that is 9.5 hours earlier - the measured ET/IST gap",
      abs(delta - 9.5 * 3600) < 2, f"{delta/3600:.2f} h")

# ---- 3. safe_localtime moves with it, so date labels agree -----------------
st = safe_localtime(T)
check("safe_localtime uses the same clock as clock_label",
      st is not None and f"{st.tm_hour:02d}:{st.tm_min:02d}:{st.tm_sec:02d}" == ex_txt,
      f"{st.tm_hour:02d}:{st.tm_min:02d} vs {ex_txt}")

# ---- 4. THE POINT: stored time is untouched --------------------------------
inst = Instruments(default_tick=0.01)
ser = BarSeries("EX", inst)
ts = 1_786_101_426_544
for i in range(50):
    ser.add_trade(Trade("EX", 220.0 + i * 0.01, 100, Aggressor.BUY, ts + i * 200))
buckets = [b.start_ts for b in ser.bars]

set_display_offset(0)
ser2 = BarSeries("EX", inst)
for i in range(50):
    ser2.add_trade(Trade("EX", 220.0 + i * 0.01, 100, Aggressor.BUY, ts + i * 200))
check("bar buckets do NOT move with the display clock - they are absolute",
      [b.start_ts for b in ser2.bars] == buckets, f"{buckets[:2]}")
check("...and so does the session volume",
      ser2.sess_volume == ser.sess_volume)

# alerts fire on price, and their timestamps stay absolute
from omnitrix.engine.alerts import AlertBook
set_display_offset(HALF_H)
bk = AlertBook()
a = bk.add("EX", 220.0)
bk.check("EX", 219.0)
hit = bk.check("EX", 221.0)
check("an alert's fired_ts is wall-clock absolute, not shifted",
      bool(hit) and abs(hit[0].fired_ts - time.time()) < 5,
      f"{hit[0].fired_ts if hit else None} vs now {time.time():.0f}")

# ---- 5. the guards still hold under the shift ------------------------------
HOSTILE = [-1, 0, float("inf"), float("-inf"), float("nan"), 1e18, -1e12]
bad = [v for v in HOSTILE if clock_label(v) != ""]
check("hostile values still return empty, not an exception", not bad, f"{bad}")
check("...and safe_localtime still returns None for them",
      all(safe_localtime(v) is None for v in HOSTILE))
# a value just inside the range must survive the shift without going negative
set_display_offset(HALF_H)
check("a timestamp smaller than the offset does not become negative",
      isinstance(clock_label(60.0), str), f"{clock_label(60.0)!r}")

set_display_offset(0)
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("EXCHANGE CLOCK OK")
