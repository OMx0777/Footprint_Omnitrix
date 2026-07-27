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
import sys

from PyQt6.QtWidgets import QApplication

from .engine import Instruments, SyntheticFeed, PipeFeed, Recorder, ReplayFeed
from .ui import OmnitrixWindow


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
    args = ap.parse_args()

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
