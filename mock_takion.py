r"""Mock Takion DLL — feeds the LIVE pipe path without the real platform.

`SyntheticFeed` (the app's default) injects events in-process and never touches
the named pipes, so it exercises the charts but not `PipeFeed`: the wire
unpacking, the dual-pipe assembly, the 'C' sweep marker, the ET->local clock
alignment. This script drives that real path instead. It plays the DLL's role —
the pipe *client* — writing the exact 104-byte L1 and 32-byte L2 records the
extension emits, so the terminal cannot tell it from Takion.

Usage (two terminals):

    1)  py -m omnitrix.app --live --symbols QQQ,SPY
    2)  py mock_takion.py            --symbols QQQ,SPY

Start the app first: it is the pipe server and must create the pipes before a
client can connect. This one retries until they exist, then streams until Ctrl-C.

    --symbols   comma list (default QQQ,SPY,AAPL)
    --speed     time multiplier (default 1.0; 4 = 4x faster)
    --seed      RNG seed for a repeatable session

The wire formats are imported from the app itself (`pipe_feed.L1` / `L2`), so
this mock can never silently drift out of sync with the reader.
"""

from __future__ import annotations

import argparse
import random
import struct
import sys
import threading
import time

import win32file
import win32pipe
import pywintypes

from omnitrix.engine.pipe_feed import L1_PIPE, L2_PIPE, L1, L2

# Venues the extension reports depth for, in book order.
VENUES = [b"NSDQ", b"ARCA", b"BATS", b"EDGX", b"NYSE"]


def _midnight_local_ms() -> int:
    t = time.localtime()
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0,
                        t.tm_wday, t.tm_yday, t.tm_isdst))
    return int(base * 1000)


class Instrument:
    """A per-symbol random walk with a couple of *persistent* walls.

    Persistent walls are the whole point: they are what the heatmap draws as a
    continuous band and what the S/R tracker locks onto once they have held.
    A pure random book would never let those features show.
    """

    def __init__(self, symbol: str, tick: float, price: float, rng: random.Random):
        self.symbol = symbol
        self.tick = tick
        self.mid = price
        self.rng = rng
        self.cum_vol = 0
        self.depth = 18                    # book levels emitted per side
        # Fixed walls inside book depth, so they actually render and price can
        # travel between them. Further out than the book reaches would be
        # invisible - correct, but then S/R has nothing to lock onto.
        self.support_px = round(price - 12 * tick, 2)
        self.resist_px = round(price + 12 * tick, 2)
        self._wall_ttl = rng.randint(500, 1000)    # ticks before a wall relocates

    def step(self) -> None:
        # Mean-reverting walk that oscillates through the support/resist channel
        # so price repeatedly approaches each wall while both stay in the book.
        centre = 0.5 * (self.support_px + self.resist_px)
        drift = (centre - self.mid) * 0.02
        self.mid += drift + self.rng.uniform(-1, 1) * self.tick * 1.5
        # Keep price inside the channel it's meant to trade.
        self.mid = min(self.resist_px - self.tick,
                       max(self.support_px + self.tick, self.mid))
        self._wall_ttl -= 1
        if self._wall_ttl <= 0:
            # Occasionally relocate a wall (still in book) so S/R is seen
            # re-locking, not frozen.
            span = self.rng.randint(9, 15) * self.tick
            if self.rng.random() < 0.5:
                self.support_px = round(self.mid - span, 2)
            else:
                self.resist_px = round(self.mid + span, 2)
            self._wall_ttl = self.rng.randint(500, 1000)

    # ---- L1 -------------------------------------------------------------
    def l1_record(self) -> bytes:
        tick = self.tick
        bid = round(self.mid - tick, 2)
        ask = round(self.mid + tick, 2)
        aggressive_buy = self.rng.random() < 0.5
        last = ask if aggressive_buy else bid
        self.cum_vol += self.rng.randint(1, 12) * 100
        ms = (int(time.time() * 1000) - _midnight_local_ms())   # ms since midnight
        return L1.pack(
            self.symbol.encode()[:32].ljust(32, b"\x00"),
            self.mid, round(self.mid + 3 * tick, 2),
            round(self.mid - 3 * tick, 2), last, bid, ask,
            self.cum_vol, ms & 0xFFFFFFFF, 0,
            self.rng.randint(1, 40) * 100, self.rng.randint(1, 40) * 100)

    # ---- L2 -------------------------------------------------------------
    def l2_records(self) -> list[bytes]:
        """A full book sweep: B/A levels across venues, then the 'C' marker."""
        tick = self.tick
        sym8 = self.symbol.encode()[:8].ljust(8, b"\x00")
        out: list[bytes] = []
        best_bid = round(self.mid - tick, 2)
        best_ask = round(self.mid + tick, 2)

        for lvl in range(self.depth):
            bpx = round(best_bid - lvl * tick, 2)
            apx = round(best_ask + lvl * tick, 2)
            for venue in VENUES[: self.rng.randint(2, len(VENUES))]:
                bsz = self.rng.randint(1, 8) * 100
                asz = self.rng.randint(1, 8) * 100
                if abs(bpx - self.support_px) < tick / 2:
                    bsz += self.rng.randint(60, 120) * 100      # the support wall
                if abs(apx - self.resist_px) < tick / 2:
                    asz += self.rng.randint(60, 120) * 100      # the resist wall
                out.append(L2.pack(sym8, venue, bpx, bsz, b"B"))
                out.append(L2.pack(sym8, venue, apx, asz, b"A"))
        out.append(L2.pack(sym8, b"", 0.0, 0, b"C"))            # sweep complete
        return out


def _client(name: str, stop: threading.Event):
    """Connect to a server pipe as a client, retrying until it exists."""
    while not stop.is_set():
        try:
            h = win32file.CreateFile(
                name, win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, 0, None)
            win32pipe.SetNamedPipeHandleState(
                h, win32pipe.PIPE_READMODE_BYTE, None, None)
            print(f"  connected: {name}")
            return h
        except pywintypes.error as e:
            if e.winerror in (2, 231):        # not there yet / all instances busy
                time.sleep(0.4)
                continue
            raise
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="mock_takion")
    ap.add_argument("--symbols", default="QQQ,SPY,AAPL")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--tick", type=float, default=0.01)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    base = {"QQQ": 689.50, "SPY": 623.10, "AAPL": 227.40}
    insts = [Instrument(s, args.tick, base.get(s, 100 + i * 7), random.Random(args.seed + i))
             for i, s in enumerate(syms)]

    print(f"[mock] waiting for the app's pipes ({', '.join(syms)}) …")
    print("       start it first:  py -m omnitrix.app --live --symbols "
          + ",".join(syms))
    stop = threading.Event()
    h1 = _client(L1_PIPE, stop)
    h2 = _client(L2_PIPE, stop)
    if h1 is None or h2 is None:
        return 1

    trade_dt = 0.05 / args.speed          # ~20 trades/sec/symbol
    book_every = 5                        # a full book sweep every N trade ticks
    print("[mock] streaming — Ctrl-C to stop")
    n = trades = books = 0
    try:
        while True:
            for inst in insts:
                inst.step()
                win32file.WriteFile(h1, inst.l1_record())
                trades += 1
                if n % book_every == 0:
                    payload = b"".join(inst.l2_records())
                    win32file.WriteFile(h2, payload)
                    books += 1
            n += 1
            if n % 100 == 0:
                print(f"  sent {trades:,} L1 / {books:,} L2 sweeps")
            time.sleep(trade_dt)
    except KeyboardInterrupt:
        print(f"\n[mock] stopped — {trades:,} L1 / {books:,} L2 sweeps sent")
    except pywintypes.error as e:
        print(f"\n[mock] pipe closed by the app ({e.winerror}) — "
              f"{trades:,} L1 / {books:,} L2 sent")
    finally:
        stop.set()
        for h in (h1, h2):
            try:
                win32file.CloseHandle(h)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
