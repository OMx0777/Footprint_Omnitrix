"""Headless engine check: run the synthetic feed into the bar builder for a
couple of seconds and print what the footprint engine produced. No Qt, no UI.

Run:  python -m omnitrix.smoke_test
"""

import time

from omnitrix.engine import Instruments, BarSeries, SyntheticFeed


def main() -> None:
    instruments = Instruments(default_tick=0.01)
    series: dict[str, BarSeries] = {}
    trade_count = {"n": 0}
    latest_book = {}

    feed = SyntheticFeed(symbols=["QQQ", "AAPL"], trades_per_sec=200, seed=42)

    def on_trade(tr):
        s = series.get(tr.symbol)
        if s is None:
            s = series[tr.symbol] = BarSeries(tr.symbol, instruments, base_tf_s=60)
        s.add_trade(tr)
        trade_count["n"] += 1

    def on_book(bk):
        latest_book[bk.symbol] = bk

    feed.on_trade(on_trade)
    feed.on_book(on_book)
    feed.start()
    time.sleep(2.0)
    feed.stop()
    time.sleep(0.1)

    print(f"\nIngested {trade_count['n']} trades across {len(series)} symbols\n")

    for sym, s in series.items():
        bars = s.view(60)
        bar = bars[-1]
        poc = bar.poc
        vah, val = bar.value_area(0.70)
        buy_imb, sell_imb = bar.imbalances(factor=3.0)
        print(f"===== {sym}  ({len(bars)} bar(s), base_tf=60s) =====")
        print(f"  OHLC  O:{bar.open:.2f} H:{bar.high:.2f} L:{bar.low:.2f} C:{bar.close:.2f}")
        print(f"  volume={bar.volume}  delta={bar.delta:+d}  bull={bar.is_bull}")
        print(f"  price levels={len(bar.cells)}")
        if poc is not None:
            print(f"  POC={instruments.to_price(sym, poc):.2f}  "
                  f"VAH={instruments.to_price(sym, vah):.2f}  "
                  f"VAL={instruments.to_price(sym, val):.2f}")
        print(f"  buy-imbalance levels={len(buy_imb)}  sell-imbalance levels={len(sell_imb)}")
        print(f"  CVD(last)={s.cvd(60)[-1]:+d}")
        bk = latest_book.get(sym)
        if bk:
            print(f"  book: {len(bk.bids)} bid levels, {len(bk.asks)} ask levels, "
                  f"best_bid={bk.best_bid:.2f} best_ask={bk.best_ask:.2f}")

        # show a few footprint rows around the POC (ATAS-style bid x ask)
        print("  footprint (price     sell x buy):")
        for ti in sorted(bar.cells, reverse=True)[:8]:
            sell_v, buy_v = bar.cells[ti]
            mark = " <-- POC" if ti == poc else ""
            print(f"    {instruments.to_price(sym, ti):8.2f}   "
                  f"{sell_v:6d} x {buy_v:<6d}{mark}")
        print()


if __name__ == "__main__":
    main()
