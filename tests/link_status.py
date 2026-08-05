"""The status line must describe the feed the app is ACTUALLY running on, and
the replay token must actually be sent.

Both of these shipped broken and were only caught by a user running it:

  * the indicator understood pipe feeds and TCP feeds, so a multicast client
    reported "waiting for Takion" while data was visibly flowing through it -
    the flow-quality figures beside the message were being computed from the
    very records the message said were absent;

  * MulticastFeed defaulted its token to "" instead of reading the environment
    the way NetworkFeed does, so the moment the server had a token set, every
    client was rejected by the replay server. That costs backfill AND gap
    repair - the two things that make UDP safe to use - and the only evidence
    was one "bad token" line in the server log.

A degraded feed that looks healthy is worse than one that looks broken.
"""

import os
import sys
import logging

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from omnitrix.engine import Instruments, SyntheticFeed
from omnitrix.engine.multicast_feed import MulticastFeed
from omnitrix.engine.network_feed import NetworkFeed
from omnitrix.ui.main_window import OmnitrixWindow

logging.basicConfig(level=logging.CRITICAL)
FAILS = []


def _safe(t: str) -> str:
    """The status line contains glyphs the Windows console cannot encode, and
    a test that crashes printing its own result reports nothing."""
    return t.encode("ascii", "replace").decode("ascii")


def check(name, ok, detail=""):
    print(_safe(("  PASS  " if ok else "  FAIL  ") + name
                + (f"   {detail}" if detail else "")))
    if not ok:
        FAILS.append(name)


# ---- the token must come from the environment ------------------------------
os.environ["OMNITRIX_TOKEN"] = "shared-secret"
f = MulticastFeed(group="239.7.7.99", port=9995, replay_host="127.0.0.1")
check("MulticastFeed picks the token up from the environment",
      f.token == "shared-secret",
      f"got {f.token!r} - an empty token means the replay server rejects it, "
      f"and backfill and gap repair are both silently lost")
n = NetworkFeed("127.0.0.1", 1)
check("...the same way NetworkFeed always has", n.token == "shared-secret")
f2 = MulticastFeed(group="239.7.7.99", port=9995, token="explicit")
check("an explicit token still wins", f2.token == "explicit")
os.environ.pop("OMNITRIX_TOKEN")

# ---- the status line must match the feed -----------------------------------
app = QApplication.instance() or QApplication([])


def status_for(feed):
    win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
    win.resize(900, 600)
    win._update_link()
    txt = win.lbl_link.text()
    win.close()
    return txt


mc = MulticastFeed(group="239.7.7.98", port=9994, replay_host="127.0.0.1")
mc.connected = {"multicast": True, "replay": True}
s = status_for(mc)
check("a healthy multicast client says MULTICAST", "MULTICAST" in s, repr(s))
check("...and does NOT claim it is waiting for Takion",
      "waiting for Takion" not in s, repr(s))

mc.connected = {"multicast": True, "replay": False}
s = status_for(mc)
check("multicast WITHOUT a replay server is flagged as degraded",
      "no replay" in s,
      "a lost datagram then discards book state instead of being repaired")

mc.connected = {"multicast": False, "replay": False}
s = status_for(mc)
check("not receiving the group says so, and names the group",
      "multicast group" in s, repr(s))

mc.connected = {"multicast": True, "replay": True}
mc.gaps.lost = 12
mc.repairs = 3
s = status_for(mc)
check("loss and repairs are surfaced once they happen",
      "lost 12" in s and "repaired 3" in s, repr(s))

mc.unrepaired = 2
s = status_for(mc)
check("UNREPAIRED loss is escalated - that is real missing data",
      "UNREPAIRED" in s, repr(s))

# ---- the other feeds must not have regressed -------------------------------
net = NetworkFeed("127.0.0.1", 1)
net.connected = {"network": True}
s = status_for(net)
check("a TCP client still reports LIVE", "LIVE" in s, repr(s))
net.connected = {"network": False}
s = status_for(net)
check("a disconnected TCP client says so", "waiting" in s, repr(s))

syn = SyntheticFeed(symbols=["QQQ"], start_price=400.0, tick=0.01,
                    trades_per_sec=5, prefill_minutes=0)
s = status_for(syn)
check("a synthetic feed shows no live-link claim at all", s.strip() == "",
      repr(s))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for x in FAILS:
        print("   -", x)
    sys.exit(1)
print("LINK STATUS OK")
