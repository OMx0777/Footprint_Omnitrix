"""Data feeds. A feed pushes `Trade` and `BookSnapshot` events into two
callbacks; nothing else in the engine cares where the data came from.

    SyntheticFeed  — self-contained random order flow for development.
    (PipeFeed, the hardened Takion dual-pipe reader, plugs in here later and
     drives the exact same engine.)
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

from .model import Trade, BookSnapshot, Aggressor

TradeCB = Callable[[Trade], None]
BookCB = Callable[[BookSnapshot], None]


class Feed:
    """Base feed: register callbacks, then start()/stop()."""

    def __init__(self) -> None:
        self._on_trade: TradeCB | None = None
        self._on_book: BookCB | None = None
        self._running = False

    def on_trade(self, cb: TradeCB) -> None:
        self._on_trade = cb

    def on_book(self, cb: BookCB) -> None:
        self._on_book = cb

    def _emit_trade(self, tr: Trade) -> None:
        if self._on_trade:
            self._on_trade(tr)

    def _emit_book(self, bk: BookSnapshot) -> None:
        if self._on_book:
            self._on_book(bk)

    def start(self) -> None:      # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False


class SyntheticFeed(Feed):
    """A believable random-walk order-flow generator.

    Produces aggressive trades whose side depends on where the print lands
    relative to a simulated bid/ask, sprinkles in occasional large "iceberg"
    prints, and periodically publishes an L2 book snapshot around the mid.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        start_price: float = 400.0,
        tick: float = 0.01,
        trades_per_sec: float = 40.0,
        book_hz: float = 10.0,
        depth_levels: int = 42,
        prefill_minutes: int = 90,
        seed: int | None = None,
    ):
        super().__init__()
        self.symbols = symbols or ["QQQ"]
        self.tick = tick
        self.trades_per_sec = trades_per_sec
        self.book_hz = book_hz
        self.depth_levels = depth_levels
        self.prefill_minutes = prefill_minutes
        self._rng = random.Random(seed)
        self._mid = {s: start_price + i * 2 for i, s in enumerate(self.symbols)}
        self._thread: threading.Thread | None = None
        # persistent resting-liquidity walls at fixed prices (Bookmap-style
        # bright bands): {symbol: {price: size}}
        self._walls: dict[str, dict[float, int]] = {}
        for i, s in enumerate(self.symbols):
            self._walls[s] = self._seed_walls(start_price + i * 2)

    def _seed_walls(self, mid: float) -> dict[float, int]:
        rng = self._rng
        walls: dict[float, int] = {}
        for _ in range(rng.randint(4, 6)):
            off = rng.randint(-32, 32) * self.tick
            # mix of medium (white/yellow) and occasional mega (red) walls
            size = rng.randint(3000, 12000)
            if rng.random() < 0.3:
                size = rng.randint(20000, 40000)
            walls[self._snap(mid + off)] = size
        return walls

    def start(self) -> None:
        if self._running:
            return
        if self.prefill_minutes > 0:
            self._prefill()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _prefill(self) -> None:
        """Synthesize `prefill_minutes` of history ending now, emitting trades
        with historical timestamps so the chart is fully populated instantly."""
        now = time.time()
        start = now - self.prefill_minutes * 60
        for sym in self.symbols:
            t = start
            while t < now:
                # ~trades_per_sec compressed: emit a slice per simulated second,
                # spreading each trade's timestamp across the second so bubbles
                # spread horizontally (as they do with live prints).
                n = max(1, int(self.trades_per_sec / len(self.symbols)))
                for k in range(n):
                    ts_ms = int((t + k / n) * 1000)
                    self._emit_trade(self._gen_trade(sym, ts_ms))
                # one book snapshot per simulated second
                self._emit_book(self._make_book(sym, self._mid[sym], int(t * 1000)))
                t += 1.0

    # ---- internals -------------------------------------------------------
    def _snap(self, price: float) -> float:
        return round(round(price / self.tick) * self.tick, 10)

    def _gen_trade(self, sym: str, ts_ms: int) -> Trade:
        """Advance the mid a step and produce one aggressive print.

        Large resting walls damp the step, so price stalls when it runs into
        heavy liquidity — that stalling *is* absorption, and it makes the
        support/resistance levels behave like real ones."""
        rng = self._rng
        cur = self._mid[sym]
        step = rng.uniform(-1, 1) * self.tick
        target = cur + step
        for wp, wsz in self._walls.get(sym, {}).items():
            if wsz > 8000 and abs(target - wp) < self.tick * 1.5:
                step *= 0.12                     # heavy wall absorbs the move
                break
        mid = max(self.tick, cur + step)
        self._mid[sym] = mid
        spread = self.tick * rng.randint(1, 3)
        bid = self._snap(mid - spread / 2)
        ask = self._snap(mid + spread / 2)
        r = rng.random()
        if r < 0.46:
            price, aggr = ask, Aggressor.BUY
        elif r < 0.90:
            price, aggr = bid, Aggressor.SELL
        else:
            price, aggr = self._snap(mid), Aggressor.UNKNOWN
        if rng.random() < 0.03:
            size = rng.randint(1500, 6000)
        else:
            size = rng.randint(100, 800)
        return Trade(sym, price, size, aggr, ts_ms)

    def _run(self) -> None:
        trade_dt = 1.0 / self.trades_per_sec
        next_book = 0.0
        book_dt = 1.0 / self.book_hz

        while self._running:
            now = time.time()
            ts_ms = int(now * 1000)
            for sym in self.symbols:
                self._emit_trade(self._gen_trade(sym, ts_ms))
                if now >= next_book:
                    self._emit_book(self._make_book(sym, self._mid[sym], ts_ms))
            if now >= next_book:
                next_book = now + book_dt
            time.sleep(trade_dt)

    def _make_book(self, sym: str, mid: float, ts_ms: int) -> BookSnapshot:
        rng = self._rng
        walls = self._walls[sym]
        # occasionally retire a filled wall and post a fresh one elsewhere
        if rng.random() < 0.02 and walls:
            walls.pop(rng.choice(list(walls)))
            walls[self._snap(mid + rng.randint(-32, 32) * self.tick)] = \
                rng.randint(4000, 16000)

        bids: dict[float, int] = {}
        asks: dict[float, int] = {}
        for lvl in range(1, self.depth_levels + 1):
            base_b = max(80, int(rng.gauss(900, 350) / lvl))
            base_a = max(80, int(rng.gauss(900, 350) / lvl))
            pb = self._snap(mid - lvl * self.tick)
            pa = self._snap(mid + lvl * self.tick)
            bids[pb] = base_b + walls.get(pb, 0)
            asks[pa] = base_a + walls.get(pa, 0)
        return BookSnapshot(sym, bids, asks, ts_ms)
