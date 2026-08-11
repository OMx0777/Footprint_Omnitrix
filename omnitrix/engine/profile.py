"""Session-level profiles built from tick-by-tick trades.

  SessionProfile  — volume-at-price (split buy/sell) plus Market-Profile TPO
                    brackets, with POC / value-area / naked-POC analytics.

Volume Profile answers "where did size trade?"; Market Profile (TPO) answers
"where did the market spend *time*?" — the two classic institutional reads.
"""

from __future__ import annotations

import numpy as np

from .model import split_size, Trade, Aggressor
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
        # THROUGH THE SHARED DEFINITION, not a local one. This split an UNKNOWN
        # print as size//2 to buy and the remainder to sell - a FIXED side for
        # the odd share, which is exactly the bias split_size exists to remove:
        # a 1-lot unclassified print counted as a whole sell here and as an
        # alternating share everywhere else, so the profile's delta drifted
        # from the footprint's over the same session with nothing to show why.
        b, sl = split_size(tr.size, tr.aggressor, ti)
        if b:
            self.buy[ti] = self.buy.get(ti, 0) + b
        if sl:
            self.sell[ti] = self.sell.get(ti, 0) + sl
        self.total += tr.size

        b = tr.ts_ms // 1000 // self.tpo_secs
        s = self.tpo.get(ti)
        if s is None:
            s = self.tpo[ti] = set()
        s.add(b)
        self.brackets.add(b)
        self._version += 1

    def add_bars(self, bars, tf_s: int) -> int:
        """Ingest sealed bars' footprints - for history that arrives as BARS.

        The profile was fed only from the live drain loop, so a symbol whose
        session was backfilled had a chart going back hours and a profile that
        began when the application did. The volume profile is one of the main
        reasons to have the history at all, so it has to receive it too.

        A bar's footprint is already split by aggressor through split_size at
        ingestion, so this adds the arrays directly rather than re-deriving the
        split - re-deriving is how four consumers ended up disagreeing about
        the same print in the first place.

        VECTORISED, because this runs on the GUI thread the moment a merge
        lands. Walking every price level of every bar in Python measured
        194 ms for one 6.5-hour session, and a four-chart grid merges four of
        them - which is the stall reported as "freezing after past data
        loads". Bars hold sorted int32 arrays, so the whole batch is summed
        with bincount and only the DISTINCT price levels are touched in
        Python: a few hundred instead of tens of thousands.

        TPO brackets come from each bar's own start_ts, which is market time,
        so a replayed bar lands in the bracket it actually traded in.

        Returns the volume added, so a caller can report what was gained.
        """
        if not bars:
            return 0
        # Group the batch by TPO bracket; a session is a dozen or so, and the
        # bracket is the only per-bar thing the profile needs.
        by_bracket: dict[int, list] = {}
        for bar in bars:
            by_bracket.setdefault(bar.start_ts // self.tpo_secs, []).append(bar)

        added = 0
        for bracket, group in by_bracket.items():
            tis, sells, buys = [], [], []
            for bar in group:
                t, sv, bv = bar.arrays()
                if t.size:
                    tis.append(t)
                    sells.append(sv)
                    buys.append(bv)
            if not tis:
                continue
            ti = tis[0] if len(tis) == 1 else np.concatenate(tis)
            sv = sells[0] if len(sells) == 1 else np.concatenate(sells)
            bv = buys[0] if len(buys) == 1 else np.concatenate(buys)
            # bincount over a zero-based index is far cheaper than a dict per
            # level; the offset makes negative tick indices safe.
            lo = int(ti.min())
            idx = (ti - lo).astype(np.intp)
            n = int(idx.max()) + 1
            s_tot = np.bincount(idx, weights=sv.astype(np.float64), minlength=n)
            b_tot = np.bincount(idx, weights=bv.astype(np.float64), minlength=n)
            nz = np.nonzero(s_tot + b_tot)[0]
            self.brackets.add(bracket)
            tpo = self.tpo
            buy_d, sell_d = self.buy, self.sell
            for k in nz.tolist():
                key = lo + k
                b_ = int(b_tot[k])
                s_ = int(s_tot[k])
                if b_:
                    buy_d[key] = buy_d.get(key, 0) + b_
                if s_:
                    sell_d[key] = sell_d.get(key, 0) + s_
                added += b_ + s_
                st = tpo.get(key)
                if st is None:
                    st = tpo[key] = set()
                st.add(bracket)
        if added:
            self.total += added
            self._version += 1
            self._cache.clear()
        return added

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
