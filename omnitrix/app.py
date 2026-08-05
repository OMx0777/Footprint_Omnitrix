r"""Entry point.

    py -m omnitrix.app              # synthetic feed (develop any time)
    py -m omnitrix.app --live       # live Takion dual-pipe feed
    py -m omnitrix.app --live --symbols QQQ,SPY

With --live this process becomes the pipe *server*: it creates
\\.\pipe\TakionOHLCV and \\.\pipe\TakionData and waits for the Takion DLL to
connect (nothing else may hold those pipes). Every chart, profile and signal
works identically on either feed.
"""

import argparse
import os
import logging
import sys
import traceback

from PyQt6.QtWidgets import QApplication

from .engine.multicast_feed import (MulticastFeed, check_interface,
                                    pick_interface)
from .engine import (Instruments, SyntheticFeed, PipeFeed, NetworkFeed,
                     Recorder, ReplayFeed)
from .ui import OmnitrixWindow

log = logging.getLogger("omnitrix")


def _install_excepthook() -> None:
    """Stop one bad frame from killing the whole terminal.

    PyQt6 routes an unhandled Python exception raised inside a slot or a
    QGraphicsItem.paint() to qFatal(), which aborts the process immediately -
    no traceback, no chance to save, mid-session. Verified: a single ValueError
    in a QTimer slot terminates the app before the next line runs.

    That is the wrong trade for a trading terminal. A stale indicator on one
    frame is survivable; losing the chart mid-session because a book went empty
    at an unexpected moment is not. Installing a hook makes Qt treat the
    exception as handled, so the app logs it and keeps running.

    This is a backstop, NOT a licence to leave exceptions unhandled - anything
    that lands here is a real bug and the log line is how it gets found.
    """
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("unhandled exception (survived):\n%s",
                  "".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = hook


def _tune_gc() -> None:
    """Stop the cycle collector stalling the UI for work it never finds.

    MEASURED, 60 symbols at 900 trades/s:

        default (700,10,10)      18.8 gen-2 sweeps/min, worst frame gap 104 ms
        gen-2 rare (700,10,500)   0 sweeps,             worst frame gap  23 ms

    A gen-2 sweep walks every tracked object, and a busy desk holds a lot of
    them - 877,674 measured at 100 symbols, taking 139 ms to sweep. That is
    four frames lost, roughly twice a second, and it is a large part of the
    "goes sluggish after a while" feeling: the longer the session, the more
    objects there are, and the longer each sweep takes.

    Raising the threshold is safe HERE because of what the collector is for.
    It reclaims reference CYCLES; everything else is freed the moment its
    refcount hits zero. Measured across three runs - automatic GC on, gen-2
    off, and GC fully disabled - RSS grew 56.1 / 54.5 / 56.2 MB and a forced
    sweep found ZERO cyclic objects each time. The rings are dicts and deques
    that evict by refcount, so there was nothing cyclic to collect and the
    sweep was pure cost.

    Not DISABLED, though. Qt can create cycles and third-party code may too, so
    gen-2 still runs - about 50x less often. Anything cyclic is still
    reclaimed; it simply stops happening twice a second in the middle of a
    frame. tests/gc_gate.py fails if the engine ever starts creating cycles,
    because then this tuning would be holding real memory.
    """
    import gc
    gc.set_threshold(700, 10, 500)
    # Everything alive at startup - modules, Qt classes, the import graph - is
    # long-lived by definition and will never be garbage. Moving it to the
    # permanent generation takes it out of every future sweep.
    gc.freeze()


def main() -> int:
    ap = argparse.ArgumentParser(prog="omnitrix")
    ap.add_argument("--live", action="store_true",
                    help="read the live Takion named pipes instead of simulating")
    ap.add_argument("--symbols", default="",
                    help="comma-separated symbol filter (live mode); "
                         "empty = accept everything the DLL sends")
    ap.add_argument("--tick", type=float, default=0.01, help="price tick size")
    ap.add_argument("--record", metavar="FILE",
                    help="capture the event stream to FILE for later replay")
    ap.add_argument("--replay", metavar="FILE",
                    help="replay a previously captured session")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="replay speed (0 = instant, 1 = real time, 5 = 5x)")
    # Remote mode. The default comes from the environment so a packaged client
    # can ship a host without forking this file - see Host_Omnitrix/client_app.
    ap.add_argument("--network", metavar="HOST[:PORT]",
                    default=os.environ.get("OMNITRIX_HOST", ""),
                    help="connect to a remote Takion broadcaster over TCP "
                         "(e.g. 192.168.1.50:9999)")
    # Multicast is the mode a 100-desk LAN uses: one copy on the wire whatever
    # the desk count, against 1.27 Gbit/s for 100 unicast copies. --network is
    # kept for a single desk or for diagnosing whether a problem is the group.
    ap.add_argument("--multicast", metavar="GROUP[:PORT]", nargs="?",
                    const=os.environ.get("OMNITRIX_MCAST", "239.7.7.7"),
                    default=os.environ.get("OMNITRIX_MCAST_ON_CLIENT", ""),
                    help="join the multicast feed (default group 239.7.7.7:9997)")
    ap.add_argument("--replay-host", metavar="HOST[:PORT]",
                    default=os.environ.get("OMNITRIX_REPLAY", ""),
                    help="replay server for history and gap recovery "
                         "(default: the multicast server, port 9998)")
    ap.add_argument("--iface", default=os.environ.get("OMNITRIX_MCAST_IF", ""),
                    help="LAN address to join the group on. REQUIRED on a "
                         "machine with virtual adapters (Hyper-V/WSL/VMware), "
                         "where the OS may otherwise pick one no desk is on")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _install_excepthook()
    _tune_gc()

    app = QApplication(sys.argv)
    instruments = Instruments(default_tick=args.tick)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.replay:
        feed = ReplayFeed(args.replay, speed=args.speed)
        print(f"[omnitrix] REPLAY {args.replay} (speed={args.speed or 'max'})")
    elif args.live:
        feed = PipeFeed(symbols=syms or None)
        print("[omnitrix] LIVE mode — waiting for Takion to connect to "
              r"\\.\pipe\TakionOHLCV and \\.\pipe\TakionData …")
    elif args.multicast:
        group, _, gport_s = args.multicast.partition(":")
        gport = int(gport_s) if gport_s else 9997
        rhost, _, rport_s = (args.replay_host or "").partition(":")
        rport = int(rport_s) if rport_s else 9998
        if not rhost:
            # Without a replay host there is no gap recovery, and a lost
            # datagram then costs the book rather than a round trip. Say so
            # plainly - it is a downgrade, not a detail.
            print("[omnitrix] WARNING: no --replay-host; a lost datagram will "
                  "discard book state instead of being repaired, and the "
                  "chart will open with no history")
        iface = args.iface or pick_interface(rhost, group)
        if not args.iface and iface != "0.0.0.0":
            print(f"[omnitrix] interface {iface} chosen automatically "
                  f"(same subnet as {rhost}); override with --iface")
        feed = MulticastFeed(group=group, port=gport, iface=iface,
                             replay_host=rhost, replay_port=rport,
                             symbols=syms or None)
        warn = check_interface(iface)
        if warn:
            print(f"[omnitrix] WARNING: {warn}")
        print(f"[omnitrix] MULTICAST mode — group {group}:{gport} via "
              f"{iface}" + (f", replay {rhost}:{rport}" if rhost else ""))
    elif args.network:
        host, _, port_s = args.network.partition(":")
        port = int(port_s) if port_s else 9999
        feed = NetworkFeed(host=host, port=port, symbols=syms or None)
        print(f"[omnitrix] NETWORK mode — connecting to {host}:{port} …")
    else:
        feed = SyntheticFeed(
            symbols=syms or ["QQQ", "AAPL", "SPY"],
            start_price=400.0,
            tick=args.tick,
            trades_per_sec=60,
            prefill_minutes=90,
            seed=7,
        )

    win = OmnitrixWindow(feed, instruments)

    recorder = None
    if args.record:
        # After the window registered its callbacks (so we tap the chain) but
        # BEFORE the feed starts — SyntheticFeed.start() emits its whole prefill
        # synchronously, and attaching afterwards missed every event of it.
        recorder = Recorder(args.record)
        recorder.attach(feed)
        print(f"[omnitrix] recording -> {args.record}")

    win.start_feed()
    win.show()
    try:
        return app.exec()
    finally:
        if recorder:
            recorder.close()
            print(f"[omnitrix] captured {recorder.trades:,} trades / "
                  f"{recorder.books:,} books -> {args.record}")


if __name__ == "__main__":
    sys.exit(main())
