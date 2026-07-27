"""Session-level profiles built from tick-by-tick trades.

  SessionProfile  — volume-at-price (split buy/sell) plus Market-Profile TPO
                    brackets, with POC / value-area / naked-POC analytics.

Volume Profile answers "where did size trade?"; Market Profile (TPO) answers
"where did the market spend *time*?" — the two classic institutional reads.
"""

from __future__ import annotations

from .model import Trade, Aggressor
from .instruments import Instruments


class SessionProfile:
    def __init__(self, symbol: str, instruments: Instruments,
                 tpo_minutes: int = 30):
        self.symbol = symbol
        self.instruments = instruments
        self.tpo_secs = tpo_minutes * 60
        self.buy: dict[int, int] = {}        # tick_index -> aggressive buy vol
        self.sell: dict[int, int] = {}       # tick_index -> aggressive sell vol
        self.tpo: dict[int, set[int]] = {}   # tick_index -> set(bracket idx)
        self.brackets: set[int] = set()
        self.total = 0
        self._version = 0
        self._cache: dict = {}

    # ---- ingestion -------------------------------------------------------
    def add_trade(self, tr: Trade) -> None:
        ti = self.instruments.to_index(self.symbol, tr.price)
        if tr.aggressor is Aggressor.BUY:
            self.buy[ti] = self.buy.get(ti, 0) + tr.size
        elif tr.aggressor is Aggressor.SELL:
            self.sell[ti] = self.sell.get(ti, 0) + tr.size
        else:
            h = tr.size // 2
            self.buy[ti] = self.buy.get(ti, 0) + h
            self.sell[ti] = self.sell.get(ti, 0) + tr.size - h
        self.total += tr.size

        b = tr.ts_ms // 1000 // self.tpo_secs
        s = self.tpo.get(ti)
        if s is None:
            s = self.tpo[ti] = set()
        s.add(b)
        self.brackets.add(b)
        self._version += 1

    # ---- analytics (cached per version) ----------------------------------
    def _totals(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for ti, v in self.buy.items():
            out[ti] = out.get(ti, 0) + v
        for ti, v in self.sell.items():
            out[ti] = out.get(ti, 0) + v
        return out

    def analytics(self, va_pct: float = 0.70) -> dict:
        key = (self._version, va_pct)
        if self._cache.get("key") == key:
            return self._cache["val"]
        tot = self._totals()
        if not tot:
            val = {"poc": None, "vah": None, "val": None, "totals": {},
                   "hvn": [], "lvn": []}
        else:
            poc = max(tot, key=tot.get)
            idxs = sorted(tot)
            target = sum(tot.values()) * va_pct
            pos = idxs.index(poc)
            lo = hi = pos
            acc = tot[poc]
            n = len(idxs)
            while acc < target and (lo > 0 or hi < n - 1):
                up = tot[idxs[hi + 1]] if hi < n - 1 else -1
                dn = tot[idxs[lo - 1]] if lo > 0 else -1
                if up < 0 and dn < 0:
                    break
                if up >= dn:
                    hi += 1; acc += tot[idxs[hi]]
                else:
                    lo -= 1; acc += tot[idxs[lo]]
            # High/Low Volume Nodes, as a share of the busiest level: HVN >= 70%
            # of the peak, LVN <= 12%. (Deliberately a threshold, not a local
            # peak test — neighbour comparison on a thin book flags noise.)
            mx = max(tot.values())
            hvn = [ti for ti in idxs if tot[ti] >= 0.70 * mx]
            lvn = [ti for ti in idxs if tot[ti] <= 0.12 * mx]
            val = {"poc": poc, "vah": idxs[hi], "val": idxs[lo],
                   "totals": tot, "hvn": hvn, "lvn": lvn}
        self._cache = {"key": key, "val": val}
        return val

    @property
    def poc(self) -> int | None:
        return self.analytics()["poc"]

    def tpo_rows(self) -> list[tuple[int, list[int]]]:
        """[(tick_index, sorted bracket indices)] ascending by price."""
        return [(ti, sorted(bs)) for ti, bs in sorted(self.tpo.items())]

    def bracket_range(self) -> tuple[int, int] | None:
        if not self.brackets:
            return None
        return min(self.brackets), max(self.brackets)
