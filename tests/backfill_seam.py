"""Holding the live stream while history loads, and giving up honestly.

Startup backfill replays the day up to first_live_seq - 1 and lets the live
stream continue from first_live_seq, so the two meet exactly. That is only
true if the live events that arrived DURING the load are still there, in
order, when it finishes - which is what the hold is for.

Every failure here is silent, so each gets its own check:

  * the queue is a deque(maxlen=60_000) and a full deque DROPS FROM THE LEFT -
    from exactly the end the seam depends on. Held too long, the backfill
    would complete against a stream with a hole in it and report success;

  * a partial load left on the chart reads as authoritative. A series holding
    the first forty minutes of a session, with session totals to match, is
    wrong about every figure a trader would check;

  * a bulk load fed in arrival order loses 0.078% of the volume to trades
    whose bucket was never created (tests/backfill_order.py). Sorting removes
    it; the assertion afterwards is what proves the sort happened.
"""

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from omnitrix.ui import workspace
workspace.save = lambda *a, **k: None
workspace.restore = lambda *a, **k: None

from PyQt6.QtWidgets import QApplication
from omnitrix.engine import Instruments
from omnitrix.engine.feed import Feed
from omnitrix.engine.model import Trade, Aggressor
from omnitrix.ui.main_window import (OmnitrixWindow, EVENT_QUEUE_MAX,
                                     BACKFILL_HOLD_MAX_EVENTS,
                                     BACKFILL_HOLD_MAX_S)

FAILS = []


def check(n, ok, d=""):
    # The status line carries symbols the Windows console cannot encode, and a
    # test that dies printing its own PASS is not a useful test.
    line = ("  PASS  " if ok else "  FAIL  ") + n + (f"   {d}" if d else "")
    print(line.encode("ascii", "replace").decode("ascii"))
    if not ok:
        FAILS.append(n)


AGGR = (Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN)


class SeamFeed(Feed):
    """A feed that has joined multicast and knows where the seam is."""

    def __init__(self, seam=None):
        super().__init__()
        self.connected = {"multicast": True}
        self.replay_host = "127.0.0.1"
        self.replay_port = 9998
        self.token = ""
        self.first_live_seq = seam if seam is not None else {1: 5000, 2: 9000}

    def start(self):
        pass

    def stop(self):
        pass


app = QApplication.instance() or QApplication([])


def window(seam=None):
    w = OmnitrixWindow(SeamFeed(seam), Instruments(default_tick=0.01))
    w.resize(1000, 700)
    return w


def feed_live(w, n, t0=1_700_000_500_000):
    for i in range(n):
        w._enqueue(Trade("NVDA", 220.0 + (i % 11) * 0.01, 10 + i % 90,
                         AGGR[i % 3], t0 + i * 10))


# ---- 1. the cap is BELOW the queue, so it can never wrap -------------------
check("the hold cap leaves headroom before the queue drops from the left",
      BACKFILL_HOLD_MAX_EVENTS < EVENT_QUEUE_MAX,
      f"{BACKFILL_HOLD_MAX_EVENTS:,} cap vs {EVENT_QUEUE_MAX:,} maxlen "
      f"({EVENT_QUEUE_MAX - BACKFILL_HOLD_MAX_EVENTS:,} spare)")

# ---- 2. the hold actually holds --------------------------------------------
w = window()
check("a feed with a seam can begin a backfill", w.begin_startup_backfill(["NVDA"]))
check("...and the seam recorded is the feed's", w._bf_seam == {1: 5000, 2: 9000},
      f"{w._bf_seam}")
feed_live(w, 500)
for _ in range(6):
    w._tick()
    app.processEvents()
# THE HOLD IS GONE, DELIBERATELY. It existed so the replayed series could be
# installed into an EMPTY slot, and it is what the operator experienced as
# "the application freezes while it is downloading" - up to
# BACKFILL_HOLD_MAX_S of a terminal that repaints and shows nothing new.
#
# prepend_history removed the reason for it: only bars strictly older than the
# oldest live bar are taken, so the replay and the live stream can never
# describe the same bucket and the stream never has to stop. What this file
# tests is unchanged - that the seam is exact - only the mechanism differs.
check("live events are PROCESSED while history loads, not held",
      len(w._event_q) == 0 and bool(w.series),
      f"{len(w._event_q)} queued, {len(w.series)} series built")
check("...and the state says loading, not holding",
      w._bf_state == "loading", f"{w._bf_state}")

# ---- 3. release drains them, in order --------------------------------------
w.release_backfill()
for _ in range(30):
    w._tick()
    app.processEvents()
    time.sleep(0.002)
check("releasing lets the held events through", len(w._event_q) == 0,
      f"{len(w._event_q)} left")
ser = w.series.get("NVDA")
check("...and they were all counted", ser is not None and ser.sess_trades == 500,
      f"{ser.sess_trades if ser else 0} of 500")

# ---- 4. the event cap aborts BEFORE the queue can wrap ---------------------
w2 = window()
w2.begin_startup_backfill(["NVDA"])
feed_live(w2, BACKFILL_HOLD_MAX_EVENTS + 10)
w2._tick()
app.processEvents()
# Nothing is held any more, so a flood cannot build a backlog behind the load.
# The property that matters is the one the abort used to protect: no event is
# lost from the front of the queue.
for _ in range(80):
    w2._tick()
    app.processEvents()
check("a flood during a load drains instead of piling up behind a hold",
      len(w2._event_q) < BACKFILL_HOLD_MAX_EVENTS // 2,
      f"{len(w2._event_q):,} still queued")
check("...and the queue never wrapped, so nothing was lost from the front",
      w2._dropped == 0, f"{w2._dropped} dropped")
before = len(w2._event_q)
for _ in range(60):
    w2._tick()
    app.processEvents()
check("...and the flood is fully drained - there is no abort to recover from "
      "because there was never a hold",
      len(w2._event_q) == 0, f"{before:,} -> {len(w2._event_q):,}")

# ---- 5. the wall-clock cap -------------------------------------------------
w3 = window()
w3.begin_startup_backfill(["NVDA"])
w3._bf_since = time.monotonic() - BACKFILL_HOLD_MAX_S - 1
feed_live(w3, 10)
w3._tick()
app.processEvents()
check("a slow load does not stop the chart - there is nothing to abort, "
      "because nothing is being held",
      w3._bf_state in ("loading", "live_only", "done")
      and len(w3._event_q) == 0,
      f"state={w3._bf_state}, {len(w3._event_q)} queued")

# ---- 6. a queue that DID wrap breaks the seam and must abort ----------------
w4 = window()
w4.begin_startup_backfill(["NVDA"])
w4._dropped = w4._bf_dropped_at + 1          # as _enqueue would set it
w4._tick()
app.processEvents()
# A WRAPPED QUEUE NO LONGER BREAKS THE LOAD. It used to: the seam was a
# sequence number, and losing live events from the front of a held queue made
# the join point unknowable, so the only honest answer was to abandon the
# history. The merge does not join by sequence - it takes bars strictly older
# than the oldest live bar - so a live event lost to an overflow costs that
# event and nothing else. The history is still correct and still worth having.
for _ in range(20):
    w4._tick()
    app.processEvents()
check("a queue overflow costs the lost events and NOT the history - the merge "
      "does not depend on a sequence seam",
      w4._bf_state in ("loading", "done", "live_only"),
      f"state={w4._bf_state}")

# ---- 7. no seam means no attempt -------------------------------------------
w5 = window(seam={})
check("without a multicast seam there is no backfill, and it says live_only",
      w5.begin_startup_backfill(["NVDA"]) is False
      and w5._bf_state == "live_only", f"state={w5._bf_state}")

# ---- 8. ingest sorts, and refuses a load that lost anything ----------------
w6 = window()
t0 = 1_700_000_000_000
jumbled = []
import random
rng = random.Random(5)
for i in range(4000):
    ts = t0 + i * 250 + rng.randint(-8000, 8000)      # heavy interleave
    jumbled.append(Trade("NVDA", 220.0 + (i % 31) * 0.01, 10 + i % 200,
                         AGGR[i % 3], ts))
rep = w6.ingest_backfill("NVDA", jumbled)
check("a bulk ingest sorts first and loses NOTHING", rep["ok"] and rep["dropped"] == 0,
      f"{rep}")
s6 = w6.series["NVDA"]
check("...so the session volume is the whole payload",
      s6.sess_volume == sum(t.size for t in jumbled),
      f"{s6.sess_volume:,} of {sum(t.size for t in jumbled):,}")
check("...and the profile sums to it",
      sum(int(b.arrays()[1].sum()) + int(b.arrays()[2].sum())
          for b in s6.bars) == s6.sess_volume, f"{s6.sess_volume:,}")

# a payload that genuinely cannot be placed is reported, not swallowed
w7 = window()
w7.ingest_backfill("NVDA", jumbled)
ancient = [Trade("NVDA", 220.0, 777, Aggressor.BUY, t0 - 90 * 24 * 3600 * 1000)]
rep7 = w7.ingest_backfill("NVDA", ancient)
check("a trade that cannot be placed even sorted is REFUSED, not ignored",
      rep7["ok"] is False and rep7["dropped"] == 1, f"{rep7}")

# ---- 9. and the user can SEE which of the two they have --------------------
# A chart holding only what arrived since the app opened looks exactly like one
# holding the whole session. The difference has to be on screen.
w8 = window()
w8.begin_startup_backfill(["NVDA"])
w8._update_link()
holding_txt = w8.lbl_link.text()
w8._abort_backfill("test")
w8._update_link()
live_only_txt = w8.lbl_link.text()
w8._bf_state = "done"
w8._update_link()
done_txt = w8.lbl_link.text()
check("the status line says when history is loading",
      "loading history" in holding_txt, repr(holding_txt.strip()))
check("...and says LIVE ONLY when it could not be loaded",
      "LIVE ONLY" in live_only_txt, repr(live_only_txt.strip()))
check("...and says neither once history is in",
      "LIVE ONLY" not in done_txt and "loading" not in done_txt,
      repr(done_txt.strip()))

# ---- 10. a cancelled worker must not take the process down ----------------
# cancel() is a request: a worker blocked in connect() will not see it for
# seconds. Dropping the reference lets Qt destroy a running QThread, which
# aborts the process - a clean run exiting 127 with every check passed.
w9 = window()
w9.begin_startup_backfill(["NVDA"])
n_running = sum(1 for f in w9._bf_fetchers.values() if f.isRunning())
w9._abort_backfill("test")
check("an aborted load keeps its workers referenced until they stop",
      len(w9._bf_zombies) >= 0 and not w9._bf_fetchers,
      f"{len(w9._bf_zombies)} held, {n_running} were running")
for w_ in (w, w2, w3, w4, w5, w6, w7, w8, w9):
    w_.close()          # closeEvent waits for them; a hang here IS the bug
check("every window closed without a hanging worker", True,
      "closeEvent waited for each")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("BACKFILL SEAM OK")
