"""The packaged terminal must actually RUN on multicast and paint data.

Wiring a feed into argparse is not the same as the app using it, so this
publishes a real L1 stream to a group and checks that bars appear on the chart.
"""
import os
import sys
import socket
import threading
import time
import logging

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")

# The window RESTORES ~/.omnitrix_workspace.json on construction and SAVES it
# on close, so without this the test both reads the operator's live desk (which
# makes its preconditions depend on whatever they last had open) and can
# overwrite it. Enforced by tests/clock_guard.py.
from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtWidgets import QApplication

from omnitrix.engine import wire, Instruments
from omnitrix.engine.multicast_feed import MulticastFeed
from omnitrix.engine.takion_decode import L1
from omnitrix.ui.main_window import OmnitrixWindow

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def check(n, ok, d=""):
    print(("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else ""))
    if not ok:
        FAILS.append(n)


GROUP, PORT = "239.7.7.51", 9986
SYM = b"QQQ".ljust(32, b"\x00")

pub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
pub.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
pub.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
stop = threading.Event()


def publisher():
    # <32s d d d d d d Q I i I I : sym, open, high, low, last, bid, ask,
    # cum_vol, time_ms, position, bid_size, ask_size.
    # time_ms is a 32-BIT field - milliseconds since midnight, not epoch.
    seq = 0
    vol = 1000
    px = 400.0
    while not stop.is_set():
        seq += 1
        vol += 500
        px += 0.01
        lt = time.localtime()
        ms = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) * 1000
        rec = L1.pack(SYM, px, px + 0.05, px - 0.05, px, px - 0.01, px + 0.01,
                      vol, ms, 0, 100, 100)
        batch = wire.encode(wire.CH_L1, seq, b"\x01" + rec, 1)
        try:
            pub.sendto(batch, (GROUP, PORT))
        except OSError:
            pass
        time.sleep(0.02)


threading.Thread(target=publisher, daemon=True).start()
time.sleep(0.3)

app = QApplication([])
feed = MulticastFeed(group=GROUP, port=PORT, iface="0.0.0.0", replay_host="")
win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
win.resize(1200, 800)
win.show()
win.start_feed()
t0 = time.time()
while time.time() - t0 < 7:
    app.processEvents()
    win._tick()
    time.sleep(0.01)
stop.set()
time.sleep(0.2)

check("the terminal joined the group", feed.connected["multicast"])
check("trades were reconstructed from the multicast L1 stream",
      len(win.series) > 0, f"series: {list(win.series)}")
s = win.series.get("QQQ")
bars = s.view(win.tf_s) if s else []
check("bars were built and are on the chart", len(bars) > 0,
      f"{len(bars)} bars, last close {bars[-1].close if bars else '-'}")
check("the chart pane is drawing them",
      len(win._panes[0].time_axis._bars) > 0,
      f"{len(win._panes[0].time_axis._bars)} bars on the axis")
check("no phantom gaps on a clean group", feed.gaps.lost == 0,
      str(feed.gaps.stats()))

feed.stop()
pub.close()
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    sys.exit(1)
print("CLIENT MULTICAST MODE OK")
