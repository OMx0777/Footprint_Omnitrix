# Omnitrix — Order-Flow Terminal

An ATAS-style footprint terminal and Bookmap-style liquidity heatmap, built on
PyQt6 + pyqtgraph, fed by live Level 1 / Level 2 market data out of the Takion
trading platform.

Everything runs on three interchangeable data sources — **synthetic**, **live**
and **replay** — behind one `Feed` interface, so every chart, profile and signal
behaves identically on all three. You can develop the whole app at midnight on a
weekend and it will work unchanged at the open.

---

## Quick start

```cmd
pip install -r requirements.txt
py -m omnitrix.app                    :: synthetic feed — works anywhere, no Takion
```

That's it. A 90-minute session is pre-filled instantly, so the charts are
populated the moment the window opens.

---

## Running modes

```cmd
py -m omnitrix.app                                   :: synthetic order flow
py -m omnitrix.app --live                            :: live Takion dual-pipe feed
py -m omnitrix.app --live --symbols QQQ,SPY          :: live, filtered
py -m omnitrix.app --record session.omni             :: capture the event stream
py -m omnitrix.app --replay session.omni --speed 5   :: replay it at 5x
py -m omnitrix.smoke_test                            :: headless engine check, no Qt
```

| Flag | Meaning |
|---|---|
| `--live` | Read the Takion named pipes instead of simulating |
| `--symbols` | Comma list; empty accepts every symbol the DLL sends |
| `--tick` | Price tick size (default `0.01`); per-symbol overrides live in Settings |
| `--record FILE` | Tap the feed and write every event to a capture file |
| `--replay FILE` | Play a capture back through the same engine |
| `--speed` | `0` = as fast as possible, `1` = real time, `5` = 5× |

> **In live mode, pass `--symbols` unless you really want all 100.** The feed
> accepts everything the DLL sends; the event queue is bounded and will shed —
> and report — events if the GUI cannot keep up.

---

## Live mode

This app is the **pipe server**. It creates the pipes and waits; the Takion
extension DLL connects as a client.

1. Start the app first: `py -m omnitrix.app --live --symbols QQQ`
2. Then start Takion, with the `omnitrixds` column in a Market Sorter window.

The toolbar shows `● LIVE`, `● PARTIAL` or `○ waiting for Takion…`, plus a
`⚠ dropped N` warning if the queue is shedding.

Only one process can own the pipes at a time, so this app and the extractor's
own `test_all.py` cannot run together.

**Wire formats** (produced by the Takion extension in `Scraper_Takion_Omnitrix`):

| Pipe | Size | Layout |
|---|---|---|
| `\\.\pipe\TakionOHLCV` | 104 B | `<32sddddddQIiII` — symbol, OHLC, bid/ask, cum volume, time, position, sizes |
| `\\.\pipe\TakionData` | 32 B | `<8s8sdIc3x` — symbol, venue MMID, price, size, side (`B`/`A`/`C` = sweep complete) |

`takionTimeMs` is a **uint32 of milliseconds since midnight**, not epoch. It is
rebased onto today and then snapped to a 15-minute boundary (median of the first
25 records) so exchange-clock trades align with locally-stamped book snapshots.

### No Takion? Test the live path anyway

```cmd
py -m omnitrix.app --live --symbols QQQ,SPY    :: terminal 1 (pipe server)
py mock_takion.py --symbols QQQ,SPY            :: terminal 2 (plays the DLL)
```

`mock_takion.py` writes byte-exact 104/32-byte records as a pipe client, so it
exercises the real unpacking, sweep assembly and clock alignment — the parts the
synthetic feed skips entirely.

---

## What's in the window

**Main window** — footprint price pane over a cumulative-delta pane, with VWAP
and ±1σ/±2σ bands, EMAs, daily Central Pivot Range, and drawing tools
(Fibonacci, long/short position planner with live R:R, fixed-range volume
profile). Docks for Session Statistics, Time & Sales and Order-Flow Signals.

Chart modes: `Footprint`, `Cluster`, `Profile`, `Delta`, `Heatmap`, and the two
`+ Heatmap` combinations.

**Child windows** (toolbar): **Bookmap** liquidity heatmap with BBO, volume
dots, DOM ladder, projection bands and persistence-weighted S/R · **Market
Profile** TPO beside session volume profile · **Analytics** four time-aligned
microstructure panes · **Market Monitor** multi-symbol grid · **DOM Ladder**.

Layout, symbol, timeframe, mode, theme and order-flow settings persist to
`~/.omnitrix_workspace.json`.

---

## Architecture

```
omnitrix/
├── app.py            entry point / CLI
├── engine/           pure Python, ZERO Qt imports
│   ├── model.py        Trade, BookSnapshot, Aggressor
│   ├── feed.py         Feed base + SyntheticFeed
│   ├── pipe_feed.py    live Takion dual-pipe reader
│   ├── recorder.py     Recorder + ReplayFeed (capture / replay)
│   ├── bars.py         footprint bars, cached analytics, session stats
│   ├── bookmap.py      1-second time x price liquidity buffer
│   ├── profile.py      session volume profile + TPO
│   ├── levels.py       persistence-weighted support / resistance
│   ├── metrics.py      book imbalance, spread, intensity, delta / CVD
│   └── signals.py      block / absorption / wall-break detection
├── render/           pyqtgraph items — drawing only, no state
└── ui/               PyQt6 windows, widgets, workspace persistence
```

The engine is Qt-free by rule: it can be imported, tested and driven headlessly
(`smoke_test.py` does exactly that). Feed threads never touch widgets — they
append to one bounded queue drained on the GUI thread at ~30 fps, and using a
single queue for both trades and books preserves their true time order, so a
book always routes to a bar the preceding trades already created.

### Load-bearing details

- **Bars bucket on the trade's market timestamp**, never arrival time, so
  replay, pre-market and late ticks land in the correct bar.
- **Prices are integer tick indices**, so diagonal-imbalance neighbours are
  exact rather than float-fuzzy. Tick size is per-symbol.
- **Books are shared, never mutated.** The bookmap forward-fills by rebinding,
  which lets the renderer and the S/R tracker use identity checks as fast paths.
- **Session stats are O(1) running accumulators.** The Market Monitor reads them
  instead of re-aggregating each symbol's history; at 100 symbols that is the
  difference between ~286 ms and ~0.7 ms per refresh.
- **Toolbars and docks carry explicit `objectName`s.** Qt's `restoreState()`
  keys on them; without names the saved layout restores into garbage.

---

## Data fidelity — read this before trusting the numbers

The Takion extension publishes an **L1 summary and a book, but not the tape.**
Trades are therefore *reconstructed* from cumulative-volume deltas: the entire
delta between two L1 records is booked at `last` with a single aggressor.

- Bar volume is correct.
- Its **distribution across price within the bar is an estimate**, and delta /
  CVD is biased when a burst sweeps several prices between updates.
- Aggressor classification compares the print to the quote *in the same record*,
  i.e. after the fact.

Closing this needs a third pipe carrying real prints from the DLL. The Python
side is already shaped for it — `PipeFeed` emits `Trade` objects and everything
downstream consumes them, so only the feed would change.

Book snapshots are stamped at receipt rather than market time, and the DLL
batches 4096 records per write, so heatmap timing carries pipe latency.

---

## Licensing

Own code. The Takion SDK, its headers and the client itself are licensed by
Takion and are not part of this repository; running against live data requires
your own licensed installation and market-data entitlements.
