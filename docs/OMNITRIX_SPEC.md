# OMNITRIX — COMPLETE BUILD SPECIFICATION

A rebuild document. Everything needed to construct this system from nothing:
formats, algorithms, thresholds, architecture, and the reasoning behind each
decision — including the mistakes that produced them.

Written for a competent programmer who has never seen this codebase.

    Source of truth : Footprint_Omnitrix/omnitrix     (git, branch rebuild/pyqt-order-flow-terminal)
    Revision        : 94232de
    Client package  : 18,589 lines Python
    DLL             : 4,147 lines C++
    Tests           : 54 client + 8 server + 4 gates

--------------------------------------------------------------------------------
TABLE OF CONTENTS
--------------------------------------------------------------------------------

     0.  What you are building
     1.  System topology
     2.  The C++ DLL (data extraction)
     3.  Wire protocol
     4.  The server (broadcast, record, replay)
     5.  The client: layering rules
     6.  Decoding: raw records -> Trade / BookSnapshot
     7.  Aggressor classification (Lee-Ready)
     8.  THE SPLIT — the single most important invariant
     9.  Bar and BarSeries (the footprint)
    10.  BookmapBuffer (the heat field)
    11.  SessionArchive (whole-session heat, compressed)
    12.  SessionProfile (volume + TPO)
    13.  Signals
    14.  Rendering
    15.  The frame governor and watchdog
    16.  Hot/cold retention
    17.  History and backfill
    18.  Failure handling
    19.  Data-truth rules (READ THIS)
    20.  Dependencies and environment
    21.  Build and run
    22.  Testing philosophy and the gates
    23.  Complete constant reference
    24.  Bug archive — mistakes that shaped the design
    25.  Legal and licensing constraints
    26.  If you rebuild this, do these things differently

--------------------------------------------------------------------------------
0. WHAT YOU ARE BUILDING
--------------------------------------------------------------------------------

A real-time order-flow terminal. Not a price chart — a chart of *who was
aggressive at which price*, plus a picture of resting liquidity over time.

Three views of one session:

  FOOTPRINT   Each bar is split by price level into aggressive buy volume and
              aggressive sell volume. Shows imbalances, point of control (POC),
              and value area. This is the primary view.

  BOOKMAP     A time x price heat field of resting limit orders (the book),
              with executed trades drawn over it as size-scaled bubbles. Shows
              where liquidity sits, when it is pulled, and what traded into it.

  PROFILE     Session volume distribution by price, plus TPO (time at price).

Scale requirement: ~96-200 symbols streaming simultaneously on one machine,
one GUI thread, with a hard frame budget. Most of the engineering below exists
because of that requirement, not because of the charting.

--------------------------------------------------------------------------------
1. SYSTEM TOPOLOGY
--------------------------------------------------------------------------------

Three separate programs on (usually) two machines.

    [ Broker terminal: Takion ]           <- 3rd-party Windows app
              |
              |  TakionAdditionalColumns.dll  (YOUR C++ CODE, loaded INSIDE it)
              |  uses the Takion SDK to read L1 ticks and the L2 montage
              v
       named pipes:  \\.\pipe\TakionOHLCV   (L1, 104-byte records)
                     \\.\pipe\TakionData    (L2,  32-byte records)
              |
              v
    [ Server: broadcaster_server.py ]
              |
              +--> recorder.py     -> C:\omnitrix\data\YYYY-MM-DD\  (+ .idx), 5-day retention
              +--> UDP multicast   -> 239.7.7.7:9997   (live stream, sequenced)
              +--> replay_server   -> TCP :9998        (history + gap repair)
              +--> legacy TCP      -> TCP :9999        (unsequenced stream)
              |
              v
    [ Client: PyQt6 app, 1..N instances ]
       MulticastFeed -> TakionDecoder -> Trade / BookSnapshot
                                              |
                       BarSeries       <------+   footprint
                       BookmapBuffer   <------+   heat + tape
                       SessionProfile  <------+   volume/TPO

WHY TWO TRANSPORTS

  Multicast for live: one send reaches every client. With 100 clients, unicast
  would be 100x the bandwidth. Multicast is unreliable by nature, which is
  acceptable *because* every batch carries a sequence number.

  TCP for history and repair: exact byte ranges by sequence number. A gap is
  refilled with precisely the bytes that were missed — not patched with a
  snapshot, which would silently differ.

--------------------------------------------------------------------------------
2. THE C++ DLL (DATA EXTRACTION)
--------------------------------------------------------------------------------

Location: Scraper_Takion_Omnitrix/src/
Builds to: TakionAdditionalColumns.dll
Loaded by: the Takion terminal, as a custom-column plugin.

This is the only sanctioned way to get the data. The DLL runs inside Takion's
process and calls its SDK. There is no external API.

2.1 THE TWO RECORD FORMATS

Both are `#pragma pack(push, 1)`. The memory layout IS the protocol — Python
unpacks these byte-for-byte with `struct`. Do not add padding or reorder.

    #pragma pack(push, 1)
    struct TakionTick {              // L1 — 104 bytes
        char             symbol[32];
        double           open;
        double           high;
        double           low;
        double           last;
        double           bid;
        double           ask;
        unsigned __int64 volume;         // CUMULATIVE session volume
        unsigned int     takionTimeMs;   // ms since midnight, EXCHANGE tz
        int              posSize;
        unsigned int     bidSize;
        unsigned int     askSize;
    };
    #pragma pack(pop)

    #pragma pack(push, 1)
    struct TakionL2Quote {           // L2 — 32 bytes
        char         symbol[8];
        char         mmid[8];        // market maker / ECN identifier
        double       price;
        unsigned int size;
        char         side;           // 'B' bid, 'A' ask, 'C' sweep-complete
        char         pad[3];
    };
    #pragma pack(pop)

Python equivalents (omnitrix/engine/takion_decode.py):

    L1 = struct.Struct("<32sddddddQIiII")   # 104 bytes
    L2 = struct.Struct("<8s8sdIc3x")        #  32 bytes

2.2 THREE PROPERTIES OF THIS FORMAT THAT DRIVE EVERYTHING DOWNSTREAM

(a) VOLUME IS CUMULATIVE, NOT PER-TRADE.

    A trade's size = (this snapshot's cumulative volume) - (previous snapshot's).

    Consequence: after ANY feed gap, that difference spans the entire gap. The
    first record back would emit ONE synthetic print carrying every share that
    traded while you were away. Measured on a 30-second gap: 3,999,900 shares
    at a single price with a single aggressor. It trips block detectors and
    moves cumulative delta by millions.

    THE RULE: on any gap or reconnect, CLEAR the cumulative-volume table. The
    next record re-seeds (emits nothing). The missed volume is then honestly
    absent rather than fabricated. An honest hole beats a fake block.

(b) L2 ARRIVES AS PER-LEVEL RECORDS, ASSEMBLED INTO A SWEEP.

    You receive many records describing individual price levels. The sweep is
    only complete when a record with side == 'C' arrives. That marker carries
    an absolute epoch-milliseconds timestamp in the otherwise-unused `price`
    field.

    CRITICAL: only ~16% of L2 batches contain a 'C' marker. Any code that maps
    a byte offset to a wall-clock time must SCAN FORWARD for the next marker,
    not assume the batch it landed on has one. (This exact assumption caused a
    server-side bug where 84% of timestamp lookups returned None and the binary
    search collapsed to the start of the day.)

(c) takionTimeMs IS EXCHANGE-LOCAL.

    Not UTC, not machine-local. The client must measure the offset at runtime
    by comparing L1 times against the L2 'C' marker's absolute epoch time, and
    must WITHHOLD trades until that offset locks (25 samples). Emitting
    unshifted trades places them hours from the book clock — for an IST machine
    watching US markets that is 9.5 hours — creating a bogus bar at the head of
    the series that skews VWAP and CVD (both cumulative from bar 0) for the
    entire session.

2.3 RING BUFFERS

    L1 ring:  4,096 slots
    L2 ring:  1,048,576 slots  (32 MB at 32 bytes/slot)

    The L2 ring was 262,144 and had to be raised. On a deeper-entitlement
    account the montage carries hundreds of levels per symbol instead of ~20
    per side; 262,144 slots was under one second of buffer. 32 MB is nothing
    compared to losing records.

2.4 THE CRASH WORKAROUND

    The poller does NOT call back into TakionData.dll. Doing so faulted
    reproducibly at a fixed offset (TakionData.dll+0x2dd6, observed twice).
    NBBO is cached on the way past instead (`OmnitrixCacheNbbo()`), so the
    poll path never re-enters the vendor DLL.

    If you rebuild against a different vendor SDK: assume re-entrant calls from
    a polling thread are unsafe until proven otherwise.

--------------------------------------------------------------------------------
3. WIRE PROTOCOL
--------------------------------------------------------------------------------

omnitrix/engine/wire.py

A 13-byte header in front of a run of raw records.

    HEADER = struct.Struct("<BBBHQ")     # 13 bytes
      B  magic    0x4F  ('O')
      B  version
      B  channel  1 = L1, 2 = L2
      H  count    number of records in this batch
      Q  seq      uint64, MONOTONIC PER CHANNEL

Records follow immediately: each is one type byte (1 or 2) then the packed
struct. So an L1 record on the wire is 1 + 104 = 105 bytes.

RULES

  * A batch is consumed WHOLE OR NOT AT ALL. Applied in halves it publishes a
    partial book; half a sweep merged into the next reads as liquidity that
    does not exist. If the buffer holds fewer bytes than the batch needs, wait.

  * The sequence number is what makes loss detectable. Track last-seq per
    channel; a jump means a gap of known extent, and the exact range can be
    requested: `REPLAY <channel> <from_seq> <to_seq>`.

  * Support BOTH framings on the client: a batch header (sequenced server) and
    a bare type byte (legacy stream). This is what allows the server to be
    upgraded before all clients are. A flag day across 100 machines is not a
    deployment plan.

  * Beware the split header. If the buffer starts with the magic byte but holds
    fewer than 13 bytes, RETURN and wait. `decode_header` cannot distinguish
    "not a header" from "not enough bytes yet", and the legacy fallback would
    eat the magic byte as a record type and resync past it — discarding the
    sequence number that proves nothing was lost.

--------------------------------------------------------------------------------
4. THE SERVER
--------------------------------------------------------------------------------

Host_Omnitrix/

    broadcaster_server.py   Reads both named pipes. Frames records into
                            sequenced batches. Publishes to multicast, feeds
                            the recorder, serves legacy TCP on :9999.

    recorder.py             Appends batches to a day file plus a side index.
                            Buffers to 1 MB before writing: at 173 sweeps/sec a
                            per-batch write is 173 syscalls/second competing
                            with the feed. Retains 5 days.

    replay_server.py        TCP :9998. Serves byte ranges by sequence, and
                            resolves "which sequence covers this time".

    seqat.py                Timestamp <-> sequence mapping by scanning the
                            recording. Scans FORWARD for a 'C' marker.

    symfilter.py            Filters a replay stream to one symbol by matching
                            the symbol string at a fixed offset (32 bytes into
                            an L1 record, 8 into an L2) WITHOUT decoding.

4.1 THE INDEX

    IDX = struct.Struct("<QQ")     # (seq, byte_offset) — NO TIMESTAMPS

Timestamps are derived by scanning, never stored. This keeps the index small
and — more importantly — keeps ONE definition of "what time is this byte".
Two definitions can disagree; one cannot.

4.2 RESUMING

On restart mid-day the server resumes the existing day file at the next
sequence rather than starting a new one, so a restart does not create a
discontinuity in the recorded stream.

--------------------------------------------------------------------------------
5. THE CLIENT: LAYERING RULES
--------------------------------------------------------------------------------

    omnitrix/
      engine/     Data structures and decoding. IMPORTS NO Qt. Testable headless.
      render/     pyqtgraph GraphicsObjects. Draws engine objects.
      ui/         Windows, panes, docks, event loop, policy.
      paintguard.py   Imports NOTHING from omnitrix (everything imports it).
      app.py      Entry point, argument parsing, logging, excepthook.

The dependency direction is strict: engine knows nothing about render or ui.
This is what makes the engine testable without a display and what keeps the
data model from acquiring Qt semantics.

`paintguard.py` sits outside all three because both render/ and ui/ import it;
any dependency it had would close a cycle
(render -> ui.framegov -> ui/__init__ -> main_window -> render).

--------------------------------------------------------------------------------
6. DECODING: RAW RECORDS -> Trade / BookSnapshot
--------------------------------------------------------------------------------

omnitrix/engine/takion_decode.py

6.1 THE DATA MODEL

    Trade:         symbol, price, size, aggressor (BUY/SELL/UNKNOWN), ts_ms
    BookSnapshot:  symbol, bids{price: size}, asks{price: size}, ts_ms

6.2 L1 -> Trade

    1. Reject unusable prices: last <= 0 or NaN. Count them per symbol
       (sym_zero_px) so a broken feed is visible rather than silently thinned.
    2. size = cum_vol - last_cum_vol[symbol]; store the new cum_vol.
       If no previous value, or cum_vol <= previous, emit NOTHING (re-seed or
       volume reset).
    3. Classify the aggressor (section 7).
    4. Align the timestamp (section 6.4).
    5. Emit.

6.3 L2 -> BookSnapshot

    Accumulate per-level records into a pending dict per symbol. On a 'C'
    marker: publish the assembled BookSnapshot with the marker's epoch time,
    then clear the accumulator.

    On disconnect/gap: DISCARD every half-assembled book. The merge is additive
    (`d[price] = d.get(price, 0) + size`), so a partial book surviving into the
    next sweep makes levels present on both sides of the outage report their
    COMBINED size, and levels pulled during the outage survive as phantom
    liquidity.

6.4 THE CLOCK

    L1 gives ms-since-midnight in exchange time. The 'C' marker gives absolute
    epoch ms. Measure the offset from ~25 samples, then apply it to every L1
    timestamp.

    While measuring, HOLD trades in a pending list (cap it — 
    _TS_PENDING_MAX) and emit them once the offset locks, re-aligning each.
    Do not emit un-aligned trades.

--------------------------------------------------------------------------------
7. AGGRESSOR CLASSIFICATION (LEE-READY)
--------------------------------------------------------------------------------

Four tiers, strongest evidence first. Count each tier separately so the share
of guessing is VISIBLE, not hidden.

    quote   Price is at or through a side of the NBBO. Direct evidence.
            at/above ask -> BUY;  at/below bid -> SELL.

    mid     Inside the spread: which side of the midpoint it landed on.
            Inference, but the standard one — a print above the mid was far
            likelier taken from the offer.

    tick    Exactly at the mid, or no usable quote: compare with the last
            DIFFERENT trade price. Uptick -> BUY, downtick -> SELL.

    ztick   Same price as the last print: inherit the direction of the last
            price CHANGE. This is the zero-tick half of Lee-Ready.

Only a print with no quote, no price change, AND no prior direction stays
UNKNOWN.

WHY ztick MATTERS: without it, UNKNOWN climbed from 3% to 19% across a live
post-market session, because both conditions for it (a print at the mid, and a
price that has not moved) get commoner as a book thins. Every one of those was
being split 50/50 — i.e. 19% of the session's flow was guessed.

Expose the attribution share in the UI ("flow 88% attributed, 73% quoted").
A trader must be able to see how much of the delta is inferred.

--------------------------------------------------------------------------------
8. THE SPLIT — THE SINGLE MOST IMPORTANT INVARIANT
--------------------------------------------------------------------------------

omnitrix/engine/model.py :: split_size()

    def split_size(size, aggressor, tick_index) -> (buy, sell):
        if aggressor is BUY:  return size, 0
        if aggressor is SELL: return 0, size
        buy = size // 2
        if (size & 1) and (tick_index & 1):   # odd size, odd price
            buy += 1
        return buy, size - buy

An UNKNOWN print carries no directional information. Attributing all of it to
either side FABRICATES DATA. It is split evenly.

The odd share of an odd size cannot be split in integers, so it alternates on
the PRICE's parity. Two reasons:

  * A fixed side accumulates real bias — a 1-lot unclassified print would count
    as a whole buy, every time.
  * Keying on the trade itself (not a per-consumer counter) means every
    consumer independently computes the SAME answer for the same print.

FIVE consumers have decided this for themselves. Four were wrong, all leaning
the same direction (green):

    - the bubble/pie/split-bar overlay counted UNKNOWN as 100% BUY
    - the tape reader's prints and CVD did the same
    - the footprint gave the odd share to BUY, the bookmap to SELL, so two
      panes disagreed about the same trade
    - SessionProfile used `size//2` to buy and the remainder to sell — a FIXED
      side — so its delta drifted from the footprint's over a session with
      nothing on screen to explain why

ENFORCE IT MECHANICALLY. Reasoning about it has failed five times. Two checks:

  1. Static: no module outside model.py may contain `// 2` inside a function
     that branches on the aggressor.
  2. Behavioural: profile, footprint and bookmap must agree EXACTLY on a stream
     built from odd sizes and UNKNOWN aggressors — the only case where a local
     split can differ.

(Verify your static check against the real historical code before trusting it.
The first version of ours keyed on the word "UNKNOWN" and would have missed the
actual bug, which branched on BUY/SELL and put the split in a bare `else`.)

--------------------------------------------------------------------------------
9. BAR AND BarSeries (THE FOOTPRINT)
--------------------------------------------------------------------------------

omnitrix/engine/bars.py

9.1 Bar

Fields: start_ts, tf_s, open, high, low, close, volume, delta, book, cells.

    cells: dict[tick_index -> [sell_vol, buy_vol]]

Bucketing: by the TRADE's market timestamp, never by local arrival time.
Replay, pre-market and late ticks then all land in the correct bar.

    bucket = (ts_ms // 1000 // base_tf_s) * base_tf_s

Base timeframe is 10 seconds. Everything coarser is aggregated on demand.

9.2 seal() — THE COMPACTION

A finished bar never changes again, so it is frozen into three sorted int32
arrays and the dict is dropped:

    _ti[]    tick indices, ascending
    _sell[]  sell volume
    _buy[]   buy volume

Measured: 154 bytes per price level as a dict; 8.1x smaller as arrays. Over a
100-symbol basket across a 16-hour session that is the difference between ~7 GB
and ~3.7 GB. A process that swaps is a process that lags.

`arrays()` is THE way to read a footprint. It works for a live bar (built from
the dict) and a sealed one (returned directly, no copy). Never reach for
`cells` — it is None after sealing.

A late tick into a sealed bar must still be accepted: `_thaw()` rebuilds the
dict, takes the trade, and the bar re-seals. Rare by construction.

9.3 ANALYTICS (computed once, cached)

    POC          tick index with the greatest total volume
    Value area   see below
    Imbalances   see below

VALUE AREA (70% by default):
    Start at the POC. Repeatedly extend to whichever adjacent level (above or
    below) has more volume, accumulating, until the accumulated volume reaches
    `pct` of the bar's total. Return (high_ti, low_ti).
    Walk the sorted arrays — do NOT build a dict to do it.

DIAGONAL IMBALANCES:
    A buy imbalance at level `ti` compares buy[ti] against sell[ti-1]
    (the sell one level BELOW). A sell imbalance compares sell[ti] against
    buy[ti+1] (the buy one level ABOVE). That diagonal is the point: it
    compares the two sides that could have traded with each other.

    buy_imbalance(ti)  = buy[ti]  >= min_vol AND (sell[ti-1] == 0 OR buy[ti]  >= factor * sell[ti-1])
    sell_imbalance(ti) = sell[ti] >= min_vol AND (buy[ti+1]  == 0 OR sell[ti] >= factor * buy[ti+1])

    Defaults: factor 3.0, min_vol 20.
    A diagonal neighbour that never traded (0) COUNTS as an imbalance.

9.4 AGGREGATION (view(tf_s))

Fold base bars into coarser groups. VECTORISE THIS. The naive version walks
every price level of every bar through a Python dict and costs 35 ms over a
6.5-hour session — paid every time a symbol or timeframe is selected, and
growing linearly with the session.

    For each group:
      concatenate the members' (ti, sell, buy) arrays
      argsort once by ti
      find group boundaries with np.flatnonzero(np.diff(ti))
      sum with np.add.reduceat

    Result: 35.4 ms -> 12.8 ms at 2,340 bars.

Keep analytics LAZY on folded bars — the old path sealed every group eagerly,
computing POC and value area for bars that may never be drawn.

CACHE the fold per timeframe, keyed on a version counter, and rebuild only the
DIRTY TAIL. Live data only ever appends, so all but the last group are already
correct. Without this, a live feed invalidates the cache on every trade and each
redraw re-folds the whole session: measured 0.57 ms at one hour, 4.77 ms at
seven, times five call sites per frame. That is the "gets laggy after a few
hours" report.

9.5 PRICE AGGREGATION

At a 1-hour candle a penny-ticked name puts hundreds of rows in one bar and the
cells collapse into an unreadable stripe. Fold `step` ticks into one row.
Offer Auto (pick from the zoom so rows stay readable while panning) plus fixed
choices (1 tick, 1c, 5c, 10c, 25c, 50c, $1).

--------------------------------------------------------------------------------
10. BookmapBuffer (THE HEAT FIELD)
--------------------------------------------------------------------------------

omnitrix/engine/bookmap.py

10.1 Column — one per `col_dt` seconds (1.0 by default)

    bucket    int(ts_s // col_dt)   — the ABSOLUTE bucket is the x coordinate,
                                      so columns and trade bubbles share one
                                      continuous time axis and scroll without
                                      re-indexing
    book      PriceLadder — resting size by tick index
    buy/sell  dict[tick_index -> aggressive volume]
    bid_ti, ask_ti
    vol       total volume
    net       SIGNED aggressive volume, maintained incrementally
    sweeps    how many book snapshots were ACTUALLY OBSERVED here

FORWARD FILL: a column with no sweep inherits its predecessor's book by
REFERENCE (ladders are immutable, so sharing is free). This makes a standing
wall draw as one continuous band instead of scattered dashes.

BUT: `sweeps` is NOT inherited. A column carrying a forward-filled book has
sweeps == 0, and the renderer fades it. This is the difference between drawing
liquidity and drawing an assumption.

`net` is maintained per trade rather than recomputed. The volume-bar renderer
needs it to colour each bar, and recomputing it there meant two sum() passes
over the column's whole price dict on every frame, for every visible column.

10.2 PriceLadder — the app's dominant allocation

Two int32 arrays (ti ascending, sz parallel), NOT a dict.

    dict[int,int] of boxed ints : 23.6 kB per column
    two int32 arrays            :  2.3 kB per column
    ratio                       : 10.4x
    across 100 symbols at cap   : 4.2 GB -> 0.4 GB

READ-ONLY BY CONTRACT. Columns share ladders by reference for forward-fill, so
mutating one silently rewrites history for every column carrying it. Build a
new ladder; never modify one.

Build it in ONE vectorised pass and cache it on the snapshot, so the
BookmapBuffer and the BarSeries share one ladder instead of each building it.
Measured: 774,000 to_index() calls for 3,000 books — 258 per book, each
re-doing symbol.upper() and a dict lookup before a divide — 73% of the drain.

FILTER BEFORE THE CAST. `astype(np.int32)` on NaN or 1e12 does not raise, it
WRAPS — putting a phantom wall at an arbitrary price that looks exactly like
real liquidity. Drop non-finite, non-positive and out-of-range prices first.
A level that is missing is survivable; a level that is invented is not.

10.3 THE TAPE

Four parallel ring arrays (x, tick_index, size, aggressor-code), not a deque of
tuples.

    deque of (float,int,int,Aggressor) : 261 bytes to carry 17 bytes of data
                                          = 15.65 MB per symbol at 60,000
                                          = 63.6% of process footprint
    four parallel arrays               : 1.02 MB per symbol

Grow the ring ON DEMAND rather than allocating full size: a thin symbol never
pays for depth it does not use.

CAREFUL WITH SHRINK-THEN-GROW. A shrunk ring is ROTATED. Growth that assumes
the ring never wrapped copies it verbatim and every slot past the old capacity
reads uninitialised memory — which surfaced as an aggressor code outside 0..2
while painting, but could equally have been a plausible wrong price. Do not
derive the retained count from a session total that does not shrink; track it
explicitly. Test against a deque at EVERY head alignment, not one.

--------------------------------------------------------------------------------
11. SessionArchive (WHOLE-SESSION HEAT, COMPRESSED)
--------------------------------------------------------------------------------

omnitrix/engine/heatarchive.py

PROBLEM: the live ring holds 1,400 one-second columns ~ 23 minutes. A full
session at that resolution is 23,400 columns/symbol = 54 MB/symbol = 10.8 GB
across 200 symbols.

OBSERVATION: nobody reads a six-hour heatmap at one second and one cent. At
that zoom a single pixel column already spans minutes — the screen throws the
precision away before the eye sees it.

SOLUTION: fold columns as the live ring evicts them. Three compressions:

  TIME       30 live columns -> 1 slot, resting liquidity TIME-AVERAGED.
             A wall that stood the whole slot stays bright; a flash order that
             showed for one second fades to a thirtieth of its size. Duration
             is information — a spoof should not look like real size.

  PRICE      4 ticks -> 1 bin, SUMMED ("this much rested in this band").
             Summing rather than max is what a zoomed-out view means. A real
             wall is 10-100x the surrounding book and still stands out.

  AMPLITUDE  log-quantised to uint8:
                 HEAT_K = 255 / log1p(10_000_000)
                 code   = min(255, round(log1p(size) * HEAT_K))
                 decode via a precomputed 256-entry table
             Looks lossy, is not: the heat ramp has 256 entries, so anything
             finer cannot be displayed. Worst case 3.03% across 1..1e7.

Store bins DENSE over the occupied range (bin0 + arrays), not sparse. A book is
a contiguous ladder, so a sparse (index,value) pair spends 4 bytes on an index
to save nothing.

    MEASURED, 200 symbols, 6.5-hour session:
        naive     10.8 GB
        archive   38 MB   (188 kB/symbol)
        standing 2-minute wall : 89x the surrounding book
        same size flashed 1s   : 16x dimmer than the wall

DO NOT COMPRESS TOTALS. Per-slot volume and signed delta stay exact integers,
because the footprint and the monitor report the same session and must agree.
Only the per-bin SHAPE is quantised.

RENDERING: convert slots to objects that wear the same shape as a live Column
(same attribute names) so every existing renderer works unchanged. Do NOT teach
the renderer a second shape — the branch exercised less will rot. Add one flag
(`archived = True`) read with getattr so the live class needn't carry it.

CONVERSION MUST BE INCREMENTAL AND BUDGETED. A full conversion of two hours at
1-second aggregation measured 121 ms and would run every time a slot landed.
Build newest-first, at most ~50 slots per frame, walking backwards on later
frames: the part being looked at appears immediately and the rest fills in
behind it. Cache PER AGGREGATION — a single-entry cache thrashes when several
panes use different timeframes (measured 92 ms/frame vs 0.06 ms).

--------------------------------------------------------------------------------
12. SessionProfile (VOLUME + TPO)
--------------------------------------------------------------------------------

omnitrix/engine/profile.py

    buy/sell    dict[tick_index -> volume]
    tpo         dict[tick_index -> set(bracket_index)]
    brackets    set of bracket indices (30-minute buckets by default)
    total       total volume

Derived: POC, value area, and the TPO letter chart.

TWO THINGS THAT WENT WRONG HERE:

(a) It used its own split. See section 8. Route through split_size().

(b) It was fed ONLY from the live event loop, so a symbol whose session was
    backfilled showed a chart going back hours next to a profile that began
    when the application did. It needs an `add_bars()` path that ingests sealed
    bars' footprint arrays directly.

    VECTORISE add_bars(). Walking every price level in Python measured 194 ms
    for one 6.5-hour session, on the GUI thread, the moment history lands — and
    a 2x2 grid merges four. Group by TPO bracket, bincount over a zero-based
    index, then touch only the DISTINCT levels: 194.4 ms -> 10.3 ms.

    Take the split from the bar arrays; do not re-derive it. Re-deriving is how
    consumers came to disagree in the first place.

    Bars whose footprint was stripped by cold retention must add NOTHING. The
    profile must not invent volume for detail that is gone.

--------------------------------------------------------------------------------
13. SIGNALS
--------------------------------------------------------------------------------

omnitrix/engine/signals.py

    detect_blocks         outsized prints in the tape
    detect_absorption     heavy volume at a level that does not move price
    detect_wall_breaks    a large resting level that traded through
    detect_delta_divergence   price makes a new extreme, cumulative delta does not

EVERY DETECTOR MUST BE BOUNDED BY A WINDOW, not by session length:

    COLS_WINDOW    columns scanned
    TRADES_WINDOW  prints scanned
    lookback       bars scanned (divergence: 120)

Otherwise cost grows with uptime — the recurring failure in this codebase.

DIVERGENCE, specifically: compare SWING points, not consecutive bars. A single
quiet bar inside an advance is a quiet bar, not a divergence. A swing high is a
bar whose high is the highest of the `swing` bars either side. Use CUMULATIVE
delta, not per-bar: per-bar delta at a high says what that one bar did; the
running total says what the whole leg did, which is the thing that is supposed
to confirm the move.

Test that it stays QUIET on a healthy trend and on noise. An indicator that
fires often is not an indicator.

SLICING THE TAPE: use a real slice, not islice. The tape is a view over ring
arrays; islice walks from index 0 materialising a tuple for every entry it then
throws away. Measured 34 ms -> 77 ms on detect_all.

--------------------------------------------------------------------------------
14. RENDERING
--------------------------------------------------------------------------------

PyQt6 + pyqtgraph. Custom GraphicsObjects, not built-in plot items.

14.1 GENERAL RULES

  * Build pens, brushes and colours ONCE PER FRAME, not per bar and not per
    cell. Profiling a 150-bar view found 2,250 mkPen and 3,000 mkColor calls
    per frame — rebuilding identical objects cost as much as drawing.
  * Draw only what is in the viewport. Cull by x range first.
  * `prepareGeometryChange()` before any boundingRect() change, or Qt culls
    against the stale rect and the item flickers.
  * Antialiasing OFF for dense grids, ON for bubbles.

14.2 THE FOOTPRINT LAYOUT

Two styles, both supported, CLASSIC BLOCKS as the default:

  blocks     Two equal-width fields per row: sell left, buy right. Denser;
             every row the same width so columns line up down the whole bar.
  histogram  Each side scaled by its own volume, growing outward from a centred
             candle. Carries the shape of the auction without reading a digit.

THE CANDLE SITS AT THE COLUMN CENTRE and is drawn AFTER the cells (so it stays
legible over them). THEREFORE: cell labels must be kept clear of the candle
band. Placing them at the centre line puts them exactly under the body, and
because the candle paints last, the leading digits vanish. Rows read ".4K".

A width/fit check CANNOT catch this — the text genuinely fits its rect. It is
not clipping; it is two objects drawn in the same place. Test WHERE the label
lands relative to the candle band.

A label too wide for its own bar should fall just OUTSIDE the bar tip, into
space nothing else uses, rather than being dropped. Confining labels strictly
to their bars leaves most rows blank while the column sits empty. A label that
lands outside must switch to the chart text colour — on a POC row the on-cell
colour is near-black against a white cell, and drawing that on the dark chart
makes it invisible.

Give the candle half-width ONE accessor used by both the candle and the labels.
The bug was possible only because the candle computed it and the labels assumed
it.

14.3 THE HEAT FIELD

Map resting size to a colour ramp (256-entry LUT). Fade columns with
sweeps == 0 so a forward-filled region is visibly less certain.

14.4 PAINT SAFETY

Under PyQt6, an unhandled exception in a Qt-invoked callback reaches qFatal()
and ABORTS the process — but ONLY while sys.excepthook is the default one.
Install a hook before QApplication and the process survives.

    default hook, unguarded : exit 0xC0000409, no traceback anywhere
    custom hook installed   : survives 30 consecutive failing paints

But the hook then writes the full traceback EVERY FRAME, synchronously, on the
GUI thread. Wrap every paint in a guard that rate-limits (one line per site per
30 s) and blanks the widget:

    300 failing frames, hook only : 0.27 ms/frame, 114 kB of log
    with the guard                : 0.02 ms/frame, 0.6 kB

COUNT the faults per site and assert the count is zero in tests. Swallowing
exceptions is how bugs go quiet; the counter keeps them loud where loud is
useful.

--------------------------------------------------------------------------------
15. THE FRAME GOVERNOR AND WATCHDOG
--------------------------------------------------------------------------------

omnitrix/ui/framegov.py

15.1 THE PROBLEM

Qt serves EVERY window from ONE GUI thread. If each window picks its own timer
interval as though it were alone:

    main chart   26 ms of work every 33 ms = 79% of the thread
    4 bookmaps   11 ms of work every 80 ms = 55%
                                             ----
                                             134%  on a thread that has 100%

Qt resolves the overdraft by firing timers late, and the cost lands wherever it
happens to land. Measured: the main chart fell from 19.8 fps alone to 6.2 fps
with four bookmaps open, gaps up to 206 ms.

15.2 THE SOLUTION

Windows declare a PRIORITY, not a rate. The governor measures what each
actually costs (refresh AND paint — they are measured in different places; the
callback is 0.3 ms and the paint is 26 ms) and returns an interval that keeps
total demand under target.

    TARGET      ~68% of one thread. Measured: this app saturates at ~680 ms of
                work per second, the rest going to Qt's event handling, layout,
                styling, GC and the feed thread's GIL slices. Budgeting at 100%
                is budgeting for a thread you do not have.
    LATE_HIGH   1.40 — an unloaded single chart sits at ~1.18 and a genuinely
                oversubscribed desk at ~2.1, so the threshold goes between them.
    MAX_SCALE   6.0
    MAX_INTERVAL_MS 250 — an unfocused window still updates 4x/second.

Clamp a single pathological lateness sample (GC pause, resize, machine sleeping)
so one outlier cannot slam every window to the floor.

Decay the scale with NO dead band on the recovery side. An earlier version only
decayed below a second, lower threshold, and startup — where the prefill
genuinely does run late — drove the scale up and left it stuck there forever.

PAINT COST ACCUMULATES WITHIN A FRAME. A window can own several plot widgets
(a 4-chart grid), all painting in one frame under one key. Folding each report
straight into the EMA AVERAGES them instead of adding them — measured, a
4-chart grid reported 29% demand while a single chart reported 80% for the same
real work.

15.3 THE WATCHDOG

Bracket every governed frame; attribute time to named sections:

    SLOW FRAME 149 ms (window ...) - drain 148ms  redraw 1ms

Every stall in this project has been the same shape (unbounded work on the
frame thread) and every one cost hours to find, because the app recorded the
symptom and nothing about the cause.

INSTRUMENT EVERY TIMER. Five windows once ran governed frames with no watch
section, so their stalls reported "unmarked" — including the bookmap, the most
expensive thing the app draws. Add a scan that fails if any GovernedTimer lacks
one.

15.4 WITHIN A WINDOW

Refresh the FOCUSED pane every frame and the others in turn (round-robin).
Refreshing is what marks items dirty, so a skipped pane is one Qt does not
repaint: four paints per frame become two. Measured, with 4 charts + 4 books at
200 symbols: governor demand 2.0x budget -> ~1.0x.

Panes that just changed symbol/timeframe/mode redraw immediately regardless.

--------------------------------------------------------------------------------
16. HOT/COLD RETENTION
--------------------------------------------------------------------------------

Only symbols something is DRAWING keep full detail. Everything else keeps a
reduced window.

    HOT   BOOK_BARS 1500 bars with L2, full column ring, full tape
    COLD  COLD_BARS 30 bars of footprint, COLD_COLS 150 columns, TAPE_COLD 8192

Measured at 100 symbols and 100 depth/side: per-column state reached 116 MB in
six minutes and was climbing toward ~440 MB at the cap. Per-bar state at the
12,000-bar cap is 42 MB/symbol = 4.2 GB across 100.

RULES

  * START HOT; only the window demotes. A buffer constructed bare must retain
    everything a consumer expects. Starting cold was tried and the data-truth
    gate caught it immediately: a bare buffer retained less than the BarSeries
    beside it, so the two disagreed about the same session's volume.

  * DEMOTION IS DESTRUCTIVE AND IS NOT UNDONE BY PROMOTION. A symbol brought
    back has full detail from that moment and stripped bars behind it.

  * THEREFORE: a grace period (300 s) before demoting a symbol that has been on
    screen. Glancing at another ticker for ten seconds must not permanently
    strip the one you came back to. Symbols never displayed demote immediately
    — otherwise every symbol in the basket is held at full retention for the
    first five minutes of every session (measured 2.3 GB and a 325 ms bookmap
    p95 at 100 symbols x 400 depth).

  * BUDGET THE WORK, NOT THE CALLS. A symbol registered a moment ago has
    nothing to release, so demoting it is free; counting it left a 1,000-symbol
    backlog draining six per pass while the free ones ahead used every slot.

  * BUDGET IN THE RIGHT UNIT. Demotion evicts columns and each eviction folds
    one into the archive (~30 us). A budget counting SYMBOLS cannot see work
    that is per COLUMN: six symbols x 1,250 columns = 282 ms in one frame.
    Cap columns per pass (300 ~ 9 ms) and let the rest drain on later passes.

--------------------------------------------------------------------------------
17. HISTORY AND BACKFILL
--------------------------------------------------------------------------------

Two entry points, one mechanism.

    Startup backfill    on open, for the symbols on screen
    Session history     when a symbol is first DISPLAYED (hook this into
                        whatever already computes the "hot" set — it knows the
                        exact moment)

Both fetch on a worker thread and build a complete BarSeries + BookmapBuffer
OFF the GUI thread. Ingestion is ~8 us/trade, so a 6.5-hour symbol at 60
prints/sec is 11.4 SECONDS of Python — that cannot happen on the GUI thread.

17.1 THE MERGE — TAKE ONLY WHAT IS STRICTLY OLDER

    BarSeries.prepend_history(other)        bars    older than the oldest live bar
    BookmapBuffer.prepend_columns(other)    columns older than the oldest live column
    SessionProfile.add_bars(...)            the profile gets it too

This is the whole design. Replay and live can NEVER describe the same bucket,
so nothing is double-counted — and that means THE LIVE STREAM NEVER HAS TO STOP.

An earlier design held the event queue (up to 45 s) so the replayed series could
be installed into an empty slot. The window kept repainting, so nothing looked
crashed — it simply showed no new data for up to three quarters of a minute.
That hold WAS the freeze users reported.

DO NOT MERGE THE BOUNDARY BAR. The replay holds only the part that arrived
before the client connected; adding that to a bar the live stream is still
filling produces a bar that is neither. One bar of lost detail at the seam is
the honest price.

BOUND THE MERGE TO WHAT IS KEPT. A full-day replay can carry more bars and
columns than the caps allow. Splicing them in only to evict them is frame-thread
work for a result nobody can see — and every evicted column runs an archive fold.
Take the NEWEST of the older data.

17.2 DO NOT INGEST BOOKS YOU WILL DISCARD

THE BIGGEST SINGLE PERFORMANCE BUG IN THIS PROJECT.

The buffer buckets sweeps into one-second columns and a later sweep REPLACES an
earlier one in the same column. A live feed can carry ~800 sweeps/second. So
799 of every 800 snapshots had a full PriceLadder built and thrown away —
on a worker thread, holding the GIL.

    23 minutes at 800 sweeps/s:
        every snapshot   36.29 s    1,380 columns kept
        one per column    0.51 s    1,380 columns kept
                          71x       IDENTICAL OUTPUT

Deduplicate by column bucket BEFORE ingesting, keeping the last. Preserve the
collapsed count into `sweeps` so the column still reports honestly.

The user-visible symptom was: "it works until the previous data loads, then it
just gets stuck, nothing updates." The GUI could not get enough of the
interpreter to draw for half a minute.

17.3 CONCURRENCY

Cap in-flight fetches (2) and queue the rest. One symbol is ~7.7 MB in ~4 s;
a 2x2 chart grid plus a 2x2 book grid binds eight symbols in the same instant
on a workspace restore. Drop a queued symbol that has since left the screen.

Make sure the two entry points cannot fetch the SAME symbol simultaneously —
two sockets, two full-day transfers, two model builds, both holding the GIL.

--------------------------------------------------------------------------------
18. FAILURE HANDLING
--------------------------------------------------------------------------------

18.1 VALIDATE AT THE INGESTION BOUNDARY

    reject: price NaN, <= 0, or > 1e9  (a tick index must fit int32)
            size < 0 or > 2^40
            empty symbol

A price of 1e12 overflows the int32 tick index and raises inside seal(), after
which EVERY later paint of that bar fails. NaN does not raise at all — it
propagates into high/low and makes the price axis unusable with nothing on
screen to say why.

COUNT the rejections and surface the count. Never discard data silently.

18.2 FEED GAPS

Detect by sequence number. Queue the exact range for TCP repair (bounded queue;
past the bound, drop book state and let the next sweep rebuild).

On a gap: clear the cumulative-volume table (section 2.2a) and discard
half-assembled books (section 6.3).

Raise the UDP receive buffer — the default is tens of kilobytes and a burst
that overflows it is dropped by the OS before Python sees it. 8 MB is ~5
seconds of a full feed.

18.3 A DEAD WORKER THREAD IS INVISIBLE

The worst bug in this project's history: the receive thread died on the first
gap and the app looked perfectly healthy.

    health: 21.2 fps | queue 0 | dropped 0 | state=done | syms 96

Twenty-one frames a second, empty queue, nothing dropped — and no data. Four
rounds of diagnosis went to the chart, the loader and the frame budget.

MITIGATIONS:
  * Install a threading.excepthook that LOGS. A worker that dies silently is a
    feed that stops with no explanation.
  * Log a periodic health line: fps, queue depth, dropped, feed lost/repaired,
    loaders in flight, state, symbol count. One line separates the four failure
    modes that otherwise look identical from outside.
  * Consider restarting a dead receive thread, or at minimum surfacing
    "receiver stopped" in the UI.

18.4 SHUTDOWN

`cancel()` on a worker is a REQUEST, not a stop. A thread blocked in
socket.create_connection() will not notice. Hold references, disconnect
signals, and WAIT in closeEvent — a QThread destroyed while running takes the
process down (observed as exit 127 with every check passing).

--------------------------------------------------------------------------------
19. DATA-TRUTH RULES (READ THIS)
--------------------------------------------------------------------------------

These are non-negotiable. Each was written after a bug where the application
displayed something FALSE without raising an error.

  1. ONE DEFINITION OF THE SPLIT. Section 8. Enforce mechanically.

  2. MEASURED VS RECONSTRUCTED. `sweeps` counts real observations. A
     forward-filled column reports 0. A replayed column never overwrites a
     measured one. Archive columns are flagged. An absence may be drawn as
     absent; it may NEVER be drawn as data.

  3. WHOLE-BAR-OR-NOTHING. A bar gets its entire footprint or none of it.
     Splitting a fill across two frames shows a partial footprint beside a
     candle stating a different number.

  4. REFUSE RATHER THAN MISLEAD. A partial load is discarded, not shown. A
     series holding the first forty minutes of a session, with totals to match,
     reads as authoritative and is wrong about every figure a trader checks.

  5. NEVER FABRICATE ACROSS A GAP. Section 2.2a.

  6. TOTALS ARE EXACT. Compression may touch shape, never volume or delta —
     multiple views report the same session and must agree.

  7. SHOW THE UNCERTAINTY. Publish the attribution share, the dropped count,
     the lost/repaired counts, and whether history loaded.

--------------------------------------------------------------------------------
20. DEPENDENCIES AND ENVIRONMENT
--------------------------------------------------------------------------------

    Python      3.14.5 (AMD64)
    PyQt6       6.10.2  (Qt 6.10.0)
    pyqtgraph   0.14.0
    numpy       2.4.6
    psutil      7.2.2      (optional; test-time memory reporting)
    pywin32                (server side, named pipes)
    MSVC                   (builds the DLL; .vcxproj provided)
    Takion SDK             (vendor headers; see section 25)

The client has NO third-party dependency beyond PyQt6, pyqtgraph and numpy.
Wire framing, decoding, recording and replay are all first-party — deliberately,
because the format is dictated by the DLL and a library would only add a
translation layer that can disagree.

ENVIRONMENT TRAP (this machine): `python` on the bash PATH resolves to a 3.11
venv with no PyQt6. Use `py`.

--------------------------------------------------------------------------------
21. BUILD AND RUN
--------------------------------------------------------------------------------

    # DLL
    cd Scraper_Takion_Omnitrix
    build.bat            # MSVC -> TakionAdditionalColumns.dll
    deploy.bat           # copy into the Takion install

    # Server
    cd Host_Omnitrix
    py broadcaster_server.py

    # Client from source
    cd Footprint_Omnitrix
    py -m omnitrix.app                      # simulated data, 3 symbols
    py -m omnitrix.app --multicast          # live
    py -m omnitrix.app --replay FILE --speed 4
    py -m omnitrix.app --symbols A,B,C

    # Client packaged
    Host_Omnitrix\build_client.bat          # mirrors the package, then builds

MIRROR RULE: Host_Omnitrix/client_app/omnitrix must be a zero-divergence copy
of Footprint_Omnitrix/omnitrix. build_client.bat mirrors with robocopy /MIR, so
any file existing only in the copy is DELETED. Never edit the copy.

LOGS: ~/.omnitrix_logs/omnitrix-<date>-<time>.log, new file per run, 5 kept,
plus a .fatal companion written by faulthandler for hard crashes (a segfault or
qFatal leaves no Python traceback — this is the only artifact).

STARTUP ON SIMULATED DATA IS SLOW BY DESIGN: the synthetic feed generates 90
minutes of history per symbol first. Measured 5 s at 3 symbols, 71 s at 100,
187 s at 200 — linear. Live startup does not do this.

--------------------------------------------------------------------------------
22. TESTING PHILOSOPHY AND THE GATES
--------------------------------------------------------------------------------

54 client tests, 8 server tests. Each is a standalone script printing PASS/FAIL
and exiting non-zero. No framework — a test is `py tests/name.py`.

FOUR GATES (`py tests/run_gates.py`):

    gate_truth    Folds, deltas, POC and value area exact and order-independent.
                  One definition of the split, enforced statically AND
                  behaviourally across three consumers.
    gate_perf     No periodic consumer grows with session length. Frame budgets
                  hold. Memory plateaus.
    gate_frames   The governor keeps total demand bounded.
    gate_gc       The engine creates NO reference cycles, so the gen-2 threshold
                  can be raised without holding memory.

PRINCIPLES LEARNED THE HARD WAY

  * TEST THE PROPERTY, NOT THE MECHANISM. Several tests had to be rewritten
    when a mechanism changed; their purpose survived because they asserted what
    the user gets, not how.

  * ASSERT RATES, NOT COUNTS. A frame-count threshold punished a fetch for
    getting faster — twice. ("30 frames at 76 fps" failed "more than 40
    frames" while being the best result yet.)

  * A CHECK THAT WOULD NOT HAVE CAUGHT ITS OWN BUG IS THEATRE. Verify a new
    detector against the actual historical source before trusting it.

  * MEASURE THE REAL PATH. A test that called the tick function directly
    bypassed the watchdog's bracket and reported a 10 ms worst frame while the
    drain, fold and paint were all happening where nothing was watching.

  * WATCH FOR VACUOUS PASSES. "0 added from 0 blank bars" passed because the
    fixture produced no blank bars. Assert the fixture produced the condition.

  * NEVER WRITE THE OPERATOR'S REAL WORKSPACE. `def save(win, path=PATH)` binds
    the default at import, so reassigning the module's PATH is not enough — the
    FUNCTIONS must be replaced. Add a scanner that fails if any test builds a
    window without neutering them.

  * BEWARE THE HARNESS BEING THE BOTTLENECK. A synthetic feed's book generator
    cost 91.3% of a core at depth 400; every "stall" measured in that
    configuration was the harness, not the app. Pre-build events where possible.

--------------------------------------------------------------------------------
23. COMPLETE CONSTANT REFERENCE
--------------------------------------------------------------------------------

    RETENTION
      BOOK_BARS               1500     bars keeping their L2 snapshot (~4 h)
      COLD_BARS                 30     footprint kept by an undrawn symbol
      COLD_COLS                150     heat columns kept by an undrawn symbol
      TAPE_SEED               2048     tape ring initial allocation
      TAPE_COLD               8192     tape ceiling for an undrawn symbol
      HISTORY_COLS           14400     deep ring for a Bookmap window (4 h)
      max_bars               12000     hard cap on base bars per symbol
      DEMOTE_GRACE_S           300     grace before a symbol loses detail

    FRAME / DRAIN
      EVENT_QUEUE_MAX        60000     feed -> GUI queue depth
      DRAIN_BUDGET_S         0.008     normal per-frame drain budget
      DRAIN_BUDGET_BUSY_S    0.022     budget when backlog >= DRAIN_BUSY_AT
      DRAIN_BUSY_AT           2000     backlog that switches budgets
      clock check every          8     events (was 32, was 256)
      MAX_DEMOTIONS_PER_SYNC     6     symbols per demotion pass
      MAX_FOLD_COLS_PER_SYNC   300     columns per demotion pass (~9 ms)
      TARGET                  ~0.68    fraction of one GUI thread
      LATE_HIGH               1.40
      MAX_SCALE                6.0
      MAX_INTERVAL_MS          250

    HISTORY
      BACKFILL_SESSION_H       7.0     hours of L1 fetched
      BACKFILL_L2_MIN         23.0     minutes of L2 fetched
      MAX_SESSION_FETCHES        2     concurrent history loads
      TIMEOUT_S                8.0     replay socket timeout

    ARCHIVE
      ARCH_COL_S              30.0     seconds per archive slot
      ARCH_TICK_STEP             4     ticks per archive bin
      ARCH_MAX_SLOTS          1200     ~10 hours
      HEAT_MAX             1.0e7       top of the log quantiser range

    FEED
      RCVBUF                 8 MB      UDP receive buffer (~5 s of feed)
      REPAIR_DELAY_S          0.35     batching window for gap repairs
      MAX_PENDING_REPAIRS       64     outstanding ranges before dropping state
      L2_RING_BUFFER_SIZE  1048576     DLL-side L2 ring (32 MB)
      RING_BUFFER_SIZE        4096     DLL-side L1 ring

    FOOTPRINT
      base_tf_s                 10     seconds per base bar
      imbalance factor         3.0
      min_imbalance_vol         20
      va_pct                  0.70     value area
      CANDLE_HW              0.055     candle half-width with cells
      CANDLE_HW_PLAIN         0.30     candle half-width without cells
      BOX_W                   0.66     column block width, x-units
      LABEL_PAD_PX               4

--------------------------------------------------------------------------------
24. BUG ARCHIVE — MISTAKES THAT SHAPED THE DESIGN
--------------------------------------------------------------------------------

Each is a CLASS of mistake. Read them before writing the equivalent code.

24.1 A SILENT NAME COLLISION KILLED THE FEED (the worst one)

    MulticastFeed subclasses TakionDecoder. The decoder owns `_pending`: trades
    held until the clock offset locks, as (Trade, ts) PAIRS. The feed declared
    its own `_pending` for gap ranges — (channel, from, to) TRIPLES — silently
    replacing the parent's attribute.

    Nothing was wrong until the FIRST GAP. Then a triple landed in the list the
    decoder unpacks as pairs:

        ValueError: too many values to unpack (expected 2, got 3)

    raised on the receive thread, which STOPPED and was never restarted. The
    terminal stayed at 21 fps with an empty queue and received nothing more.

    LESSON: Python never complains when a subclass reuses an attribute name,
    and nothing surfaces until the two shapes meet. Test that no subclass
    replaces a parent attribute with a different type.

24.2 REBUILDING A MILLION BOOKS THAT WERE THEN DISCARDED

    Section 17.2. 36.29 s -> 0.51 s, identical output.
    LESSON: before ingesting a stream, ask what the sink actually retains.

24.3 THE HOLD THAT WAS THE FREEZE

    Section 17.1. The app deliberately stopped draining for up to 45 s.
    LESSON: "the UI is repainting" is not the same as "the app is working".

24.4 A BUDGET THAT COUNTED THE WRONG THING

    set_hot(False) stripped up to 1,500 bars then fell off the end of the
    function returning None. The budget counts what set_hot reports, so the most
    expensive operation scored as FREE.
        600 symbols x 900 bars: 183.1 ms (599 demotions) -> 1.8 ms (6)
    LESSON: a function whose return value is a budget input must always return.

24.5 TWO OBJECTS DRAWN IN THE SAME PLACE

    Section 14.2. A fit check cannot catch it; the text fitted its rect.
    LESSON: when A is drawn after B, test WHERE they land, not whether each fits.

24.6 sys.getsizeof DOES NOT FOLLOW A DICT'S VALUES

    A cache was reported at 184 B/bar and measured with a deep sizer at
    7,098 B — 68% of a sealed bar, nearly 4x the L2 book. Across 1,000 symbols
    at the bar cap: 85 GB.
    LESSON: write a recursive sizer before believing any memory number.

24.7 FOLLOW MODE CHOSE THE ZOOM

    The redraw snapped x to a fixed 22 bars every frame. Zooming in near the
    live edge leaves follow ON (the right edge is still at the newest bar), so
    the next frame threw the zoom away — ~30 times a second.
    LESSON: "follow" means keep the newest bar in view. It does not mean choose
    the width.

24.8 A TOGGLE THAT MOVED ONE PANE OF FOUR

    Overlay switches acted on the ACTIVE pane's item; the other three kept
    whatever they were constructed with (visible, for a PlotDataItem). Worse,
    the startup default was never APPLIED — the checkbox state was set before
    the signal was connected, so nothing fired.
    LESSON: a default must be applied, not merely stored. And a multi-pane app
    needs one function that pushes state to ALL panes.

24.9 UNVALIDATED INPUT REACHED THE STORAGE LAYER

    Section 18.1. astype(int32) WRAPS rather than raising, inventing walls.
    LESSON: validate at the boundary; a wrong value is worse than a missing one.

24.10 WORK ON THE GUI THREAD THE MOMENT DATA LANDS

    profile.add_bars: 194.4 ms -> 10.3 ms.
    cold timeframe fold: 35.4 ms -> 12.8 ms.
    symbol registration inside the drain: 149 ms -> 47 ms.
    opening a bookmap built all 4 panes: 192.7 ms -> 80.3 ms.
    LESSON: profile the FIRST frame after any bulk arrival, not the steady state.

--------------------------------------------------------------------------------
25. LEGAL AND LICENSING CONSTRAINTS
--------------------------------------------------------------------------------

  * The Takion SDK headers and libraries are VENDOR-LICENSED and are not
    redistributable. They exist under Scraper_Takion_Omnitrix/sdk/ for building
    only. Takion support explicitly authorised use of the files under C:\Takion
    and the extracted reference material for this integration.

  * Bookmap is a licensed commercial product whose EULA prohibits reverse
    engineering. No Bookmap code, assets or branding is used. The heat-ramp
    values were sampled from a screen capture as MEASUREMENTS — recorded
    observations, not copied assets.

  * THERE IS NO ORDER-ROUTING PATH. The DOM ladder displays depth. Nothing in
    this system places, modifies or cancels an order. Adding one is a different
    project with different obligations.

--------------------------------------------------------------------------------
26. IF YOU REBUILD THIS, DO THESE THINGS DIFFERENTLY
--------------------------------------------------------------------------------

Honest retrospective. These are not defects to fix in this codebase so much as
decisions worth revisiting from a blank sheet.

  1. PUT THE INGESTION LOOP IN C, NOT PYTHON. Almost every performance crisis
     in this project traces to the GIL: a worker holding it starves the GUI.
     ~8 us/trade in Python is the root cause of the 11-second model build, the
     36-second book ingest, and the general fragility under load. A small C
     extension (or Cython) for decode + BarSeries/BookmapBuffer ingestion would
     have removed the entire class.

  2. SEPARATE THE PROCESS, NOT JUST THE THREAD. A data process feeding a UI
     process over shared memory sidesteps the GIL entirely and makes a dead
     ingest visible immediately (the process is gone) rather than invisible
     (a thread quietly stopped).

  3. RESTART DEAD WORKER THREADS, AND SURFACE IT. See 24.1. Any long-lived
     thread should have a supervisor that logs, restarts with backoff, and puts
     "receiver stopped" on screen.

  4. ADD THE HEALTH HEARTBEAT ON DAY ONE. One log line — fps, queue, dropped,
     feed lost/repaired, loaders, state — separates four failure modes that are
     indistinguishable from outside. It was added after four rounds of failed
     diagnosis and identified the real bug immediately.

  5. NAMESPACE SUBCLASS STATE. Prefix a subclass's private attributes, or use
     composition instead of inheritance for the feed hierarchy. See 24.1.

  6. STORE TIMESTAMPS IN THE INDEX after all, or at least a coarse one. The
     scan-forward design is correct but cost real debugging time; a (seq,
     offset, coarse_ts) triple would have been cheap.

  7. DECIDE THE AGGREGATION STRATEGY UP FRONT. The fold was written naively,
     then cached, then made incremental, then vectorised. Building it
     array-first from the start would have avoided three rewrites.

  8. WRITE THE FAULT-INJECTION TEST EARLY. It found three real bugs on its
     first run. Hostile prices, a dead server, a tick change, rapid clicking,
     an unknown symbol, a stopped feed — all cheap to simulate and all things
     that happen.

--------------------------------------------------------------------------------
END
--------------------------------------------------------------------------------

Every number in this document is a measurement taken from this codebase or a
benchmark run against it. None is an estimate.
