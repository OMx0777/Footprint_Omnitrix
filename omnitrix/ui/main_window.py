"""Main ATAS-style window: footprint price pane + cumulative-delta pane,
fed by any engine Feed via a thread-safe queue drained on the GUI thread.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import replace

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QEvent
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QMainWindow, QToolBar, QLabel, QComboBox, QCheckBox, QPushButton, QWidget,
    QSizePolicy, QDockWidget, QLineEdit, QGraphicsRectItem, QMenu,
    QToolButton, QWidgetAction, QHBoxLayout, QSplitter, QVBoxLayout,
)

from .framegov import (GOVERNOR, GovernedTimer, GovernedPlotWidget,
                       watch, WATCHDOG)
from .alert_ui import AlertToast, SOUNDER
from ..engine.alerts import AlertBook, CROSS, ABOVE, BELOW
from ..engine.history import HistoryFetcher, StartupFetcher
from .chart_pane import ChartPane
from ..engine import (
    Instruments, BarSeries, BookmapBuffer, SessionProfile, Feed,
)
from ..engine.model import Trade, BookSnapshot, sane_trade
from ..render import (
    FootprintItem, HeatmapItem, DARK, LIGHT, TimeAxis, PriceAxis, Crosshair,
    FibRetracement, PositionDrawer, FixedVolumeProfile, PenDrawing,
    CprDrawing, PriceLevel, MeasureTool, EMAItem, CPRItem,
    ExecutionMarkersItem
)
from .settings_dialog import SettingsDialog
from .bookmap_window import BookmapWindow
from .tape_window import TapeWindow
from .profile_window import ProfileWindow
from .analytics_window import AnalyticsWindow
from .monitor_window import MarketMonitorWindow
from .dom_ladder import DomLadderWindow
from . import workspace
from . import design
from .tape_widget import TapeWidget
from .stats_panel import StatsPanel
from .signals_panel import SignalsPanel

log = logging.getLogger(__name__)

# Roughly 15 s of a very busy 100-symbol basket. Beyond this the GUI is not
# keeping up and holding more events only makes the lag worse.
# Backlog ceiling, in events. A queued BookSnapshot is ~22.7 kB (two ~128-level
# dicts), so this is really a memory budget: 120,000 events is ~2.7 GB worst
# case if every one is a book, versus 13.6 GB at the 600,000 it used to be.
#
# Raised from 40,000 after live 100-symbol sessions reported "dropped 14,817",
# then trimmed to 60,000 once the drain was fixed (4,467 -> 19,138 events/sec).
#
# Sized for the deployment target: a 16 GB machine that is ALSO running Takion.
# After Windows, Takion and a no-swap margin, Omnitrix has roughly 8 GB, and a
# safety valve that can itself consume 2.7 GB of that (120,000 books) is not a
# safety valve - it is the thing that pushes the box into swap, which is the
# failure it exists to prevent. 60,000 caps the worst case at ~1.4 GB and is
# still ~30 seconds of burst at the measured arrival rate against a drain that
# clears 19,000 events/sec.
EVENT_QUEUE_MAX = 60_000

# Wall-clock budget for one drain pass, in seconds. A COUNT cap cannot bound
# time: at 75 us/event the old 40,000-event cap allowed a single frame to block
# for 3.0 s, and the GUI thread is the only thread that draws.
#
# Adaptive, because one fixed budget cannot serve both cases. 8 ms of a 33 ms
# frame keeps the UI liquid when the feed is calm, but it caps throughput at
# roughly 8/33 of what the machine could do — and when a burst arrives that
# ceiling is precisely what turns a backlog into dropped data. So: spend the
# small budget normally, and escalate to the large one while a backlog exists.
# A late frame is recoverable; a dropped print is not.
DRAIN_BUDGET_S = 0.008
DRAIN_BUDGET_BUSY_S = 0.022
DRAIN_BUSY_AT = 2_000            # backlog that switches to the busy budget

# How many symbols may be demoted to reduced retention in one pass. Each costs
# a tape reallocation and a column eviction - measured at 626 us, so a
# unbounded pass over a large universe would itself drop the frame it exists to
# protect. The queue drains over the following seconds and a symbol waiting its
# turn is only holding memory it already held.
MAX_DEMOTIONS_PER_SYNC = 6
# ...and a second, harder budget in COLUMNS. Demotion evicts columns, and every
# eviction folds one into the session archive at a measured 38 us, so the real
# cost of a pass is the number of COLUMNS released and not the number of
# symbols. 300 columns is about 11 ms of an 80 ms frame; the six-symbol limit
# above still applies, whichever binds first.
MAX_FOLD_COLS_PER_SYNC = 300
# How close (screen px) the cursor must already be to a bar's open/high/low/
# close before the magnet takes the point. Small enough that a deliberate
# placement in open space is never moved, large enough to catch the near-miss
# that magnet mode exists for.
MAGNET_PX_DEFAULT = 12.0

# Deep scroll-back. A pan emits a range change per mouse move; waiting this
# long after the last one turns a drag into ONE request instead of forty.
HISTORY_DEBOUNCE_MS = 200
# Never ask for more than this in one go. The old backfill had no bound and
# replayed a whole session - 1.3 GB of L2 - which is what froze the terminal.
#
# Four hours, not one. One hour truncated the window on any view wider than
# that - a 1-minute chart showing 100 bars is 100 minutes - so the bars beyond
# it stayed blank however long you waited, which is "it does not show full
# footprint candles". Affordable now only because the fold is spread across
# frames (see _fold_pending); the reply is still capped by MAX_REPLY_BYTES.
HISTORY_MAX_SPAN_MS = 4 * 60 * 60 * 1000
# How much of one frame the fold may take. The measured cost of folding a
# single scroll-back was 484 ms - 431 for 135,000 trades and 53 for the heat -
# in ONE frame, against a 33 ms budget. That is the lag: not a slow leak, a
# half-second stall every time you scroll into cold history.
FOLD_BUDGET_S = 0.008

# How long a symbol must be OFF SCREEN before its detail is released.
#
# Demotion is destructive and promotion does not undo it, so demoting the
# instant a symbol leaves the screen means glancing at another ticker for ten
# seconds permanently destroys the footprint and the heat field behind the
# cold window on the one you came back to. That is what "footprint only for
# the last fifteen minutes after an hour of watching" was: not a fetch that
# failed, an eviction that fired on ordinary use.
#
# Five minutes of grace costs a few hundred kB per recently-viewed symbol and
# makes switching between a handful of names free, which is how the app is
# actually used.
DEMOTE_GRACE_S = 300.0

# ---- startup backfill -------------------------------------------------------
# Live events are HELD while the day's history loads, so the two meet at the
# sequence seam instead of interleaving. Two caps decide when to give up.
#
# The event cap is deliberately well below EVENT_QUEUE_MAX. _event_q is a
# deque(maxlen=60_000) and a full deque DROPS FROM THE LEFT - silently, and
# from exactly the end the seam depends on. Aborting at 40,000 means the queue
# never wraps, so the choice is always between a complete load and an honest
# refusal, never a quiet hole.
BACKFILL_HOLD_MAX_EVENTS = 40_000
# And a wall-clock bound, because a server that never answers must not hold the
# chart forever. A 6.5 h single-symbol load measured ~12 s of ingest.
BACKFILL_HOLD_MAX_S = 45.0
# Whether the live queue is HELD while history loads. Off: the hold is what a
# user experiences as a freeze, and prepend_history made it unnecessary. Kept
# as a switch rather than deleted because the hold is the only thing that makes
# a seam exact, and if a future loader needs that again this is where it lives.
HOLD_FOR_BACKFILL = False
# How far back a startup load reaches. L1 covers the session because the bars,
# the footprints and the volume profile all come from it; L2 is capped to what
# the 1400-column ring can physically hold, because fetching a full day of
# depth moves 934 MB so the buffer can discard 94% of it.
BACKFILL_SESSION_H = 7.0
BACKFILL_L2_MIN = 23.0
# How many session fetches may be in flight at once. Measured on the operator's
# server: one symbol is 7.7 MB in 3.95 s, and eight panes bind in the same
# instant on a workspace restore. Two at a time keeps the server responsive and
# the GIL available to the live decode; the queue drains behind it.
MAX_SESSION_FETCHES = 2
# How long to wait for the multicast seam before giving up on history. The join
# buffers for a second before publishing first_live_seq, so this only has to
# cover a slow start - not a slow load, which has its own cap.
BACKFILL_SEAM_WAIT_S = 12.0

TF_CHOICES = {
    "5s": 5, "10s": 10, "15s": 15, "30s": 30,
    "1m": 60, "2m": 120, "3m": 180, "5m": 300,
    "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}

# mode -> (footprint mode, draw footprint cells, heatmap visible)
MODES = {
    "Footprint": ("Footprint", True, False),
    "Cluster": ("Cluster", True, False),
    "Profile": ("Profile", True, False),
    "Delta": ("Delta", True, False),
    "Heatmap": ("Footprint", False, True),
    "Footprint + Heatmap": ("Footprint", True, True),
    "Cluster + Heatmap": ("Cluster", True, True),
}

# Footprint price aggregation, as a PRICE not a tick count, so "10c" means 10c
# whatever the instrument's tick is. 0.0 = Auto (chosen from the zoom).
# Chart grid layouts: label -> (panes, rows, cols). Four is the ceiling on a
# 1920x1080 screen - a fifth chart is 640 px wide and the footprint numbers
# stop being legible, which is worse than not showing it.
LAYOUTS = {"1 chart": (1, 1, 1), "2 charts": (2, 1, 2), "4 charts": (4, 2, 2)}
MAX_PANES = 4

PRICE_STEPS = {
    "Auto": 0.0,
    "1¢": 0.01, "5¢": 0.05, "10¢": 0.10, "25¢": 0.25, "50¢": 0.50,
    "$1": 1.00, "$2": 2.00, "$5": 5.00,
}


class OmnitrixWindow(QMainWindow):
    def __init__(self, feed: Feed, instruments: Instruments | None = None):
        super().__init__()
        self.setWindowTitle("Omnitrix — Order Flow Terminal")
        self.resize(1680, 940)

        self.instruments = instruments or Instruments(default_tick=0.01)
        self.feed = feed
        self.series: dict[str, BarSeries] = {}
        self.bookmaps: dict[str, BookmapBuffer] = {}
        self.profiles: dict[str, SessionProfile] = {}
        self._child_windows: dict = {}        # key -> live child window
        self.latest_book: dict[str, BookSnapshot] = {}
        # Your fills per symbol, oldest first. Bounded: a marker you can no
        # longer scroll to is a marker nobody will ever look at.
        self.executions: dict[str, list] = {}
        # Set before _build_ui: _bind_pane reads it to decide whether to draw
        # the active-pane border at all.
        self._n_panes = 1
        self._pending_active_symbol = ""
        self.theme = DARK
        # auto_scroll / _auto_y / _needs_center are PER PANE (see the
        # properties below) - each chart follows its own symbol independently.
        self._dirty = False
        self._known_symbols: set[str] = set()
        # Price alerts. Checked in the drain, for EVERY symbol - the point of
        # the feature is being told about a level on a name you are not
        # currently looking at.
        self.alerts = AlertBook()
        self._alert_pending: list = []

        # thread-safe hand-off: feed thread appends, GUI timer drains.
        # ONE queue keeps trades and books in their true time order, so a book
        # always routes to a bar the preceding trades already created.
        #
        # BOUNDED on purpose. Live mode accepts every symbol the DLL sends (a
        # 100-name basket at snapshot rate), and an unbounded deque under a slow
        # drain grows without limit: RAM climbs, latency climbs, and the chart
        # silently falls further behind real time with nothing to show for it.
        # A maxlen sheds the oldest events instead and reports the loss.
        #
        # The bound is in EVENTS but the memory is not: a BookSnapshot carries
        # two ~128-entry dicts and measures ~22.7 kB, against a few hundred
        # bytes for a Trade. At the old 600,000-event ceiling a full queue was
        # 13.6 GB - the process would die of the safety valve long before the
        # valve opened. Sized so that a FULL queue is bounded in bytes, not just
        # in count; see EVENT_QUEUE_MAX.
        self._event_q: deque = deque(maxlen=EVENT_QUEUE_MAX)
        self._dropped = 0

        # Custom footprint painting is viewport-culled, so GL buys nothing and
        # only adds driver-dependent bugs. Software raster is crisp + portable.
        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_ui()
        self._apply_theme()

        feed.on_trade(self._enqueue)
        feed.on_book(self._enqueue)
        feed.on_execution(self._on_execution)

        self._pending_symbol = ""
        workspace.restore(self)               # reapply the last saved desk

        # Priority 0: this is the chart being traded from. When the shared
        # budget is tight, every other window gives way to this one.
        # Deep scroll-back. Debounced rather than immediate: a pan emits a
        # range change per mouse move, and one socket round trip per move is
        # how the previous backfill attempt drowned the terminal.
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(HISTORY_DEBOUNCE_MS)
        self._history_timer.timeout.connect(self._fetch_history_if_needed)
        # A QThread with no Python reference is collected mid-run and takes the
        # process with it. This is the reference.
        self._history_fetcher = None
        self._history_pane = None
        # Windows already asked for, so a range that legitimately has no
        # recording is not re-requested every time the user pans over it.
        self._history_tried: set = set()
        # Replay payloads waiting to be folded, a slice per frame.
        self._fold_q: deque = deque()
        # When each symbol was last seen on screen. Absent means "on screen
        # now"; see DEMOTE_GRACE_S.
        self._cold_since: dict = {}
        # Symbols that have been on screen at least once. Only these earn the
        # demotion grace period - see _sync_hot.
        self._ever_hot: set = set()
        # Startup backfill: "idle" until asked, "loading" while history is
        # being fetched, then "done" or "live_only". Nothing gates the drain
        # any more - see HOLD_FOR_BACKFILL.
        self._bf_state = "idle"
        # Session-history merge for symbols selected after startup.
        self._sess_fetchers: dict = {}
        self._sess_done: set = set()
        self._sess_queue: list = []
        # Symbols seen but not yet added to the pickers - see
        # _register_symbol / _flush_symbol_items.
        self._pending_sym_items: list = []
        # Events rejected at the boundary as unchartable - see the drain.
        self._dropped_bad = 0
        self._bf_since = 0.0
        self._bf_dropped_at = 0
        self._bf_seam: dict = {}
        self._bf_symbols: list = []
        self._bf_done: set = set()
        self._bf_failed: set = set()
        self._bf_fetchers: dict = {}
        # Cancelled workers that have not noticed yet. Held so Qt cannot
        # destroy a running QThread; reaped in _tick.
        self._bf_zombies: list = []
        self._bf_want = None
        self._bf_arm_timer = None

        self._timer = GovernedTimer(self, self._tick, 33, priority=0)
        self.glw.set_gov_key(id(self))
        GOVERNOR.set_focus(id(self))
        self._timer.start()

    # Per-pane view state, exposed under the names the rest of the window (and
    # the workspace, and the tests) already use. Each delegates to the pane the
    # toolbar is driving, so "the chart" always means the one you selected.
    @property
    def tf_s(self) -> int:
        """Timeframe of the pane the toolbar is driving. Per pane, so a grid
        can show the same name on four horizons at once."""
        p = getattr(self, "_active_pane", None)
        return p.tf_s if p is not None else 60

    @tf_s.setter
    def tf_s(self, v: int) -> None:
        p = getattr(self, "_active_pane", None)
        if p is not None:
            p.tf_s = int(v)

    @property
    def auto_scroll(self) -> bool:
        p = getattr(self, "_active_pane", None)
        return p.auto_scroll if p is not None else True

    @auto_scroll.setter
    def auto_scroll(self, v: bool) -> None:
        # A timeframe change applies to every chart, so this deliberately sets
        # ALL panes rather than only the active one.
        for p in getattr(self, "_panes", ()):
            p.auto_scroll = bool(v)

    @property
    def _auto_y(self) -> bool:
        p = getattr(self, "_active_pane", None)
        return p.auto_y if p is not None else True

    @_auto_y.setter
    def _auto_y(self, v: bool) -> None:
        p = getattr(self, "_active_pane", None)
        if p is not None:
            p.auto_y = bool(v)

    @property
    def _needs_center(self) -> bool:
        p = getattr(self, "_active_pane", None)
        return p._needs_center if p is not None else True

    @_needs_center.setter
    def _needs_center(self, v: bool) -> None:
        p = getattr(self, "_active_pane", None)
        if p is not None:
            p._needs_center = bool(v)

    @property
    def active_symbol(self) -> str:
        """The symbol of the pane the toolbar is driving.

        A property rather than an attribute because with a grid there is no
        single 'current symbol' any more - there is one per pane, and the
        toolbar acts on whichever is selected. Every existing call site keeps
        working because the name and the type are unchanged.
        """
        pane = getattr(self, "_active_pane", None)
        return pane.symbol if pane is not None else self._pending_active_symbol

    @active_symbol.setter
    def active_symbol(self, value: str) -> None:
        pane = getattr(self, "_active_pane", None)
        if pane is None:                     # during __init__, before panes exist
            self._pending_active_symbol = value
        else:
            pane.symbol = value

    def start_feed(self) -> None:
        """Begin streaming.

        Deliberately NOT called from __init__: SyntheticFeed.start() emits its
        entire 90-minute prefill synchronously, so a Recorder attached after
        construction used to miss every one of those events. The caller starts
        the feed once all taps are in place.
        """
        self.feed.start()

    def _on_execution(self, ex) -> None:
        """Feed-thread entry point for your own fills.

        Appended directly rather than queued: fills arrive at human frequency,
        not market frequency, and a list append is atomic under the GIL. Routing
        them through the event queue would make them compete with book sweeps
        for the drain budget for no benefit.
        """
        lst = self.executions.get(ex.symbol)
        if lst is None:
            lst = self.executions[ex.symbol] = []
        lst.append(ex)
        if len(lst) > 5000:
            del lst[:len(lst) - 5000]
        self._dirty = True

    def _enqueue(self, ev) -> None:
        """Feed-thread entry point. Counts what the bounded queue sheds."""
        q = self._event_q
        if len(q) >= EVENT_QUEUE_MAX:
            self._dropped += 1
        q.append(ev)

    # ---- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        tb = QToolBar()
        # QMainWindow.saveState()/restoreState() key every toolbar and dock by
        # objectName. With the default empty name Qt cannot match them back and
        # the restore is undefined — in practice it dumped this toolbar into the
        # LEFT dock area at 556 px and left the chart 38 % of the window. Every
        # toolbar and dock below therefore carries a stable, unique name.
        tb.setObjectName("tb_main")
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" Symbol "))
        self.sym_combo = QComboBox()
        self.sym_combo.setMinimumWidth(90)
        self.sym_combo.currentTextChanged.connect(self._on_symbol)
        tb.addWidget(self.sym_combo)

        tb.addWidget(QLabel("  Grid "))
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(list(LAYOUTS))
        self.layout_combo.setToolTip(
            "Show one, two or four charts at once. Each pane keeps its own "
            "symbol, zoom and drawings; the highlighted one is what the "
            "toolbar and the drawing tools act on. Click a chart to select it.")
        self.layout_combo.currentTextChanged.connect(self._on_layout)
        tb.addWidget(self.layout_combo)

        tb.addWidget(QLabel("  TF "))
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(list(TF_CHOICES))
        self.tf_combo.currentTextChanged.connect(self._on_tf)
        tb.addWidget(self.tf_combo)

        tb.addWidget(QLabel("  Mode "))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(MODES))
        self.mode_combo.currentTextChanged.connect(self._on_mode)
        tb.addWidget(self.mode_combo)

        tb.addWidget(QLabel("  Price "))
        self.step_combo = QComboBox()
        self.step_combo.addItems(list(PRICE_STEPS))
        self.step_combo.setCurrentText("Auto")
        self.step_combo.setToolTip(
            "Price rows per footprint cell. A 1-tick grid is right on a 10s "
            "candle and unreadable on a 1h one, where hundreds of levels "
            "collapse into a stripe. Volume is SUMMED into each row, and the "
            "POC, value area and imbalances are recomputed on that grid.\n"
            "Auto follows the zoom so rows stay readable while you pan.")
        self.step_combo.currentTextChanged.connect(self._on_price_step)
        tb.addWidget(self.step_combo)
        # Auto is otherwise opaque - show which step it actually settled on.
        self.lbl_step = QLabel("")
        self.lbl_step.setStyleSheet("color:#8A93A6;font-weight:600;")
        tb.addWidget(self.lbl_step)

        # ---- Overlays menu ----------------------------------------------
        # These were nine checkboxes and a combo strung across the toolbar,
        # which pushed the live stats readout off the right-hand edge on a
        # 1920-wide screen. They are settings you change occasionally, not
        # controls you reach for every minute, so they belong behind a menu.
        #
        # Deliberately QAction, not QCheckBox: QAction exposes the same
        # isChecked()/setChecked()/toggled API, so workspace.py persists them
        # with no change at all.
        tb.addSeparator()
        self.menu_overlays = QMenu("Overlays", self)
        btn_overlays = QToolButton()
        btn_overlays.setText("Overlays ▾")
        btn_overlays.setMenu(self.menu_overlays)
        btn_overlays.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(btn_overlays)

        def _act(menu, label, checked, slot, tip=""):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(checked)
            if tip:
                a.setToolTip(tip)
            a.toggled.connect(slot)
            return a

        self.chk_imb = _act(self.menu_overlays, "Imbalance", True,
                            lambda v: self.fp.set_show_imbalance(v))

        # The imbalance factor is a choice among values, not a toggle, so it
        # goes in as a real widget rather than four mutually exclusive items.
        w_imb = QWidget()
        _l = QHBoxLayout(w_imb)
        _l.setContentsMargins(24, 2, 8, 2)
        _l.addWidget(QLabel("Imbalance ×"))
        self.imb_combo = QComboBox()
        self.imb_combo.addItems(["2.0", "3.0", "4.0", "5.0"])
        self.imb_combo.setCurrentText("3.0")
        self.imb_combo.currentTextChanged.connect(
            lambda t: self.fp.set_imbalance_factor(float(t)))
        _l.addWidget(self.imb_combo)
        wa = QWidgetAction(self)
        wa.setDefaultWidget(w_imb)
        self.menu_overlays.addAction(wa)

        self.chk_va = _act(self.menu_overlays, "Value Area", True,
                           lambda v: self.fp.set_show_va(v))
        self.chk_numbers = _act(
            self.menu_overlays, "Numbers", True, self._on_numbers,
            "Show the volume numbers inside cells and the per-bar "
            "delta/volume footer")
        self.chk_fills = _act(
            self.menu_overlays, "My fills", True, self._on_fills,
            "Mark your own executions: hollow green = bought, red = sold, "
            "radius by size")

        self.menu_overlays.addSeparator()
        # OFF by default: a secondary study should not take a fifth of every
        # chart before anyone asks for it - four times over in a 2x2 grid.
        self.chk_cvd = _act(self.menu_overlays, "CVD pane", False,
                            self._on_cvd_pane,
                            "Show the cumulative-delta sub-chart on this chart")
        # OFF by default, for the same reason as the CVD pane. A 2x2 grid put
        # a VWAP on all four charts before anyone asked for one, so three
        # quarters of the lines on screen were unrequested - and an overlay
        # nobody turned on is chart-junk competing with the flow for
        # attention. Turning it on for the chart being traded is one click.
        self.chk_vwap = _act(self.menu_overlays, "VWAP", False,
                             self._on_vwap_toggled,
                             "Volume-weighted average price for this chart")
        self.chk_magnet = _act(self.menu_overlays, "Magnet (snap to OHLC)", True,
                               self._on_magnet_toggled,
                               "Snap drawing points to the nearest bar "
                               "open/high/low/close  (Alt+N)")
        self.chk_cpr = _act(self.menu_overlays, "CPR", False,
                            self._on_cpr_toggled)
        self.chk_ema = _act(self.menu_overlays, "EMAs", False,
                            self._on_ema_toggled)

        # ---- Windows menu ------------------------------------------------
        self.menu_windows = QMenu("Windows", self)
        btn_windows = QToolButton()
        btn_windows.setText("Windows ▾")
        btn_windows.setMenu(self.menu_windows)
        btn_windows.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(btn_windows)
        for label, slot, tip in (
            ("Tape", self._open_tape,
             "Tape reader: every print, speed and running delta"),
            ("Profile", self._open_profile, "Session volume profile"),
            ("Analytics", self._open_analytics, "Order-flow analytics"),
            ("Monitor", self._open_monitor, "Market monitor across symbols"),
            ("DOM", self._open_dom, "Depth-of-market ladder"),
        ):
            a = self.menu_windows.addAction(label)
            a.setToolTip(tip)
            a.triggered.connect(slot)



        tb.addSeparator()
        tb.addWidget(QLabel(" Theme "))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.currentTextChanged.connect(self._on_theme)
        tb.addWidget(self.theme_combo)

        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setToolTip("Settings")
        self.btn_settings.setFixedWidth(34)
        self.btn_settings.clicked.connect(self._open_settings)
        tb.addWidget(self.btn_settings)

        self.btn_center = QPushButton("Center")
        self.btn_center.setToolTip("Re-centre on the latest bars  (Alt+R)")
        self.btn_center.setShortcut("Alt+R")
        self.btn_center.clicked.connect(self._center)
        tb.addWidget(self.btn_center)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.lbl_stats = QLabel("  ")
        tb.addWidget(self.lbl_stats)

        # Live-feed indicator: in --live mode you need to see at a glance
        # whether the DLL is actually attached to both pipes.
        self.lbl_link = QLabel("")
        tb.addWidget(self.lbl_link)

        # The most-used window gets the corner, not a menu item. Placed after
        # the expanding spacer so it stays pinned to the top-right however wide
        # the window is, and coloured so it is findable without reading.
        self.btn_bookmap = QPushButton("  BOOKMAP  ")
        self.btn_bookmap.setToolTip(
            "Open the liquidity heatmap for the selected symbol  "
            "(shows up to four books in one window)")
        self.btn_bookmap.setMinimumHeight(30)
        self.btn_bookmap.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bookmap.setStyleSheet(
            "QPushButton {"
            " background:#1E9E76; color:#06110C; font-weight:800;"
            " font-size:13px; letter-spacing:1px; border:none;"
            " border-radius:5px; padding:5px 16px; margin:2px 6px 2px 10px; }"
            "QPushButton:hover  { background:#26C08F; }"
            "QPushButton:pressed{ background:#178A66; }")
        self.btn_bookmap.clicked.connect(self._open_bookmap)
        tb.addWidget(self.btn_bookmap)

        # ---- Drawing Toolbar (Left) ----
        dtb = QToolBar("Drawings")
        dtb.setObjectName("tb_drawings")
        dtb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, dtb)
        
        self.active_drawing_tool = None
        # Magnet: snap drawing points to bar extremes. On by default,
        # like TradingView, because a level drawn a few cents off the wick
        # is not the level anybody meant.
        self.magnet_on = True
        self.drawing_items = []
        self._drawing_start_point = None
        # Freehand stroke in progress, or None. Initialised here as well as in
        # _set_drawing_tool: mouse moves can arrive before any tool is armed.
        self._pen_points = None
        self._selected_drawing = None

        # A narrow glyph strip, as every charting terminal has. Full-word buttons
        # made this a 166 px column stealing a tenth of the window from the
        # chart; the meaning now lives in the tooltip. Buttons are checkable so
        # the armed tool is visible — previously nothing indicated which tool
        # was active, and the tool auto-reverts after one use.
        self._tool_buttons: dict = {}
        for glyph, tool, tip in (
            ("⌖", None, "Cursor — pan and zoom"),
            ("F", "Fib", "Fibonacci retracement"),
            ("▲", "Long", "Long position — entry / TP / SL with R:R"),
            ("▼", "Short", "Short position — entry / TP / SL with R:R"),
            ("▤", "VP",
             "Volume Profile — drag a box over any range. "
             "Draws volume-at-price with POC (yellow), and VAH / VAL (blue) "
             "bounding the 70% value area, each labelled with its price."),
            ("✎", "Pen", "Freehand pen — hold the left button and draw"),
            ("╪", "CPR", "Central Pivot Range over the boxed bars"),
            ("⟷", "Measure",
             "Measure — price move, %, bars, duration and volume"),
            ("—", "HLine", "Horizontal price level  (Alt+H at the crosshair)"),
        ):
            b = QPushButton(glyph)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setFixedSize(34, 30)
            b.clicked.connect(lambda _=False, t=tool: self._set_drawing_tool(t))
            dtb.addWidget(b)
            self._tool_buttons[tool] = b
        self._tool_buttons[None].setChecked(True)

        btn_clear_drawings = QPushButton("✕")
        btn_clear_drawings.setToolTip("Clear all drawings")
        btn_clear_drawings.setFixedSize(34, 30)
        btn_clear_drawings.clicked.connect(self._clear_drawings)
        dtb.addWidget(btn_clear_drawings)

        # ---- chart grid: one, two or four panes -------------------------
        # The central widget is a grid container rather than a single plot, so
        # the layout can go to 1x2 or 2x2 without rebuilding anything. Panes
        # are created ONCE, up front, and shown or hidden - constructing and
        # destroying plots on every layout change would orphan the drawings
        # that live on them.
        # SPLITTERS, not a fixed grid, so the panes are resizable by dragging.
        # An outer vertical splitter holds one horizontal splitter per row; the
        # two rows' column positions are kept in step (see _link_rows), so the
        # vertical divider reads as ONE line through the whole grid and
        # grabbing it anywhere - including where all four corners meet - moves
        # both rows together. Two independent splitters would let the rows
        # drift out of alignment and stop looking like a grid at all.
        # A plain container holds the splitter. The floating ticker-search box
        # is a child of THIS, not of the splitter: a QLineEdit parented to a
        # QSplitter becomes a splitter section, so the search box was silently
        # occupying a third row of the grid (sizes read [456, 456, 0]) and
        # would have appeared as a resizable band the moment it was shown.
        self._chart_host = QWidget()
        _cv = QVBoxLayout(self._chart_host)
        _cv.setContentsMargins(0, 0, 0, 0)
        self._grid_host = QSplitter(Qt.Orientation.Vertical)
        self._grid_host.setChildrenCollapsible(False)
        self._grid_host.setHandleWidth(6)
        self._rows = [QSplitter(Qt.Orientation.Horizontal) for _ in range(2)]
        for r in self._rows:
            r.setChildrenCollapsible(False)
            r.setHandleWidth(6)
            self._grid_host.addWidget(r)
        self._syncing_rows = False
        for r in self._rows:
            r.splitterMoved.connect(lambda _p, _i, sp=r: self._link_rows(sp))
        _cv.addWidget(self._grid_host)
        self.setCentralWidget(self._chart_host)

        self._panes = [
            ChartPane(self, id(self), self.theme, self.instruments, i)
            for i in range(MAX_PANES)
        ]
        for pane in self._panes:
            pane.tf_combo.addItems(list(TF_CHOICES))
            pane.tf_combo.setCurrentText("1m")
            pane.tf_combo.currentTextChanged.connect(
                lambda t, p=pane: self._set_pane_tf(p, t))
            pane.mode_combo.addItems(list(MODES))
            pane.mode_combo.currentTextChanged.connect(
                lambda t, p=pane: self._on_pane_mode(p, t))
            pane.sym_combo.currentTextChanged.connect(
                lambda t, p=pane: self._on_pane_symbol(p, t))
            pane.sym_combo.lineEdit().returnPressed.connect(
                lambda p=pane: self._on_pane_symbol(p, p.sym_combo.currentText()))
            pane.glw.scene().sigMouseMoved.connect(
                lambda pos, p=pane: self._on_mouse_move(pos, p))
            pane.glw.scene().sigMouseClicked.connect(
                lambda ev, p=pane: self._on_mouse_click(ev, p))
            pane.price_plot.getViewBox().sigRangeChangedManually.connect(
                lambda *_a, p=pane: self._on_view(p))
        self._active_pane = self._panes[0]
        self._bind_pane(self._panes[0])
        self._apply_layout(1)
        # APPLY the overlay defaults, do not merely store them. _act sets the
        # checked state before connecting `toggled`, so nothing fired at
        # startup and every pane kept its constructor default - which for a
        # PlotDataItem is visible. That is why VWAP drew on charts whose switch
        # was off.
        self.apply_overlays()

        # TradingView-style ticker search: start typing a symbol anywhere on the
        # chart and a floating box appears; Enter opens it, Escape cancels.
        self.sym_search = QLineEdit(self._chart_host)
        self.sym_search.setPlaceholderText("Type ticker, Enter to open")
        self.sym_search.setStyleSheet(
            "QLineEdit { background:#0A0D14; color:#F0F0F0; border:2px solid #2E9E7E;"
            " border-radius:8px; padding:8px 14px; font-size:15px; font-weight:700;"
            " letter-spacing:1px; }")
        self.sym_search.setFixedSize(240, 40)
        self.sym_search.hide()
        self.sym_search.returnPressed.connect(self._apply_sym_search)
        self.sym_search.installEventFilter(self)

        self._last_cursor = None
        # Parented to the chart host so it cannot end up behind the terminal,
        # and cannot steal focus while a ticker is being typed.
        self._toast = AlertToast(self._chart_host)

        # ---- Time & Sales tape dock (right) ----
        self.tape = TapeWidget(
            get_source=lambda: (self.bookmaps.get(self.active_symbol).trades
                                if self.active_symbol in self.bookmaps else None),
            tick_fn=lambda: self.instruments.tick(self.active_symbol or "QQQ"),
            dt_fn=lambda: (self.bookmaps[self.active_symbol].col_dt
                           if self.active_symbol in self.bookmaps else 1.0),
        )
        self.stats = StatsPanel(self)
        self.stats_dock = QDockWidget("Session Statistics", self)
        self.stats_dock.setObjectName("dock_stats")
        self.stats_dock.setWidget(self.stats)
        self.stats_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea |
                                        Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.stats_dock)

        self.tape_dock = QDockWidget("Time & Sales", self)
        self.tape_dock.setObjectName("dock_tape")
        self.tape_dock.setWidget(self.tape)
        self.tape_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea |
                                       Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.tape_dock)

        self.signals = SignalsPanel(self)
        self.signals_dock = QDockWidget("Order-Flow Signals", self)
        self.signals_dock.setObjectName("dock_signals")
        self.signals_dock.setWidget(self.signals)
        self.signals_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea |
                                          Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.signals_dock)
        self.tabifyDockWidget(self.tape_dock, self.signals_dock)
        self.tape_dock.raise_()

        # Default split: the chart is the product, the panels are support. Left
        # unset, Qt hands the docks their size hints and the price pane ends up
        # far smaller than a trading terminal should allow.
        self.resizeDocks([self.stats_dock, self.tape_dock],
                         [210, 230], Qt.Orientation.Horizontal)

    # ---- theme -----------------------------------------------------------
    def _apply_theme(self) -> None:
        t = self.theme
        # EVERY pane, not just the active one. This used to run through the
        # `self.fp` / `self.glw` aliases, which point at the selected chart, so
        # in a grid the other three never got themed at all and kept
        # pyqtgraph's default black background while the selected one was the
        # theme colour. That is the "bottom charts are black" report.
        for pane in self._panes:
            pane.theme = t
            pane.fp.set_theme(t)
            pane.vwap_curve.setPen(pg.mkPen(t.vwap, width=2))
            pane.cvd_curve.setPen(pg.mkPen(t.cvd, width=2))
            for mult, curve in pane.vwap_bands:
                curve.setPen(pg.mkPen(t.vwap, width=1,
                                      style=(Qt.PenStyle.DashLine if mult == 1
                                             else Qt.PenStyle.DotLine)))
            pane.price_line.setPen(pg.mkPen(t.cvd, width=1,
                                            style=Qt.PenStyle.DashLine))
            # The tag is filled, so its text colour is the BACKGROUND colour -
            # left alone on a theme switch it stays dark-on-dark or
            # light-on-light and the live price becomes unreadable.
            _lbl = getattr(pane.price_line, "label", None)
            if _lbl is not None:
                _lbl.setColor(t.bg)
                _lbl.fill = pg.mkBrush(t.cvd)
                _lbl.update()
            pane.glw.setBackground(t.bg)
            # Re-applying the SAME sheet forces a full re-polish for nothing.
            # The pane owns its border state and reapplies it when it changes;
            # a theme change goes through _apply_theme on the window itself.
            pass
        # Restrained, terminal-like chrome. Painting every QPushButton in the
        # bull accent turned the toolbars into a wall of teal that competed with
        # the chart for attention; controls are now neutral, with the accent
        # reserved for the armed drawing tool.
        # ONE SOURCE OF TRUTH, not thirteen typed-in paddings. Every
        # number in the stylesheet is now a token from ui/design.py, so a
        # spacing or radius decision is made once and holds everywhere -
        # which is the difference between a composed interface and an
        # assembled one. See that module for the reasoning, including the
        # places Apple's guidance is deliberately NOT followed.
        self.setStyleSheet(design.qss(t))
        for pane in self._panes:
            for plot in (pane.price_plot, pane.cvd_plot):
                for ax_name in ("bottom", "right"):
                    ax = plot.getAxis(ax_name)
                    ax.setPen(pg.mkPen(t.axis))
                    ax.setTextPen(pg.mkPen(t.text))

    # ---- feed drain + redraw (GUI thread) --------------------------------
    def _tick(self) -> None:
        try:
            self._drain_and_draw()
        except Exception:
            # Belt and braces alongside the excepthook in app.py: keep the timer
            # alive and the terminal on screen even if one frame throws.
            log.exception("frame failed (recovering)")

    def _drain_and_draw(self) -> None:
        drained = 0
        q = self._event_q
        if self._bf_state in ("arming", "holding") and HOLD_FOR_BACKFILL:
            # HOLD THE DRAIN, NOT THE DRAW.
            #
            # The events stay in the queue and are drained in order once the
            # history is in, so the replay and the live stream meet at the seam
            # rather than interleaving. "arming" is held for the same reason: a
            # bar built from the first second of live data would make the swap
            # refuse.
            #
            # But returning from here skipped the REDRAW as well, and with a
            # seam wait plus a load cap that is nearly a minute in which the
            # window paints nothing and changes nothing. Reported as "the app
            # froze", and fairly - a terminal that has stopped updating is
            # frozen as far as anyone using it is concerned. The frame still
            # runs; only the queue is left alone.
            self._check_backfill_hold()
            if self._bf_state in ("arming", "holding") and HOLD_FOR_BACKFILL:
                self._update_link()
                self._redraw()
                return
        backlog = len(q)
        budget = DRAIN_BUDGET_BUSY_S if backlog >= DRAIN_BUSY_AT else DRAIN_BUDGET_S
        deadline = time.perf_counter() + budget
        _w_drain = watch("drain")
        _w_drain.__enter__()
        while q:
            # CHECK THE CLOCK EVERY 32 EVENTS, NOT EVERY 256.
            #
            # The old interval was chosen against the cost of a TRADE, where
            # perf_counter is comparable to the work and checking per event
            # would double the drain's cost to police it. That reasoning does
            # not survive a deep book: a fresh 800-level snapshot costs 93 us
            # against a trade's 1.7 us - 50x - so a window of 256 events can
            # be 24 ms of work before the budget is even consulted, and the
            # budget is 22 ms.
            #
            # Found by the watchdog, which named it directly:
            #     SLOW FRAME 199 ms - drain 198ms  redraw 1ms
            # with half the queued events being 800-level books.
            #
            # 32 costs 40 ns per check spread over 32 events - about 1 ns each,
            # against the 47 us average this queue actually carries. The
            # original concern is a rounding error at this granularity.
            # ...AND EVERY 8, NOT EVERY 32.
            #
            # 32 was already a correction from 256, made when a deep book was
            # measured at 93 us against a trade's 1.7 us. With 200 symbols and
            # four charts plus four books open, the watchdog still caught
            #     SLOW FRAME 84 ms - drain 80ms  redraw 4ms
            # against a 22 ms busy budget: a window of 32 events is 32 events
            # of overshoot, and the events that arrive together are the
            # expensive ones, because a burst of book snapshots is what a busy
            # queue is MADE of.
            #
            # perf_counter is ~40 ns, so checking every 8 costs 5 ns per event
            # against the tens of microseconds an event of this kind actually
            # takes. The budget is what protects the frame; it has to be
            # consulted often enough to mean something.
            if not (drained & 7) and time.perf_counter() > deadline:
                break
            ev = q.popleft()
            drained += 1
            if isinstance(ev, Trade):
                # VALIDATE AT THE BOUNDARY. A price of 1e12 overflows the int32
                # tick index every array in the storage layer uses and raises
                # inside Bar.seal, which then makes every later paint of that
                # bar fail; a NaN price does not raise at all - it propagates
                # into high/low and makes the price axis unusable with nothing
                # on screen to say why. Both were reproduced by injecting them.
                #
                # COUNTED, not silently dropped. This app does not discard data
                # without saying so - `dropped_bad` is reported by the link
                # status, so a feed sending nonsense is visible rather than
                # quietly thinned.
                if not sane_trade(ev):
                    self._dropped_bad += 1
                    continue
                s = self.series.get(ev.symbol)
                if s is None:
                    s = self.series[ev.symbol] = BarSeries(ev.symbol, self.instruments)
                s.add_trade(ev)
                self._bookmap(ev.symbol).add_trade(ev)
                self._profile(ev.symbol).add_trade(ev)
                if ev.symbol not in self._known_symbols:
                    self._register_symbol(ev.symbol)
                # ANY visible pane, not just the active one - otherwise the
                # three charts you are not clicking on would sit frozen.
                if self._shows(ev.symbol):
                    self._dirty = True
                # Alerts run HERE, not in a chart's paint, so a level on a
                # symbol nobody is watching still fires. Costs one dict lookup
                # per print when no alert exists for that symbol.
                if self.alerts:
                    hit = self.alerts.check(ev.symbol, ev.price)
                    if hit:
                        self._alert_pending.extend(hit)
            else:  # BookSnapshot
                self.latest_book[ev.symbol] = ev
                self._bookmap(ev.symbol).add_book(ev)
                # Register on depth too, not only on a trade. Trades are
                # reconstructed from the L1 cumulative-volume delta, so a symbol
                # that is quoting but has not printed yet produces NO trade at
                # all - pre-market, thin names, or anything whose volume has not
                # moved since we connected. Those symbols were streaming depth
                # into a BookmapBuffer that nothing could ever select, because
                # they never reached the combo box.
                if ev.symbol not in self._known_symbols:
                    self._register_symbol(ev.symbol)
                s = self.series.get(ev.symbol)
                if s is not None:
                    s.add_book(ev)
                if self._shows(ev.symbol):
                    self._dirty = True

        _w_drain.__exit__()
        if self._alert_pending:
            with watch("alerts"):
                fired, self._alert_pending = self._alert_pending, []
                self._fire_alerts(fired)

        # Refresh the live indicator ~2x/sec even when no data is flowing, so
        # "waiting for Takion" is visible before the first tick arrives.
        # A HEARTBEAT IN THE LOG, so a stuck terminal says WHY.
        #
        # Three rounds of this have been diagnosed from a server log that shows
        # what was requested and nothing about what the client then did with
        # it. When the app "sticks", the question is always the same and has
        # never been answerable from outside: is the GUI thread busy, is the
        # feed still delivering, is the queue backing up, or is a loader still
        # running? One line every few seconds answers all four.
        self._hb = getattr(self, "_hb", 0) + 1
        if self._hb % 150 == 0:
            now = time.monotonic()
            prev = getattr(self, "_hb_at", now)
            self._hb_at = now
            fps = 150.0 / max(now - prev, 1e-6)
            fd = self.feed
            log.info("health: %.1f fps | queue %d | dropped %d | bad %d | "
                     "feed lost %s repaired %s | loaders bf=%d sess=%d q=%d | "
                     "state=%s | syms %d",
                     fps, len(self._event_q), self._dropped,
                     self._dropped_bad,
                     getattr(getattr(fd, "gaps", None), "lost", "-"),
                     getattr(fd, "repaired", "-"),
                     len(getattr(self, "_bf_fetchers", {})),
                     len(self._sess_fetchers), len(self._sess_queue),
                     self._bf_state, len(self.series))
        self._link_tick = getattr(self, "_link_tick", 0) + 1
        if self._link_tick % 15 == 0:
            self._update_link()
        # Retention follows what is on screen. Re-checked on a timer rather
        # than hooked to every place a symbol can change - there are a dozen
        # of those (pane combo, toolbar, bookmap search, workspace restore,
        # grid layout) and one missed hook is a symbol that silently keeps or
        # loses history. A second's lag costs nothing; a missed hook is a bug
        # nobody would find.
        if self._link_tick % 25 == 0:
            with watch("sync_hot"):
                self._sync_hot()
        with watch("symbols"):
            self._flush_symbol_items()
        with watch("history_fold"):
            self._fold_pending()
        if self._bf_zombies:
            self._bf_zombies = [f for f in self._bf_zombies if f.isRunning()]

        if self._dirty and self.active_symbol:
            self._redraw()
            self._dirty = False

    # ---- chart panes -----------------------------------------------------
    def _shows(self, sym: str) -> bool:
        """Is this symbol on screen in any visible pane?"""
        return any(p.symbol == sym for p in self._panes[:self._n_panes])

    def _hot_symbols(self) -> set[str]:
        """Symbols something is actually DRAWING, anywhere in the app.

        Everything else keeps a reduced history - see BookmapBuffer.set_hot.
        The set is deliberately generous: a symbol in any chart pane, any
        bookmap pane, or any child window counts, and so does the toolbar's
        selection even before a pane has caught up with it. Getting this wrong
        in the cheap direction means dropping history the user can see, which
        is much worse than holding a few extra megabytes.
        """
        hot = {p.symbol for p in self._panes[:self._n_panes] if p.symbol}
        if self.active_symbol:
            hot.add(self.active_symbol)
        for key in self._child_windows:
            # Child windows are registered as "profile:NVDA", "analytics:QQQ".
            if ":" in key:
                hot.add(key.split(":", 1)[1].strip().upper())
        bm = self._child("bookmap")
        if bm is not None:
            try:
                for pane in bm._visible_panes():
                    s = getattr(getattr(pane, "buffer", None), "symbol", "")
                    if s:
                        hot.add(s)
            except Exception:
                # A half-built or closing bookmap must not cost the app its
                # retention policy; erring hot is the safe direction.
                log.debug("could not read bookmap panes for hot set",
                          exc_info=True)
        return hot

    def _sync_hot(self) -> None:
        """Apply the hot set. Cheap: set_hot returns at once when unchanged."""
        hot = self._hot_symbols()
        # A SYMBOL BEING DRAWN FOR THE FIRST TIME GETS ITS SESSION.
        #
        # _sync_hot already knows precisely when a symbol goes on screen, which
        # is the moment its history is wanted and the only moment it is worth
        # fetching. One request per symbol per run - request_session_history
        # keeps its own done-set - so switching back and forth costs nothing.
        if getattr(self.feed, "replay_host", ""):
            for sym in hot:
                if sym not in self._sess_done and sym not in self._sess_fetchers:
                    self.request_session_history(sym)

        # PROMOTIONS FIRST AND ALWAYS. A symbol the user just selected must be
        # at full retention before the next frame draws it; there is no budget
        # worth trading against that.
        for sym in hot:
            b = self.bookmaps.get(sym)
            if b is not None:
                b.set_hot(True)
            s = self.series.get(sym)
            if s is not None:
                s.set_hot(True)

        # DEMOTIONS ARE RATE-LIMITED. Each one reallocates a tape ring and
        # evicts columns - measured at 626 us, which is nothing for the two or
        # three symbols a layout change actually releases, but 120 ms if two
        # hundred are released at once. That would be a dropped frame caused by
        # the very work meant to prevent dropped frames.
        #
        # Nothing is lost by spreading them: a symbol waiting its turn is
        # holding memory it was already holding, and the queue drains within a
        # few seconds. This is deliberately not a full scan either - it
        # resumes where it stopped, so the cost per call is bounded by
        # MAX_DEMOTIONS and not by the size of the universe.
        now = time.monotonic()
        for sym in hot:
            self._cold_since.pop(sym, None)
            self._ever_hot.add(sym)
        done = 0
        fold_left = MAX_FOLD_COLS_PER_SYNC
        syms = self._demote_cursor = getattr(self, "_demote_cursor", 0)
        keys = list(self.bookmaps)
        n = len(keys)
        for k in range(n):
            if done >= MAX_DEMOTIONS_PER_SYNC:
                break
            sym = keys[(syms + k) % n]
            self._demote_cursor = (syms + k + 1) % n
            if sym in hot:
                continue
            # GRACE, BUT ONLY FOR A SYMBOL THAT WAS ACTUALLY ON SCREEN.
            #
            # The grace exists so that glancing at another ticker does not
            # destroy the history of the one being traded. A symbol that has
            # NEVER been displayed has no history worth protecting, and giving
            # it five minutes anyway means every symbol in the basket is held
            # at full retention for the first five minutes of every session.
            #
            # Measured at 100 symbols and 400 depth a side, that is 2.3 GB and
            # a bookmap p95 of 325 ms - the whole startup window spent in
            # exactly the state the hot/cold split exists to avoid.
            if sym in self._ever_hot:
                first = self._cold_since.get(sym)
                if first is None:
                    self._cold_since[sym] = now
                    continue
                if now - first < DEMOTE_GRACE_S:
                    continue
            # The budget counts WORK DONE, not calls made. A symbol registered
            # a moment ago has no columns to evict and no ring to shrink, so
            # demoting it is free - and counting it left a thousand-symbol
            # backlog draining six a pass while the free ones ahead of it used
            # every slot. Measured at 1,000 symbols: a 760-deep queue that
            # never cleared. set_hot reports whether it released anything.
            # THE BUDGET IS IN COLUMNS, NOT SYMBOLS.
            #
            # Every evicted column is now folded into the session archive, at a
            # measured 38 us. A symbol with a full 1,400-column ring therefore
            # costs 47 ms to demote, and six of those in one pass is 282 ms -
            # caught by the watchdog at 200 symbols as
            #     SLOW FRAME 239 ms - sync_hot 216ms
            # A budget that counts symbols cannot see work that is per column,
            # which is the same mistake as a budget that counts calls.
            buf = self.bookmaps[sym]
            # Lower the cap and shrink the tape, but evict NOTHING here - the
            # eviction is the part that has to be budgeted, and set_hot returns
            # early once the cap is already down, so a symbol part-way through
            # demotion would never finish if the trim lived in there.
            tape_worked = (buf.set_hot(False, max_evict=0)
                           if buf.max_cols != buf.cold_cols else False)
            released = buf.trim_to_cap(fold_left)
            fold_left -= released
            # Still counting WORK DONE, not calls. A symbol registered a moment
            # ago has no columns to evict and no ring to shrink, so it consumes
            # no slot - counting it left a thousand-symbol backlog draining six
            # a pass while the free ones ahead used every one.
            worked = tape_worked or released > 0
            ser = self.series.get(sym)
            if ser is not None:
                worked = ser.set_hot(False) or worked
            if worked:
                done += 1
            if fold_left <= 0:
                break

    def _bind_pane(self, pane) -> None:
        """Point the window's chart attributes at `pane`.

        Everything outside this file - drawing tools, the settings dialog, the
        crosshair helpers, workspace restore - was written against `self.fp`,
        `self.price_plot` and friends. Rebinding those names on activation
        means all of it keeps working unchanged and always acts on the chart
        the user is actually pointing at, instead of every call site having to
        learn about panes.
        """
        self._active_pane = pane
        self.glw = pane.glw
        self.fp = pane.fp
        self.heatmap = pane.heatmap
        self.price_plot = pane.price_plot
        self.cvd_plot = pane.cvd_plot
        self.price_axis = pane.price_axis
        self.time_axis = pane.time_axis
        self.price_time_axis = pane.price_time_axis
        self.exec_item = pane.exec_item
        self.cpr_item = pane.cpr_item
        self.ema9_item = pane.ema9_item
        self.ema21_item = pane.ema21_item
        self.vwap_curve = pane.vwap_curve
        self.vwap_bands = pane.vwap_bands
        self.cvd_curve = pane.cvd_curve
        self.cvd_zero = pane.cvd_zero
        self.price_line = pane.price_line
        self.vline = pane.vline
        self.hline = pane.hline
        self.xhair = pane.xhair
        self.xhair_price = pane.xhair.price      # kept: workspace + tests
        self.xhair_time = pane.xhair.time
        self._preview = pane.preview
        self.drawing_items = pane.drawing_items
        multi = self._n_panes > 1
        for p in self._panes:
            p.set_active_look(p is pane, multi)
        # The toolbar describes whichever chart is selected, so its controls
        # have to be re-read from that pane - otherwise selecting a Heatmap
        # pane would still show "Footprint" and the next change would be
        # applied from the wrong starting point.
        self._sync_toolbar_to(pane)

    def _sync_toolbar_to(self, pane) -> None:
        for widget, value in ((self.mode_combo, pane.mode_combo.currentText()),
                              (self.tf_combo, pane.tf_combo.currentText())):
            if value and widget.currentText() != value:
                widget.blockSignals(True)
                widget.setCurrentText(value)
                widget.blockSignals(False)
        if hasattr(self, "chk_cvd"):
            on = pane.cvd_plot.isVisible()
            if self.chk_cvd.isChecked() != on:
                self.chk_cvd.blockSignals(True)
                self.chk_cvd.setChecked(on)
                self.chk_cvd.blockSignals(False)

    def _visible_panes(self) -> list:
        return self._panes[:self._n_panes]

    def _apply_layout(self, n: int) -> None:
        """Show `n` panes in a grid. Panes keep their symbol and drawings."""
        n = max(1, min(MAX_PANES, int(n)))
        rows, cols = next((r, c) for (p, r, c) in LAYOUTS.values() if p == n)
        self._n_panes = n
        for _p in self._panes:
            _p.needs_redraw = True
        for pane in self._panes:
            # HIDE, do not unparent. setParent(None) hands the widget to Python
            # while Qt still owns its QGraphicsScene and every signal connected
            # to it, and the two then disagree about who frees what at teardown
            # - an intermittent segfault on exit. Reparenting into a splitter
            # keeps destruction order Qt's problem, which it handles correctly.
            pane.container.setVisible(False)
        for i in range(n):
            row = self._rows[i // cols]
            pane = self._panes[i]
            if pane.container.parent() is not row:
                row.addWidget(pane.container)
            pane.container.setVisible(True)
        self._rows[1].setVisible(rows > 1)
        # Equal split on a layout change. Anything the user dragged applied to
        # a different number of panes, so carrying it over would be arbitrary.
        for r in self._rows[:rows]:
            vis = r.count()
            if vis:
                r.setSizes([10_000 // vis] * vis)
        self._grid_host.setSizes([10_000 // rows] * rows)
        # The active pane must be one that is on screen, or the toolbar would
        # be driving a chart nobody can see.
        if self._active_pane not in self._visible_panes():
            self._bind_pane(self._panes[0])
        else:
            self._bind_pane(self._active_pane)
        # A new pane starts on the toolbar's symbol so the grid is never blank.
        active_tf = self._active_pane.tf_combo.currentText()
        for pane in self._visible_panes():
            if not pane.symbol:
                pane.symbol = self.active_symbol
                pane._needs_center = True
            # A pane the user has never given a timeframe adopts the one in
            # use, so a new grid opens on the timeframe you were looking at.
            if not pane.tf_explicit and pane is not self._active_pane:
                self._set_pane_tf(pane, active_tf, explicit=False)
            if pane.symbol and pane.sym_combo.currentText() != pane.symbol:
                pane.sym_combo.blockSignals(True)
                pane.sym_combo.setCurrentText(pane.symbol)
                pane.sym_combo.blockSignals(False)
        self._dirty = True

    def _link_rows(self, moved) -> None:
        """Keep both rows' column split identical.

        Without this the top and bottom rows resize independently and the
        vertical divider becomes two unrelated lines that no longer meet - the
        grid stops being a grid. Guarded against re-entry because setSizes on
        the other row emits splitterMoved right back.
        """
        if self._syncing_rows or self._n_panes < 4:
            return
        sizes = moved.sizes()
        if len(sizes) < 2:
            return
        self._syncing_rows = True
        try:
            for r in self._rows:
                if r is not moved and r.count() == len(sizes):
                    r.setSizes(sizes)
        finally:
            self._syncing_rows = False

    def _on_pane_symbol(self, pane, sym: str) -> None:
        pane.needs_redraw = True
        """Change the ticker of ONE chart, leaving the other three alone."""
        sym = (sym or "").strip().upper()
        if not sym or sym == pane.symbol:
            return
        # Accept a symbol that has not been seen yet: in live mode the user
        # knows what they want to watch before the first print arrives.
        if sym not in self._known_symbols:
            self._register_symbol(sym)
        pane.symbol = sym
        pane.fp.tick = self.instruments.tick(sym)
        pane.auto_scroll = True
        pane._needs_center = True
        if pane.sym_combo.currentText() != sym:
            pane.sym_combo.blockSignals(True)
            pane.sym_combo.setCurrentText(sym)
            pane.sym_combo.blockSignals(False)
        if pane is self._active_pane:
            self.sym_combo.blockSignals(True)
            if self.sym_combo.findText(sym) >= 0:
                self.sym_combo.setCurrentText(sym)
            self.sym_combo.blockSignals(False)
        self._dirty = True

    def _on_pane_mode(self, pane, name: str) -> None:
        pane.needs_redraw = True
        """Chart type per pane - footprint here, delta there, heatmap next."""
        pane.set_mode(*MODES.get(name, ("Footprint", True, False)))
        if pane is self._active_pane and self.mode_combo.currentText() != name:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(name)
            self.mode_combo.blockSignals(False)
        self._dirty = True

    def _on_layout(self, txt: str) -> None:
        self._apply_layout(LAYOUTS.get(txt, (1, 1, 1))[0])

    # ---- price alerts ----------------------------------------------------
    def _fire_alerts(self, fired) -> None:
        """Beep once and show one toast, however many triggered together."""
        try:
            SOUNDER.play()
            self._toast.show_alerts(fired)
            for a in fired:
                log.info("ALERT %s at %.2f%s", a.symbol, a.fired_price,
                         f" ({a.note})" if a.note else "")
        except Exception:
            log.exception("alert notification failed (the alert still fired)")

    def add_alert_here(self) -> bool:
        """Alt+A: alert at the crosshair's price on this chart's symbol."""
        if self._last_cursor is None or not self.active_symbol:
            return False
        price = float(self._last_cursor[1])
        if not (price > 0.0):
            return False
        tick = self.instruments.tick(self.active_symbol)
        price = round(round(price / tick) * tick, 10)
        a = self.alerts.add(self.active_symbol, price, CROSS)
        # Draw it, so the level is visible on the chart rather than only
        # existing in a list somewhere.
        self._add_price_level(price)
        log.info("alert armed: %s", a.describe())
        self.lbl_stats.setText(f"  alert armed: {a.describe()}  ")
        return True

    def _register_symbol(self, sym: str) -> None:
        """Note a newly seen symbol. The COMBOS are updated later, in a batch.

        This ran inside the drain and did, for every new symbol, a findText -
        a linear scan of the item list - plus an addItem and a setCurrentText
        on EVERY pane's picker. At 200 symbols and four panes a burst of eight
        new names between two budget checks is thousands of Qt operations with
        nothing watching the clock, and the watchdog caught it as

            SLOW FRAME 149 ms - drain 148ms  redraw 1ms

        which is far more than the drain's own worst event (0.7 ms) could
        explain. Membership is now recorded in O(1) and the pickers are filled
        once a frame from the pending set, with a single addItems call instead
        of one per symbol per pane.
        """
        self._known_symbols.add(sym)
        self._pending_sym_items.append(sym)

    def _flush_symbol_items(self) -> None:
        """Push newly seen symbols into every picker, in one batch.

        Runs outside the drain, once a frame. addItems is one call for the
        whole batch, and the current text is read and restored once rather
        than per symbol - setCurrentText on an editable combo is the expensive
        half of what this used to do per name per pane.
        """
        pend = self._pending_sym_items
        if not pend:
            return
        self._pending_sym_items = []
        self.sym_combo.blockSignals(True)
        self.sym_combo.addItems(pend)
        self.sym_combo.blockSignals(False)
        for pane in getattr(self, "_panes", ()):
            combo = pane.sym_combo
            combo.blockSignals(True)
            keep = combo.currentText()
            combo.addItems(pend)
            if combo.currentText() != keep:
                combo.setCurrentText(keep)
            combo.blockSignals(False)
        for sym in pend:
            if (self._pending_symbol and sym == self._pending_symbol)                     or not self.active_symbol:
                self._select_pending_symbol(sym)
    def _select_pending_symbol(self, sym: str) -> None:
        """Adopt a newly arrived symbol if we were waiting for it.

        Either it is the one the saved workspace asked for, or nothing is
        selected yet and the first symbol to print is a better default than an
        empty chart.
        """
        if self._pending_symbol and sym == self._pending_symbol:
            self._pending_symbol = ""
        elif self.active_symbol:
            return
        self.active_symbol = sym
        self.sym_combo.setCurrentText(sym)
        self.fp.tick = self.instruments.tick(sym)
        self._dirty = True

    def _redraw(self) -> None:
        """Redraw the focused pane every frame, the others in turn.

        THE COST IS THE PAINT, NOT THIS CALLBACK. Measured, _redraw itself is
        0.41 ms for one chart and 1.35 ms for four - it only pushes data. What
        it also does is mark items dirty, and Qt then repaints them, which is
        where the milliseconds actually go.

        With four charts and four books open the frame governor measured total
        demand at 2.0x the budget and stretched every window's interval to
        match. Nothing was LATE - there were simply half as many frames, which
        from the chair is exactly what "it started lagging" means.

        So a pane that is not being interacted with updates on its turn instead
        of every frame: four panes become two paints per frame rather than
        four. This is the same trade the governor already makes between
        windows - protect the one being looked at, stretch the rest - applied
        inside a window, and the chart being traded from is unaffected.

        A pane that has just changed symbol, timeframe or mode is redrawn
        immediately regardless, so nothing waits its turn to show a change the
        user just asked for.
        """
        with watch("redraw"):
            panes = self._visible_panes()
            if len(panes) <= 1:
                for pane in panes:
                    self._redraw_pane(pane)
                return
            active = self._active_pane
            others = [p for p in panes if p is not active]
            self._redraw_turn = (getattr(self, "_redraw_turn", 0) + 1)
            turn = others[self._redraw_turn % len(others)]
            for pane in panes:
                if (pane is active or pane is turn
                        or getattr(pane, "needs_redraw", False)):
                    pane.needs_redraw = False
                    self._redraw_pane(pane)

    def _redraw_pane(self, pane) -> None:
        # A tick change (or a symbol selected before its first print) leaves the
        # series absent until the next trade rebuilds it. Every accessor below
        # must tolerate that or the GUI raises on the very next frame.
        sym = pane.symbol
        s = self.series.get(sym)
        if s is None:
            # Selecting a symbol that has depth but no prints yet, or a tick
            # change that dropped the series, used to `return` here - which left
            # the PREVIOUS symbol's bars painted on screen under the new
            # symbol's name. Clear instead, so an empty chart honestly means
            # "no trades for this symbol yet".
            pane.clear()
            if pane is self._active_pane:
                # Say WHY. A symbol reaches the picker as soon as any record
                # mentions it - depth included - so "no prints" covers four
                # different situations that need four different responses.
                why = ""
                h = getattr(self.feed, "symbol_health", None)
                if callable(h):
                    try:
                        why = h(sym)
                    except Exception:
                        why = ""
                self.lbl_stats.setText(
                    f"  {sym}   no prints - {why}  " if why
                    else f"  {sym}   (no prints yet)  ")
            return
        if pane is self._active_pane:
            self._sync_step_label()
        bars = s.view(pane.tf_s)
        pane.fp.set_bars(bars)
        if pane.heatmap.isVisible():
            pane.heatmap.set_bars(bars)
        # Both axes stay fed: whichever one is showing must have the bars, and
        # the cost is a list reference, not a copy.
        pane.time_axis.set_bars(bars)
        pane.price_time_axis.set_bars(bars)
        if pane.exec_item.isVisible():
            pane.exec_item.set_data(bars, self.executions.get(sym, []))

        if pane.cpr_item.isVisible(): pane.cpr_item.set_bars(bars)
        if pane.ema9_item.isVisible(): pane.ema9_item.set_bars(bars)
        if pane.ema21_item.isVisible(): pane.ema21_item.set_bars(bars)

        self._update_overlays(s, bars, pane)
        if bars:
            if pane._needs_center:
                # First bars for this symbol - fit the view to THEM instead of
                # leaving the previous instrument's price range on screen.
                pane._needs_center = False
                self._center(pane)
            elif pane.auto_scroll and pane.auto_y:
                self._follow_price(bars, pane)
            pane.sync_price_tag()
            pane.price_line.setPos(bars[-1].close)
            if pane.auto_scroll:
                # FOLLOW AT THE USER'S ZOOM, not at a fixed 22 bars.
                #
                # This used to snap the range to (n-22, n+3) on every frame it
                # ran. Zooming in near the live edge leaves auto_scroll on -
                # the right edge is still at the end of the data, which is what
                # "following" means - so the next frame threw the zoom away and
                # put 25 bars back. That is the "I cannot zoom in, it zooms
                # itself out" report, and it happened about thirty times a
                # second, which is why it felt like the chart was fighting.
                #
                # Following means keeping the newest bar in view. It does not
                # mean choosing the width. So the width is preserved and only
                # the position moves.
                n = len(bars)
                vb = pane.price_plot.getViewBox()
                vr = vb.viewRect()
                if vr.right() < n + 1:
                    w = vr.width()
                    # A degenerate width (first paint, or a pane that has never
                    # been sized) must not be preserved, or the chart sticks at
                    # whatever pyqtgraph happened to start with.
                    if not (w == w) or w < 2.0 or w > max(40.0, n * 4.0):
                        w = 25.0
                    right = n + 3
                    vb.setXRange(right - w, right, padding=0)
            if pane.header.isVisible():
                b = bars[-1]
                pane.lbl_last.setText(
                    f"{b.close:.2f}   Δ {b.delta:+,}")
            if pane is self._active_pane:
                self._update_stats(bars[-1])

    def _update_overlays(self, series: BarSeries, bars: list, pane) -> None:
        if not bars:
            pane.vwap_curve.setData([], [])
            pane.cvd_curve.setData([], [])
            for _, c in pane.vwap_bands:
                c.setData([], [])
            return
        tick = self.instruments.tick(pane.symbol)
        # One memoized pass over the bars' cached moments, not a full walk of
        # every price cell in the history on every frame.
        vy, vstd, cy = series.overlays(pane.tf_s, tick)
        xs = list(range(len(vy)))

        pane.vwap_curve.setData(xs, vy)
        show_bands = pane.vwap_curve.isVisible()
        for k, (mult, curve) in enumerate(pane.vwap_bands):
            sign = 1 if k % 2 == 0 else -1
            if show_bands and vstd:
                curve.setData(xs, [m + sign * mult * s
                                   for m, s in zip(vy, vstd)])
            else:
                curve.setData([], [])
        pane.cvd_curve.setData(list(range(len(cy))), cy)

    def _update_stats(self, bar) -> None:
        self.lbl_stats.setText(
            f"  {self.active_symbol}   {bar.close:.2f}   "
            f"Vol {bar.volume:,}   Δ {bar.delta:+,}   "
        )
        self._update_link()

    def _update_link(self) -> None:
        """Show live-feed status; blank for feeds that aren't Takion-based."""
        conn = getattr(self.feed, "connected", None)
        if not isinstance(conn, dict):
            return
        if "multicast" in conn:
            # Multicast has TWO states worth distinguishing, because a client
            # receiving the group without a reachable replay server is running
            # DEGRADED: a lost datagram then discards book state instead of
            # being repaired, and the chart opened with no history. That is
            # exactly the condition a trader needs told, not buried in a log.
            live = bool(conn.get("multicast"))
            replay = bool(conn.get("replay"))
            gaps = getattr(self.feed, "gaps", None)
            lost = gaps.lost if gaps is not None else 0
            unrep = getattr(self.feed, "unrepaired", 0)
            if live and replay:
                txt, col = f"● MULTICAST  {len(self.series)} sym", "#2E9E7E"
            elif live:
                txt, col = (f"● MULTICAST  {len(self.series)} sym  "
                            f"⚠ no replay (gaps cannot be repaired)"), "#E0A03C"
            else:
                txt, col = "○ waiting for the multicast group…", "#D4564F"
            if lost:
                repaired = getattr(self.feed, "repairs", 0)
                txt += f"   lost {lost:,} · repaired {repaired:,}"
                if unrep:
                    txt += f" · UNREPAIRED {unrep:,}"
                    col = "#D4564F"
        elif "network" in conn:
            live = bool(conn.get("network"))
            txt, col = ((f"● LIVE  {len(self.series)} sym", "#2E9E7E") if live
                        else ("○ waiting for the broadcaster…", "#D4564F"))
        else:
            l1, l2 = bool(conn.get("l1")), bool(conn.get("l2"))
            if l1 and l2:
                txt, col = f"● LIVE  {len(self.series)} sym", "#2E9E7E"
            elif l1 or l2:
                txt, col = (f"● PARTIAL  L1:{'✓' if l1 else '×'} "
                            f"L2:{'✓' if l2 else '×'}"), "#E0A03C"
            else:
                txt, col = "○ waiting for Takion…", "#D4564F"
        # How much of the delta is evidence and how much is an even split.
        # Every buy/sell figure on screen rests on this, so it belongs on the
        # status line rather than in a log nobody reads: if `?` is large, the
        # feed is not telling us who was aggressing and the deltas are weaker
        # than they look.
        q = getattr(self.feed, "quality", None)
        if callable(q):
            try:
                m = q()
                # ATTRIBUTED vs split-in-half, which is the distinction that
                # matters: everything except `unknown` was given a side.
                # The old line showed quote+mid as "known" against unknown as
                # "?", leaving the tick tiers in NEITHER - so the two numbers
                # did not sum to 100 and there was no way to read what the
                # remainder was. The quoted share is shown in brackets because
                # it is the part resting on direct evidence rather than
                # inference.
                unknown = m["unknown"]
                attributed = 1.0 - unknown
                quoted = m["quote"] + m["mid"]
                if unknown > 0.02 or quoted < 0.75:
                    txt += (f"   flow {attributed:.0%} attributed"
                            f" ({quoted:.0%} quoted)  ? {unknown:.0%}")
                    if unknown > 0.15:
                        col = "#E0A03C"
            except Exception:
                pass
        if self._dropped:
            # Visible, not silent: if the GUI cannot keep up you need to know the
            # chart is now an incomplete picture.
            txt += f"   ⚠ dropped {self._dropped:,}"
            col = "#E0A03C"
        # THE BACKFILL STATE IS PART OF THE TRUTH ABOUT THIS FEED. A chart
        # holding only what arrived since the app opened looks exactly like one
        # holding the whole session; the difference has to be on screen, not
        # only in a log file nobody reads until something has already gone
        # wrong.
        # NOT gated on HOLD_FOR_BACKFILL. Whether the stream is held is an
        # implementation choice; whether this chart yet holds the session is a
        # fact about the data, and the user needs it either way. Gating the
        # indicator on the hold hid it the moment the hold was removed.
        if self._bf_state in ("arming", "holding", "loading"):
            txt += "   ⏳ loading history…"
            # ON THE PANES TOO. The toolbar is one line at the top of one
            # window; a trader looking at a four-chart grid needs to know which
            # of those four is still filling in, not merely that something is.
            for p_ in self._panes[:self._n_panes]:
                if not p_.symbol or p_.symbol in self._bf_done:
                    continue
                if self._bf_want is None or p_.symbol in (self._bf_symbols
                                                          or self._bf_want or []):
                    p_.lbl_last.setText("loading history…")
        elif self._bf_state == "live_only":
            txt += "   ⚠ LIVE ONLY (no history)"
            col = "#E0A03C"
        self.lbl_link.setText(f"  {txt}  ")
        self.lbl_link.setStyleSheet(f"color:{col}; font-weight:700;")

    # ---- interactions ----------------------------------------------------
    def _on_symbol(self, sym: str) -> None:
        if sym:
            self.active_symbol = sym
            pane = self._active_pane
            if pane.sym_combo.currentText() != sym:
                pane.sym_combo.blockSignals(True)
                pane.sym_combo.setCurrentText(sym)
                pane.sym_combo.blockSignals(False)
            self.fp.tick = self.instruments.tick(sym)
            self.auto_scroll = True
            # Centre on the new symbol WITHOUT the user reaching for Alt+R.
            # This cannot be done here: a symbol selected before its first
            # print has no bars yet, and the old price range is meaningless
            # for the new instrument (QQQ at 400 and a $12 stock share an
            # axis). So arm it and let _redraw fire once bars exist.
            self._needs_center = True
            self._dirty = True

    # ---- TradingView-style ticker search --------------------------------
    # TradingView's keys, because they are the ones a trader's hands already
    # know. Digits pick a timeframe, letters arm a drawing tool, and the
    # navigation keys do what they do in every charting package.
    #
    # Bare letters are ALSO the ticker search, so every tool key is checked
    # BEFORE that fallback and none of them may be a plain letter that would
    # make typing a symbol impossible - hence Alt for the tools.
    TF_KEYS = {Qt.Key.Key_1: "1m", Qt.Key.Key_2: "2m", Qt.Key.Key_3: "3m",
               Qt.Key.Key_4: "5m", Qt.Key.Key_5: "15m", Qt.Key.Key_6: "30m",
               Qt.Key.Key_7: "1h", Qt.Key.Key_8: "4h", Qt.Key.Key_9: "1d",
               Qt.Key.Key_0: "10s"}
    MAGNET_PX = MAGNET_PX_DEFAULT

    MAGNET_KEY = Qt.Key.Key_N

    TOOL_KEYS = {Qt.Key.Key_T: "Trend", Qt.Key.Key_F: "Fib",
                 Qt.Key.Key_P: "Pen", Qt.Key.Key_M: "Measure",
                 Qt.Key.Key_L: "Long", Qt.Key.Key_S: "Short",
                 Qt.Key.Key_V: "VP", Qt.Key.Key_C: "CPR"}

    def keyPressEvent(self, ev) -> None:
        key = ev.key()
        mods = ev.modifiers()
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)

        # ---- timeframe: 1..9,0 like every charting package -----------------
        if key in self.TF_KEYS and not (alt or ctrl):
            if not self.sym_search.isVisible():
                tf = self.TF_KEYS[key]
                if self.tf_combo.findText(tf) >= 0:
                    self.tf_combo.setCurrentText(tf)
                return

        # ---- drawing tools, on Alt so bare letters stay the ticker search --
        if alt and key == self.MAGNET_KEY:
            self.chk_magnet.setChecked(not self.chk_magnet.isChecked())
            ev.accept()
            return

        if alt and key in self.TOOL_KEYS:
            t = self.TOOL_KEYS[key]
            if t in self._tool_buttons:
                # Pressing the armed tool's key again disarms it, which is
                # what the same key does everywhere else.
                self._set_drawing_tool(None if self.active_drawing_tool == t
                                       else t)
            return

        # ---- navigation ----------------------------------------------------
        pane = self._active_pane
        if pane is not None and not self.sym_search.isVisible():
            vb = pane.price_plot.getViewBox()
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                r = vb.viewRect()
                span = r.width()
                step = span * (0.5 if ctrl else 0.12)
                d = -step if key == Qt.Key.Key_Left else step
                pane.auto_scroll = False
                vb.setXRange(r.left() + d, r.right() + d, padding=0)
                return
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                       Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                f = 0.8 if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal) else 1.25
                r = vb.viewRect()
                c = r.center().x()
                half = r.width() * f / 2
                vb.setXRange(c - half, c + half, padding=0)
                return
            if key == Qt.Key.Key_End:
                pane.auto_scroll = True
                pane.auto_y = True
                self._center(pane)
                return
            if key == Qt.Key.Key_Home:
                s_ = self.series.get(pane.symbol)
                if s_ is not None:
                    n = len(s_.view(pane.tf_s))
                    pane.auto_scroll = False
                    vb.setXRange(0, max(3, min(n, 60)), padding=0)
                return

        # ---- panels and windows, on Ctrl ------------------------------------
        if ctrl:
            if key == Qt.Key.Key_B:
                self._open_bookmap()
                return
            if key == Qt.Key.Key_T:
                self._open_tape()
                return
            if key == Qt.Key.Key_P:
                self._open_profile()
                return
        # Alt+R re-centres. Tested before the ticker search, which otherwise
        # eats any bare letter - `ev.text()` for Alt+R is still "r".
        if key == Qt.Key.Key_R and mods & Qt.KeyboardModifier.AltModifier:
            self._center()
            return
        # Alt+H drops a price level where the crosshair is.
        if key == Qt.Key.Key_H and mods & Qt.KeyboardModifier.AltModifier:
            self._add_price_level()
            return
        # Alt+A arms a price ALERT there - a level that beeps.
        if key == Qt.Key.Key_A and mods & Qt.KeyboardModifier.AltModifier:
            self.add_alert_here()
            return
        # Alt+D shows or hides the CVD pane.
        if key == Qt.Key.Key_D and alt:
            self.chk_cvd.setChecked(not self.chk_cvd.isChecked())
            return
        # Alt+G steps through the chart grids.
        if key == Qt.Key.Key_G and alt:
            order = list(LAYOUTS)
            cur = self.layout_combo.currentText()
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order                 else order[0]
            self.layout_combo.setCurrentText(nxt)
            return
        # Delete/Backspace removes the selected drawing. Checked before the
        # ticker search so the shortcuts cannot be swallowed by it.
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._delete_selected_drawing():
                return
        if key == Qt.Key.Key_Escape:
            # Escape unwinds one step at a time: abandon a half-drawn shape,
            # else disarm the tool, else drop the selection.
            if self._drawing_start_point is not None or self._pen_points:
                self._cancel_draw()
            elif self.active_drawing_tool is not None:
                self._set_drawing_tool(None)
            elif self._selected_drawing is not None:
                self._select_drawing(None)
            return
        # Start typing a letter anywhere on the chart to open the ticker search.
        if not self.sym_search.isVisible():
            t = ev.text()
            if t and t.isalpha():
                self._open_sym_search(t.upper())
                return
        super().keyPressEvent(ev)

    def _open_sym_search(self, seed: str = "") -> None:
        se = self.sym_search
        gw = self.glw.width()
        se.move(max(8, (gw - se.width()) // 2), 12)   # top-centre of the chart
        se.setText(seed)
        se.show()
        se.raise_()
        se.setFocus()
        se.end(False)                                  # cursor to end

    def _apply_sym_search(self) -> None:
        sym = self.sym_search.text().strip().upper()
        self.sym_search.hide()
        self.glw.setFocus()
        if not sym:
            return
        if self.sym_combo.findText(sym) < 0:           # unknown yet - add it
            self._known_symbols.add(sym)
            self.sym_combo.addItem(sym)
        self.sym_combo.setCurrentText(sym)             # fires _on_symbol

    def eventFilter(self, obj, ev):
        if obj is self.sym_search and ev.type() == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Escape:
                self.sym_search.hide()
                self.glw.setFocus()
                return True
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, "sym_search", None) is not None and self.sym_search.isVisible():
            gw = self.glw.width()
            self.sym_search.move(max(8, (gw - self.sym_search.width()) // 2), 12)

    def _on_tf(self, txt: str) -> None:
        self._set_pane_tf(self._active_pane, txt)

    def _set_pane_tf(self, pane, txt: str, explicit: bool = True) -> None:
        """Timeframe of ONE chart. The other panes keep theirs."""
        pane.tf_s = TF_CHOICES.get(txt, 60)
        if explicit:
            pane.tf_explicit = True
        pane.auto_scroll = True
        # A different horizon reframes the whole chart - the old price window
        # described a different number of bars - so re-fit rather than leave
        # the previous timeframe's view.
        pane._needs_center = True
        if pane.tf_combo.currentText() != txt:
            pane.tf_combo.blockSignals(True)
            pane.tf_combo.setCurrentText(txt)
            pane.tf_combo.blockSignals(False)
        if pane is self._active_pane and self.tf_combo.currentText() != txt:
            self.tf_combo.blockSignals(True)
            self.tf_combo.setCurrentText(txt)
            self.tf_combo.blockSignals(False)
        self._dirty = True

    def apply_overlays(self) -> None:
        """Push every overlay switch onto EVERY pane.

        These used to act on `self.vwap_curve`, which is the ACTIVE pane's
        curve - so in a 2x2 grid the switch moved one chart and left the other
        three showing whatever their items happened to be constructed with,
        which for a PlotDataItem is visible. That is why VWAP appeared on the
        other charts however the menu was set, and why turning it off only ever
        cleared one of them.

        It also has to run at STARTUP and after a layout change. `_act` sets
        the action's checked state before connecting `toggled`, so the initial
        value never fired a handler and a default of off was stored but never
        applied - and a pane created later starts from its own constructor
        defaults, not from the menu.
        """
        vwap = self.chk_vwap.isChecked()
        cpr = self.chk_cpr.isChecked()
        ema = self.chk_ema.isChecked()
        for pane in self._panes:
            pane.vwap_curve.setVisible(vwap)
            for _mult, c in pane.vwap_bands:
                c.setVisible(vwap)
            pane.cpr_item.setVisible(cpr)
            pane.ema9_item.setVisible(ema)
            pane.ema21_item.setVisible(ema)
        self._dirty = True

    def _on_vwap_toggled(self, _on: bool) -> None:
        self.apply_overlays()

    def _on_cpr_toggled(self, _on: bool) -> None:
        self.apply_overlays()

    def _on_ema_toggled(self, _on: bool) -> None:
        self.apply_overlays()

    def _on_mode(self, name: str) -> None:
        """The toolbar drives the ACTIVE pane; the pane header mirrors it."""
        pane = self._active_pane
        pane.set_mode(*MODES.get(name, ("Footprint", True, False)))
        if pane.mode_combo.currentText() != name:
            pane.mode_combo.blockSignals(True)
            pane.mode_combo.setCurrentText(name)
            pane.mode_combo.blockSignals(False)
        self._dirty = True

    def _bookmap(self, sym: str) -> BookmapBuffer:
        b = self.bookmaps.get(sym)
        if b is None:
            b = self.bookmaps[sym] = BookmapBuffer(sym, self.instruments)
        return b

    def _profile(self, sym: str) -> SessionProfile:
        p = self.profiles.get(sym)
        if p is None:
            p = self.profiles[sym] = SessionProfile(sym, self.instruments)
        return p

    def _register_child(self, key: str, win) -> None:
        """Show a child window, keeping exactly one alive per key.

        Every child runs its own refresh QTimer (the Bookmap's fires at 80 ms).
        The old code appended to a list that was never pruned and explicitly
        disabled WA_DeleteOnClose, so clicking a toolbar button ten times left
        ten windows redrawing heatmaps forever, all pinned in memory. Closing a
        window now destroys it — which stops its timers — and re-opening reuses
        the live one instead of stacking another.
        """
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._child_windows[key] = win
        win.destroyed.connect(lambda *_, k=key: self._child_windows.pop(k, None))
        win.show()
        win.raise_()
        win.activateWindow()

    def _child(self, key: str):
        return self._child_windows.get(key)

    def _open_profile(self) -> None:
        sym = self.active_symbol or "QQQ"
        if (w := self._child(f"profile:{sym}")) is not None:
            w.raise_(); w.activateWindow(); return
        win = ProfileWindow(self._profile(sym), self.instruments.tick(sym), self)
        self._register_child(f"profile:{sym}", win)
        win._fit()

    def _open_analytics(self) -> None:
        sym = self.active_symbol or "QQQ"
        if (w := self._child(f"analytics:{sym}")) is not None:
            w.raise_(); w.activateWindow(); return
        win = AnalyticsWindow(self._bookmap(sym), self.instruments.tick(sym), self)
        self._register_child(f"analytics:{sym}", win)

    def _open_dom(self) -> None:
        if (w := self._child("dom")) is not None:
            w.raise_(); w.activateWindow(); return
        self._register_child("dom", DomLadderWindow(self, self))

    def _open_monitor(self) -> None:
        if (w := self._child("monitor")) is not None:
            w.raise_(); w.activateWindow(); return
        self._register_child("monitor", MarketMonitorWindow(self, self))

    def _open_tape(self) -> None:
        sym = self.active_symbol
        if not sym:
            return
        if (w := self._child(f"tape:{sym}")) is not None:
            w.raise_(); w.activateWindow(); return
        win = TapeWindow(self._bookmap(sym), self.instruments.tick(sym), self)
        self._register_child(f"tape:{sym}", win)

    def _open_bookmap(self) -> None:
        self.open_bookmap_for(self.active_symbol or "QQQ")

    def open_bookmap_for(self, sym: str) -> None:
        """Show `sym`'s book, reusing the ONE bookmap window.

        Public because the Bookmap window's own ticker search calls back into
        it: the per-symbol buffers and the child-window registry live here, and
        a window constructed anywhere else would bypass both.

        One window, not one per symbol. The window shows up to four books in a
        resizable grid, so a second symbol fills a free pane rather than
        opening another window to be tiled by hand. If every visible pane is
        already taken, the selected one is repointed - that is what asking for
        a symbol while looking at a full grid means.
        """
        sym = (sym or "").strip().upper()
        if not sym:
            return
        if sym not in self._known_symbols:
            self._register_symbol(sym)
        win = self._child("bookmap")
        if win is None:
            win = BookmapWindow(self._bookmap(sym), self.instruments.tick(sym), self)
            self._register_child("bookmap", win)
            return
        # Already showing it? Just select that pane.
        for pane in win._visible_panes():
            if pane.buffer.symbol == sym:
                win._select_pane(pane)
                win.raise_(); win.activateWindow()
                return
        target = next((p for p in win._visible_panes() if not p.buffer.symbol),
                      win._active_pane)
        win.set_pane_symbol(target, sym)
        win._select_pane(target)
        win.raise_(); win.activateWindow()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if not dlg.exec():
            return
        v = dlg.values()
        sym = self.active_symbol
        if sym and v["tick"] != self.instruments.tick(sym):
            self.instruments.set_tick(sym, v["tick"])
            self.fp.tick = v["tick"]
            self.heatmap.tick = v["tick"]
            # A tick change reshapes the price->index map, so EVERY structure
            # keyed by tick index is now wrong — not just the bar series. The
            # old code rebuilt `series` alone and left the bookmap buffer, the
            # session profile and any open child window holding indices from the
            # previous tick, which silently mis-priced the DOM and the profile.
            self.series.pop(sym, None)
            self.bookmaps.pop(sym, None)
            self.profiles.pop(sym, None)
            self.latest_book.pop(sym, None)
            for key in [k for k in self._child_windows if k.endswith(f":{sym}")]:
                w = self._child_windows.pop(key, None)
                if w is not None:
                    w.close()
            # Drop the rendered bars too. They were indexed under the old tick,
            # and _redraw() bails out until the series is rebuilt — leaving the
            # stale block on screen priced at the new tick.
            self.fp.set_bars([])
            self.heatmap.set_bars([])
            # A tick change reprices the axis; re-fit rather than leave the
            # view framed for the old grid.
            self._needs_center = True
        self.fp.configure(
            imbalance_factor=v["imbalance_factor"],
            min_imbalance_vol=v["min_imbalance_vol"],
            stacked_min=v["stacked_min"],
            va_pct=v["va_pct"],
            show_candles=v["show_candles"],
            cell_style=v["cell_style"],
        )
        self.heatmap.alpha = v["hm_alpha"]
        self.heatmap.gamma = v["hm_gamma"]
        # colour overrides -> new immutable theme
        self.theme = replace(self.theme, bull=v["bull"], bear=v["bear"],
                             buy_imb=v["buy_imb"], sell_imb=v["sell_imb"])
        self._apply_theme()
        self._dirty = True

    def _on_theme(self, txt: str) -> None:
        self.theme = DARK if txt == "Dark" else LIGHT
        self._apply_theme()
        self._dirty = True

    # _time_at lived here until the chart moved into ChartPane, which took the
    # crosshair with it. The copy left behind was dead AND broken - it called
    # safe_localtime without importing it, so anything that had reached it
    # would have raised NameError. Deleted rather than repaired: the live one
    # is ChartPane._time_at, and a second implementation of a label the user
    # reads off the time axis is how the two quietly drift apart.

    def _place_xhair_badges(self) -> None:
        self.xhair.place()

    def _hide_xhair_badges(self) -> None:
        self._last_cursor = None
        self.xhair.hide()

    def _select_pane(self, pane) -> None:
        """Make `pane` the one the toolbar and drawing tools act on."""
        if pane is self._active_pane or pane not in self._visible_panes():
            return
        self._bind_pane(pane)
        # The toolbar must follow the selection, not the other way round, or
        # clicking a chart would silently retarget it to the wrong symbol.
        if pane.symbol:
            self.sym_combo.blockSignals(True)
            if self.sym_combo.findText(pane.symbol) >= 0:
                self.sym_combo.setCurrentText(pane.symbol)
            self.sym_combo.blockSignals(False)
        self._dirty = True

    def _on_mouse_move(self, pos, pane=None) -> None:
        pane = pane or self._active_pane
        # Each pane owns its scene, so a move here IS a move on this pane -
        # no hit-testing against sibling charts, and the crosshair can never
        # end up on the wrong one.
        if pane is not self._active_pane:
            return
        if not self.price_plot.sceneBoundingRect().contains(pos):
            # Leaving the chart must clear the readouts, or they sit there
            # asserting a price the pointer is no longer on.
            self._hide_xhair_badges()
        if self.price_plot.sceneBoundingRect().contains(pos):
            mp = self.price_plot.vb.mapSceneToView(pos)
            self.vline.setPos(mp.x())
            self.hline.setPos(mp.y())
            self._last_cursor = (mp.x(), mp.y())
            self.xhair.set(mp.x(), mp.y())
            if self.active_drawing_tool == "Pen":
                self._pen_move(mp)
                return
            if self._drawing_start_point is not None:
                self._update_preview(mp)

    # ---- freehand pen ----------------------------------------------------
    def _pen_move(self, mp) -> None:
        """Collect stroke points while the left button is held.

        pyqtgraph only publishes `sigMouseClicked`, which fires on RELEASE, so
        there is no press event to start a stroke from. The button state comes
        from the application instead: a move with the left button down is a
        stroke in progress, and the click that arrives on release ends it.
        """
        from PyQt6.QtWidgets import QApplication
        down = bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)
        if not down:
            return
        pt = (mp.x(), mp.y())
        if self._pen_points is None:
            self._pen_points = [pt]
            return
        # Thin the stroke: a raw move stream puts hundreds of points on one
        # pixel, which costs paint time and gains no detail.
        px_w, px_h = self.price_plot.vb.viewPixelSize()
        lx, ly = self._pen_points[-1]
        if (abs(pt[0] - lx) >= px_w * 2.0) or (abs(pt[1] - ly) >= px_h * 2.0):
            self._pen_points.append(pt)

    def _pen_finish(self) -> bool:
        """Commit the stroke on mouse release. True if one was created."""
        pts = self._pen_points
        self._pen_points = None
        if not pts or len(pts) < 2:
            return False
        item = PenDrawing(pts)
        self._add_drawing(item)
        return True

    def _on_mouse_click(self, ev, pane=None) -> None:
        # Clicking a chart selects it. Done before anything else so the very
        # first click on a background pane retargets the tools rather than
        # drawing on the pane you were previously using.
        if pane is not None and pane is not self._active_pane:
            self._select_pane(pane)
            return
        pos = ev.scenePos()
        if not self.price_plot.sceneBoundingRect().contains(pos):
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return

        # Not drawing: a click on empty chart clears the selection. Clicks that
        # land on a drawing never reach here (the ROI accepts them and emits
        # sigClicked), so this cannot deselect the thing just clicked.
        if not self.active_drawing_tool:
            if self._selected_drawing is not None:
                self._select_drawing(None)
            return

        # The pen draws on drag, so this click is its mouse-release: commit the
        # stroke here rather than treating it as a corner of a box.
        if self.active_drawing_tool == "Pen":
            self._pen_finish()
            return

        mp = self._magnet(self.price_plot.vb.mapSceneToView(pos))
        # A level needs one click, not two - waiting for a second would leave a
        # rubber band on screen with nothing to rubber-band.
        if self.active_drawing_tool == "HLine":
            self._add_price_level(mp.y())
            return

        if self._drawing_start_point is None:
            self._drawing_start_point = mp
            self._preview.setVisible(True)
            self._update_preview(mp)
            return

        start, end = self._drawing_start_point, mp
        self._cancel_draw()
        # Degenerate box: a double-click or a stray second click at the same
        # spot used to create a zero-size ROI that could not be grabbed or
        # removed. Treat it as a cancel.
        if abs(end.x() - start.x()) < 1e-9 and abs(end.y() - start.y()) < 1e-9:
            return

        p1 = [start.x(), start.y()]
        p2 = [end.x(), end.y()]          # two POINTS — never a delta
        tool = self.active_drawing_tool
        if tool == "Fib":
            item = FibRetracement(p1, p2)
        elif tool == "Long":
            item = PositionDrawer(p1, p2, is_long=True)
        elif tool == "Short":
            item = PositionDrawer(p1, p2, is_long=False)
        elif tool == "VP":
            item = FixedVolumeProfile(p1, p2, self._get_bars_for_vp,
                                      self.instruments.tick(self.active_symbol))
        elif tool == "CPR":
            item = CprDrawing(p1, p2, self._get_bars_for_vp)
        elif tool == "Measure":
            item = MeasureTool(p1, p2, self._get_bars_for_vp)
        else:
            return

        self._add_drawing(item)

    def _add_price_level(self, price: float | None = None) -> bool:
        """Horizontal level at the crosshair (Alt+H).

        Falls back to the centre of the view when the pointer has never entered
        the chart - pressing the shortcut should always produce a line you can
        then drag, rather than silently doing nothing.
        """
        if price is None:
            if self._last_cursor is not None:
                price = self._last_cursor[1]
            else:
                y0, y1 = self.price_plot.vb.viewRange()[1]
                price = (y0 + y1) / 2.0
        self._add_drawing(PriceLevel(float(price)))
        return True

    def _add_drawing(self, item) -> None:
        """Register a finished drawing: add, wire, select, disarm."""
        self.price_plot.addItem(item)
        self.drawing_items.append(item)
        item.sigRemoveRequested.connect(self._remove_drawing)
        item.sigClicked.connect(lambda it, _e: self._select_drawing(it))
        self._select_drawing(item)
        self._set_drawing_tool(None)          # auto-revert to the cursor

    # ---- drawing selection / lifecycle -----------------------------------
    def _select_drawing(self, item) -> None:
        if self._selected_drawing is item:
            return
        prev = self._selected_drawing
        if prev is not None and prev in self.drawing_items:
            prev.set_selected(False)
        self._selected_drawing = item
        if item is not None:
            item.set_selected(True)

    def _remove_drawing(self, item) -> None:
        if item is self._selected_drawing:
            self._selected_drawing = None
        if item in self.drawing_items:
            self.drawing_items.remove(item)
        self.price_plot.removeItem(item)

    def _delete_selected_drawing(self) -> bool:
        if self._selected_drawing is None:
            return False
        self._remove_drawing(self._selected_drawing)
        return True

    def _cancel_draw(self) -> None:
        """Drop a half-drawn shape or an in-progress stroke, hide the preview."""
        self._drawing_start_point = None
        self._pen_points = None
        self._preview.setVisible(False)

    def _on_magnet_toggled(self, on: bool) -> None:
        self.magnet_on = bool(on)

    def _magnet(self, mp):
        """Snap a drawing point to the nearest bar extreme, if magnet is on.

        TradingView's single most-used drawing behaviour, and the reason a
        trend line drawn by hand never quite touches the high it was drawn to
        touch. A line anchored a few cents off the wick is not the level the
        trader meant; it is the level their mouse managed.

        Snaps to the OHLC of the bar under the cursor - never to an arbitrary
        price - and only when the cursor is ALREADY within MAGNET_PX of one.
        Outside that radius the raw point is returned unchanged, so the magnet
        assists a near-miss and never fights a deliberate placement. That
        threshold is the whole difference between a magnet and a straitjacket.
        """
        if not self.magnet_on:
            return mp
        s = self.series.get(self.active_symbol) if self.active_symbol else None
        if s is None:
            return mp
        bars = s.view(self.tf_s)
        if not bars:
            return mp
        i = int(round(mp.x()))
        if not (0 <= i < len(bars)):
            return mp
        bar = bars[i]
        try:
            _px_w, px_h = self.price_plot.vb.viewPixelSize()
        except Exception:
            return mp
        if not px_h:
            return mp
        y = mp.y()
        # Bar index is the x axis, so snapping x to the bar centre is exact and
        # costs nothing - a drawing that lands between two bars is ambiguous
        # about which one it refers to.
        best = min((bar.open, bar.high, bar.low, bar.close),
                   key=lambda v: abs(v - y))
        if abs(best - y) / px_h > self.MAGNET_PX:
            return mp
        return QPointF(float(i), float(best))

    def _update_preview(self, mp) -> None:
        """Rubber band from the first click to the cursor.

        Without it the first click produced no feedback at all, so the tool felt
        dead until the second click committed a shape whose extent had been pure
        guesswork.
        """
        s = self._drawing_start_point
        if s is None:
            return
        x0, x1 = sorted((s.x(), mp.x()))
        y0, y1 = sorted((s.y(), mp.y()))
        self._preview.setRect(QRectF(x0, y0, max(x1 - x0, 1e-9),
                                     max(y1 - y0, 1e-9)))

    def _on_numbers(self, on: bool) -> None:
        self.fp.show_numbers = on
        self.fp.update()

    def _on_price_step(self, txt: str) -> None:
        self.fp.price_step = PRICE_STEPS.get(txt, 0.0)
        self.lbl_step.setText("")          # refreshed after the next paint
        self.fp.update()

    def _sync_step_label(self) -> None:
        """Report the step Auto chose.

        Read after the draw rather than set from inside `paint()` - touching a
        widget from a paint handler is how you get a repaint loop. One frame of
        lag on a readout is not worth that risk.
        """
        if self.fp.price_step > 0:
            self.lbl_step.setText("")      # the combo already names it
            return
        px = self.fp.step_ticks * self.fp.tick
        self.lbl_step.setText(f"({px * 100:.0f}¢)" if px < 1.0
                              else f"(${px:,.2f})".replace(".00", ""))

    def _on_fills(self, on: bool) -> None:
        self.exec_item.setVisible(on)
        self._dirty = True

    def _on_cvd_pane(self, on: bool) -> None:
        """Show/hide the cumulative-delta sub-chart on the ACTIVE pane."""
        self._active_pane.set_cvd_visible(on)

    def _get_bars_for_vp(self, x_min, x_max):
        s = self.series.get(self.active_symbol) if self.active_symbol else None
        if s is None:
            return []
        bars = s.view(self.tf_s)
        # filter bars within x_min and x_max
        # Since x-axis is bar index:
        start_idx = max(0, int(x_min))
        end_idx = min(len(bars), int(x_max) + 1)
        return bars[start_idx:end_idx]

    def _set_drawing_tool(self, tool_name):
        self.active_drawing_tool = tool_name
        self._cancel_draw()
        for tool, btn in self._tool_buttons.items():
            btn.setChecked(tool == tool_name)
        # Stay in PanMode throughout.
        #
        # Arming a tool used to switch the ViewBox to RectMode, which made a
        # left-drag draw a zoom rectangle instead of the shape - so the chart
        # jumped to a random zoom in the middle of drawing. Creation here is
        # click, move, click; the ViewBox never needs to change behaviour, and
        # the chart stays pannable while a tool is armed.
        vb = self.price_plot.getViewBox()
        vb.setMouseMode(pg.ViewBox.PanMode)
        # The pen is the one tool that DOES drag, so panning has to be off while
        # it is armed - otherwise the chart slides out from under the stroke and
        # the points land on prices the user never touched. Restored on disarm.
        pen = tool_name == "Pen"
        vb.setMouseEnabled(x=not pen, y=not pen)
        self._pen_points = None
        self.glw.setCursor(Qt.CursorShape.ArrowCursor if tool_name is None
                           else Qt.CursorShape.CrossCursor)

    def _clear_drawings(self):
        for item in list(self.drawing_items):
            self.price_plot.removeItem(item)
        self.drawing_items.clear()
        self._selected_drawing = None
        self._set_drawing_tool(None)

    def _on_view(self, pane=None) -> None:
        pane = pane or self._active_pane
        s = self.series.get(pane.symbol) if pane.symbol else None
        if s is None:
            return
        bars = s.view(pane.tf_s)
        vr = pane.price_plot.getViewBox().viewRect()
        pane.auto_scroll = vr.right() >= len(bars) - 1.0
        # sigRangeChangedManually only fires for a USER pan or zoom, so this is
        # a reliable "they took the wheel" signal. Their vertical zoom is now
        # theirs until they ask for it back.
        pane.auto_y = False
        # Touching a chart is also how you choose it in a grid.
        if pane is not self._active_pane:
            self._select_pane(pane)
        # HUNG OFF sigRangeChangedManually, not sigXRangeChanged. The latter
        # also fires on the auto-scroll setXRange this class issues every
        # frame, so it would restart the debouncer forever and either never
        # fire or fire constantly. This one means a human moved the view.
        self._history_pane = pane
        self._history_timer.start()

    # ---- startup backfill --------------------------------------------------
    def arm_startup_backfill(self, symbols=None) -> bool:
        """Take the hold NOW, then wait for the seam.

        Called straight after start_feed(). The order matters and is not
        obvious: MulticastFeed buffers for a second before it can publish
        first_live_seq, and if the stream were left running during that second
        the drain would build bars from it - after which the swap correctly
        refuses to replace a series that has already counted live trades, and
        the backfill would fail every single time on a real cold start.

        So the hold is taken before the seam is known. Nothing is processed,
        nothing is counted, and the slot the worker builds into stays empty.
        """
        if self._bf_state not in ("idle",):
            return False
        if not getattr(self.feed, "replay_host", ""):
            self._bf_state = "live_only" if getattr(self.feed, "connected", None)                 else "done"
            return False
        self._bf_want = list(symbols) if symbols is not None else None
        self._bf_since = time.monotonic()
        self._bf_dropped_at = self._dropped
        self._bf_state = "arming"
        self._bf_arm_timer = QTimer(self)
        self._bf_arm_timer.setInterval(150)
        self._bf_arm_timer.timeout.connect(self._bf_poll_seam)
        self._bf_arm_timer.start()
        log.info("startup backfill: holding from the first tick, waiting for "
                 "the multicast seam")
        return True

    def _bf_poll_seam(self) -> None:
        """The seam appears asynchronously; start the load the moment it does."""
        if self._bf_state != "arming":
            self._bf_arm_timer.stop()
            return
        seam = dict(getattr(self.feed, "first_live_seq", {}) or {})
        if seam:
            self._bf_arm_timer.stop()
            # Only what is ON SCREEN. Four or five names is a few seconds; the
            # other 990 stay on demand, because a mass reconnect of 100 clients
            # each pulling a session is the one moment the LAN cannot take it.
            syms = self._bf_want
            if syms is None:
                syms = sorted({p.symbol for p in self._panes[:self._n_panes]
                               if p.symbol})
            self._bf_state = "idle"          # let begin_ take it properly
            if not self.begin_startup_backfill(syms):
                self._bf_state = "live_only" if syms else "done"
            return
        if time.monotonic() - self._bf_since > BACKFILL_SEAM_WAIT_S:
            self._bf_arm_timer.stop()
            log.warning("startup backfill: no multicast seam after %.0fs - "
                        "LIVE ONLY", BACKFILL_SEAM_WAIT_S)
            self._bf_state = "live_only"

    def begin_startup_backfill(self, symbols=None) -> bool:
        """Hold the live stream and load today's history for `symbols`.

        Only the symbols bound to VISIBLE panes, by default. Four or five
        names is a few seconds and a few hundred MB; the other 990 stay on
        demand, because a mass reconnect of 100 clients all pulling a full
        session at once is the one moment the LAN cannot absorb it.

        Returns whether the hold was actually taken.
        """
        if self._bf_state in ("holding", "loading"):
            return False
        seam = dict(getattr(self.feed, "first_live_seq", {}) or {})
        if not seam or not getattr(self.feed, "replay_host", ""):
            # No seam means no multicast join, and without an exact boundary
            # there is nothing to be exact about - a timestamp guess can both
            # gap and overlap, and an overlap is silent double counting.
            self._bf_state = "live_only"
            return False
        syms = [x for x in (symbols if symbols is not None
                            else self._hot_symbols()) if x]
        if not syms:
            self._bf_state = "done"
            return False
        self._bf_seam = seam
        self._bf_symbols = list(syms)
        self._bf_since = time.monotonic()
        self._bf_dropped_at = self._dropped
        # NO HOLD. This used to stop draining the live queue until every
        # symbol's history had landed - up to BACKFILL_HOLD_MAX_S seconds of a
        # terminal that repaints but shows nothing new, which is exactly what
        # "the application freezes while it is downloading" describes. The
        # server log showed the same symbol pulled three times in two minutes:
        # a user killing and restarting a frozen app.
        #
        # The hold existed for a real reason - installing a replayed series
        # over one that had already counted live trades would lose those
        # trades. prepend_history removes that reason: it takes only bars
        # STRICTLY OLDER than the oldest live bar, so the two can never
        # describe the same bucket and the live stream can keep running the
        # whole time. See _on_backfill_built.
        self._bf_state = "loading"
        self._bf_done = set()
        self._bf_failed = set()
        self._bf_fetchers = {}
        # CLAIM THEM, so the session path does not fetch the same symbols at
        # the same time. The server log showed NVDA pulled twice concurrently -
        # two sockets, two 15.5 MB transfers, two full-day model builds holding
        # the GIL - because _sync_hot asks for the history of every symbol on
        # screen and the startup backfill was already asking for exactly those.
        self._sess_done.update(syms)
        now_ms = int(time.time() * 1000)
        day_ms = now_ms - int(BACKFILL_SESSION_H * 3600 * 1000)
        # L2 only as far back as the column ring can hold - anything older
        # would be transferred so the buffer could discard it.
        l2_ms = now_ms - int(BACKFILL_L2_MIN * 60 * 1000)
        for sym in syms:
            f = StartupFetcher(getattr(self.feed, "replay_host", ""),
                               getattr(self.feed, "replay_port", 9998),
                               getattr(self.feed, "token", ""), sym,
                               day_ms, now_ms, self.instruments,
                               seam=seam, l2_start_ms=l2_ms, parent=self)
            f.built.connect(self._on_backfill_built)
            f.failed.connect(self._on_backfill_failed)
            self._bf_fetchers[sym] = f
            f.start()
        log.info("startup backfill: holding live stream, seam=%s, symbols=%s",
                 seam, syms)
        return True

    def _on_backfill_built(self, symbol, series, buf, rep) -> None:
        """GUI thread. Swap in the finished objects, then catch up.

        THE SWAP IS ONLY SAFE INTO AN EMPTY SLOT. A series that has already
        counted live trades holds volume, delta and session figures the
        replayed one knows nothing about, and replacing it would discard them
        with no error and no way to notice - the chart would simply be short.
        The hold exists precisely so this slot IS empty; if it is not, that
        assumption has broken somewhere and the right answer is to keep what
        is real and refuse the load.
        """
        if not rep.get("trades") and not rep.get("books"):
            # NOTHING CAME BACK. Swapping in an empty series would replace a
            # live one with less than it had and report success; the honest
            # answer is that this symbol has no history.
            log.warning("startup backfill: %s returned no data (%s) - "
                        "no history for it", symbol, rep)
            self._bf_failed.add(symbol)
            for p_ in self._panes:
                if p_.symbol == symbol:
                    p_.lbl_last.setText("no history")
            self._bf_done.add(symbol)
            self._maybe_finish_backfill()
            return
        old = self.series.get(symbol)
        if old is not None and old.bars:
            # MERGE, do not replace. Replacing would discard the live trades
            # this series has already counted; taking only the strictly older
            # bars keeps both, and is why the live stream no longer has to be
            # held while the download runs.
            try:
                got = old.prepend_history(series)
            except Exception:
                log.exception("startup backfill: merging %s failed", symbol)
                self._bf_done.add(symbol)
                self._maybe_finish_backfill()
                return
            if got.get("added"):
                try:
                    pv = self.profiles.get(symbol)
                    if pv is not None:
                        pv.add_bars(old.bars[:got["added"]], old.base_tf_s)
                except Exception:
                    log.exception("startup backfill: profile merge failed "
                                  "for %s", symbol)
                self._dirty = True
            # THE HEAT FIELD TOO. Merging only the bars left the bookmap with
            # whatever the live stream had managed - ten columns on a cold
            # start - next to a chart showing the whole session.
            try:
                live_buf = self.bookmaps.get(symbol)
                if live_buf is None:
                    self.bookmaps[symbol] = buf
                    buf.set_hot(True)
                    got["columns"] = len(buf.order)
                else:
                    got["columns"] = live_buf.prepend_columns(buf).get("added", 0)
            except Exception:
                log.exception("startup backfill: heat merge failed for %s",
                              symbol)
            log.info("startup backfill: %s merged %s", symbol, got)
            self._sess_done.add(symbol)      # the session path need not repeat it
            self._bf_done.add(symbol)
            self._maybe_finish_backfill()
            return
        # The old instances are empty; dropping the reference here is the whole
        # of the release - they are refcounted, not collected, so there is no
        # RSS spike and nothing for the cycle collector to find.
        self.series[symbol] = series
        self.bookmaps[symbol] = buf
        series.set_hot(True)
        buf.set_hot(True)
        log.info("startup backfill: %s loaded %s", symbol, rep)
        for p_ in self._panes:
            if p_.symbol == symbol:
                p_.lbl_last.setText("")
        self._bf_done.add(symbol)
        self._maybe_finish_backfill()

    # ---- session history for a symbol selected LATER ----------------------
    def request_session_history(self, symbol: str) -> bool:
        """Fetch this symbol's session and MERGE the part older than we have.

        The startup backfill only ever covered `_hot_symbols()` - the one or
        two symbols on screen when the app opened - and refused to install
        anything into a slot that had already counted live bars. On a multicast
        feed carrying the whole basket that is every symbol within seconds, so
        selecting a symbol later gave a chart that began when the APP began,
        with no session behind it. That is the missing footprint and profile
        history.

        Merging is safe where replacing was not: prepend_history takes only
        bars strictly OLDER than the oldest live bar, so the replay and the
        live stream never describe the same bucket and nothing is double
        counted. See BarSeries.prepend_history.

        L2 is deliberately NOT re-fetched here. The heat ring holds about
        twenty minutes and the operator has said that is enough; pulling hours
        of depth to have the buffer discard it is transfer nobody sees.
        """
        if not symbol:
            return False
        host = getattr(self.feed, "replay_host", "")
        if not host:
            return False
        if symbol in self._sess_fetchers or symbol in self._sess_done:
            return False
        if symbol in getattr(self, "_bf_fetchers", {}):
            return False        # the startup backfill owns this one
        # CAP THE CONCURRENCY, QUEUE THE REST.
        #
        # Each fetch is a thread pulling a session over TCP - measured on the
        # operator's own server at 7.7 MB in 3.95 s for one symbol. A 2x2 chart
        # grid plus a 2x2 book grid puts eight symbols on screen at once, and a
        # workspace restore binds them in the same instant, so without a cap
        # eight threads hit the server simultaneously on the very frame the
        # terminal opens - each one slower for the others being there, and all
        # of them competing with the live multicast decode for the GIL.
        #
        # Serialising them costs nothing the user can perceive: history fills
        # in behind a chart that is already live, and the symbol being traded
        # is fetched first because _hot_symbols yields it first.
        if len(self._sess_fetchers) >= MAX_SESSION_FETCHES:
            if symbol not in self._sess_queue:
                self._sess_queue.append(symbol)
            return False
        now_ms = int(time.time() * 1000)
        day_ms = now_ms - int(BACKFILL_SESSION_H * 3600 * 1000)
        f = StartupFetcher(host, getattr(self.feed, "replay_port", 9998),
                           getattr(self.feed, "token", ""), symbol,
                           day_ms, now_ms, self.instruments,
                           seam=None, l2_start_ms=now_ms, parent=self)
        f.built.connect(self._on_session_built)
        f.failed.connect(self._on_session_failed)
        self._sess_fetchers[symbol] = f
        f.start()
        log.info("session history: fetching %s", symbol)
        return True

    def _on_session_built(self, symbol, series, buf, rep) -> None:
        """GUI thread. Splice the older part in front of what is live."""
        self._sess_fetchers.pop(symbol, None)
        self._sess_done.add(symbol)
        live = self.series.get(symbol)
        if live is None:
            self.series[symbol] = series
            series.set_hot(True)
            log.info("session history: %s installed whole (%s)", symbol, rep)
            self._dirty = True
            self._pump_session_queue()
            return
        try:
            got = live.prepend_history(series)
        except Exception:
            log.exception("session history: merging %s failed", symbol)
            return
        try:
            lb = self.bookmaps.get(symbol)
            if lb is not None and buf is not None:
                got["columns"] = lb.prepend_columns(buf).get("added", 0)
        except Exception:
            log.exception("session history: heat merge failed for %s", symbol)
        log.info("session history: %s merged %s", symbol, got)
        if got.get("added"):
            # THE PROFILE GETS IT TOO. It is fed only from the live drain loop,
            # so without this a backfilled symbol had a chart going back hours
            # and a volume profile that began when the app did - and the
            # profile is one of the main reasons to want the history at all.
            n = got["added"]
            try:
                pv = self.profiles.get(symbol)
                if pv is not None:
                    got["profile_vol"] = pv.add_bars(live.bars[:n],
                                                     live.base_tf_s)
            except Exception:
                log.exception("session history: profile merge failed for %s",
                              symbol)
            self._dirty = True
        self._pump_session_queue()

    def _on_session_failed(self, symbol, err) -> None:
        self._sess_fetchers.pop(symbol, None)
        self._sess_done.add(symbol)
        log.warning("session history: %s unavailable (%s)", symbol, err)
        self._pump_session_queue()

    def _pump_session_queue(self) -> None:
        """Start whatever the cap was holding back.

        Symbols that have since left the screen are dropped rather than
        fetched: by the time a slot frees, a queue built during a workspace
        restore can be full of symbols nobody is looking at any more, and
        spending a session transfer on those is spending it on nothing.
        """
        if not self._sess_queue:
            return
        hot = self._hot_symbols()
        while self._sess_queue and len(self._sess_fetchers) < MAX_SESSION_FETCHES:
            sym = self._sess_queue.pop(0)
            if sym in hot and sym not in self._sess_done:
                self.request_session_history(sym)

    def _on_backfill_failed(self, symbol, err) -> None:
        log.warning("startup backfill: %s unavailable (%s)", symbol, err)
        for p_ in self._panes:
            if p_.symbol == symbol:
                p_.lbl_last.setText("no history")
        self._bf_done.add(symbol)
        self._bf_failed.add(symbol)
        self._maybe_finish_backfill()

    def _maybe_finish_backfill(self) -> None:
        """Mark the load complete once every symbol has answered."""
        if self._bf_state not in ("holding", "loading"):
            return
        if not set(self._bf_symbols) <= self._bf_done:
            return
        self._bf_fetchers = {}
        if self._bf_failed and len(self._bf_failed) == len(self._bf_symbols):
            # Nothing loaded at all: say LIVE ONLY rather than imply history.
            self._bf_state = "live_only"
            self._bf_symbols = []
            log.warning("startup backfill: no symbol loaded - LIVE ONLY")
            return
        self.release_backfill()

    def _check_backfill_hold(self) -> None:
        """Give up honestly rather than hold the chart forever.

        Three ways out, and all of them end in a chart that is either complete
        or plainly labelled - never one that looks complete and is not.
        """
        held = len(self._event_q)
        if self._dropped > self._bf_dropped_at:
            # The queue wrapped, so live events are already gone from the
            # front. The seam can no longer be honoured and no amount of
            # history would make the result correct.
            self._abort_backfill(
                f"the live queue overflowed ({self._dropped - self._bf_dropped_at} "
                f"events lost) - the seam is broken")
        elif held >= BACKFILL_HOLD_MAX_EVENTS:
            self._abort_backfill(f"{held:,} live events held, cap is "
                                 f"{BACKFILL_HOLD_MAX_EVENTS:,}")
        elif time.monotonic() - self._bf_since > BACKFILL_HOLD_MAX_S:
            self._abort_backfill(f"took longer than {BACKFILL_HOLD_MAX_S:.0f}s")

    def _abort_backfill(self, why: str) -> None:
        """Discard the partial load and say so. Never a half-loaded chart.

        The partial objects go in the bin deliberately: a series holding the
        first forty minutes of a session, with totals to match, is a chart
        that reads as authoritative and is wrong about every figure a trader
        would check.
        """
        log.warning("startup backfill abandoned: %s - continuing LIVE ONLY", why)
        # CANCEL IS A REQUEST, NOT A STOP. cancel() sets a flag checked between
        # steps, and a worker blocked in socket.create_connection or a recv
        # will not see it for seconds. Dropping the reference here would let Qt
        # destroy a QThread that is still running, which aborts the process -
        # observed as a clean run exiting 127 with every check passed.
        #
        # So the references are KEPT, their signals disconnected so a late
        # result cannot land on a load that has already been abandoned, and
        # they are reaped once they finish.
        for sym in list(self._bf_fetchers):
            f = self._bf_fetchers.pop(sym)
            if f is None:
                continue
            f.cancel()
            try:
                f.built.disconnect()
                f.failed.disconnect()
            except TypeError:
                pass
            self._bf_zombies.append(f)
        self._bf_state = "live_only"
        self._bf_symbols = []
        for p_ in self._panes:
            if p_.lbl_last.text().startswith("loading"):
                p_.lbl_last.setText("no history")

    def release_backfill(self) -> None:
        """History is in. Let the held live events through, in order."""
        if self._bf_state not in ("holding", "loading"):
            return
        self._bf_state = "done"
        log.info("startup backfill complete: releasing %d held events",
                 len(self._event_q))

    def ingest_backfill(self, symbol: str, trades) -> dict:
        """Bulk-load a replayed session, and REFUSE it if anything was lost.

        SORTED FIRST, ALWAYS. add_trade cannot place a trade whose bucket was
        never created and discards it - measured at 0.078% of a session's
        volume when a day's replay is fed in arrival order. A bulk payload is
        held whole in memory, unlike a live stream, so it can be ordered and
        then nothing is ever late. See tests/backfill_order.py.

        The assertion afterwards is the point: dropped_late must be zero. If
        it is not, the volume profile and every session figure are short by an
        unknown amount, and a chart that is quietly wrong is worse than one
        that says it has no history.
        """
        s = self.series.get(symbol)
        if s is None:
            s = self.series[symbol] = BarSeries(symbol, self.instruments)
        before = s.dropped_late
        for tr in sorted(trades, key=lambda t: t.ts_ms):
            s.add_trade(tr)
        lost = s.dropped_late - before
        if lost:
            log.error("startup backfill REJECTED for %s: %d trades (%d volume) "
                      "could not be placed even sorted",
                      symbol, lost, s.dropped_late_vol)
        return {"symbol": symbol, "trades": len(trades), "dropped": lost,
                "ok": lost == 0}

    # ---- deep scroll-back -------------------------------------------------
    def _fetch_history_if_needed(self) -> None:
        """After the user stops moving: is anything on screen missing cells?

        A cold symbol has its footprint released behind COLD_BARS, so scrolling
        back into that region shows candles with no per-price detail. This
        notices, and asks the server for exactly that window.

        Everything here is a reason NOT to ask. One request in flight at a
        time, only for a window not already tried, only when a replay server
        exists, and only when bars are genuinely missing cells - because the
        cheapest fetch is the one that does not happen, and the previous
        version of this feature failed by asking too often for too much.
        """
        pane = self._history_pane or self._active_pane
        if pane is None or not pane.symbol:
            return
        if self._history_fetcher is not None and self._history_fetcher.isRunning():
            return
        host = getattr(self.feed, "replay_host", "")
        if not host:
            return                       # synthetic or pipe feed: no history
        s = self.series.get(pane.symbol)
        if s is None or not s.bars:
            return

        vbars = s.view(pane.tf_s)
        if not vbars:
            return
        vr = pane.price_plot.getViewBox().viewRect()
        lo = max(0, int(vr.left()))
        hi = min(len(vbars) - 1, int(vr.right()) + 1)
        if hi < lo:
            return
        t0 = vbars[lo].start_ts
        t1 = vbars[hi].start_ts + pane.tf_s

        # Which BASE bars in that span have lost their footprint. Checked on
        # the base series rather than the aggregated view because that is what
        # rebuild_footprint fills, and an aggregate can look populated while
        # the bars under it are stripped.
        missing = [b for b in s.bars
                   if t0 <= b.start_ts <= t1 and b.n_levels() == 0
                   and b is not s.bars[-1]]
        if not missing:
            return
        a_ms = missing[0].start_ts * 1000
        b_ms = (missing[-1].start_ts + s.base_tf_s) * 1000
        if b_ms - a_ms > HISTORY_MAX_SPAN_MS:
            a_ms = b_ms - HISTORY_MAX_SPAN_MS
        key = (pane.symbol, a_ms // 1000, b_ms // 1000)
        if key in self._history_tried:
            return
        self._history_tried.add(key)
        if len(self._history_tried) > 500:
            self._history_tried.clear()

        # The heat ring holds BACKFILL_L2_MIN of depth, so ask for no more.
        f = HistoryFetcher(host, getattr(self.feed, "replay_port", 9998),
                           getattr(self.feed, "token", ""), pane.symbol,
                           a_ms, b_ms, parent=self,
                           l2_start_ms=b_ms - int(BACKFILL_L2_MIN * 60 * 1000))
        f.ready.connect(self._on_history_ready)
        f.failed.connect(self._on_history_failed)
        self._history_fetcher = f
        log.info("history: fetching %s %d..%d (%d bars missing cells)",
                 pane.symbol, a_ms, b_ms, len(missing))
        f.start()

    def _on_history_ready(self, symbol, trades, books, rep) -> None:
        """GUI thread. QUEUE the replay; fold it a slice at a time.

        Folding it here cost 484 ms in one frame, measured - 431 for 135,000
        trades and 53 for the depth - against a 33 ms budget. It is not a leak
        and not the network; it is a half-second stall every time you scroll
        into cold history, and it lands again on every scroll.

        So the payload is grouped by BAR and folded a few bars per frame. By
        bar rather than by trade because rebuild_footprint's rule is that a bar
        gets its whole footprint or none of it - splitting a bar across two
        frames would leave it briefly showing a partial footprint next to a
        candle that says a different number.
        """
        s = self.series.get(symbol)
        if s is None:
            return
        if not trades and not books:
            log.warning("startup backfill: %s returned no data (%s)", symbol, rep)
            return
        by_bar: dict[int, list] = {}
        step = s.base_tf_s
        for tr in trades:
            by_bar.setdefault(int(tr.ts_ms // 1000 // step) * step, []).append(tr)
        self._fold_q.append({"symbol": symbol, "bars": sorted(by_bar.items()),
                             "books": list(books), "rep": rep,
                             "done_bars": 0, "done_books": 0})
        log.info("history: %s queued %d bars and %d books to fold",
                 symbol, len(by_bar), len(books))

    def _fold_pending(self) -> None:
        """Fold queued history within one frame's budget, then stop.

        Bounded by TIME rather than by a fixed count: a bar with four price
        levels and one with four hundred are not the same work, and a count
        tuned for one is wrong for the other.
        """
        if not self._fold_q:
            return
        deadline = time.perf_counter() + FOLD_BUDGET_S
        job = self._fold_q[0]
        sym = job["symbol"]
        s = self.series.get(sym)
        if s is None:
            self._fold_q.popleft()
            return
        # ---- footprint, whole bars at a time ----------------------------
        bars = job["bars"]
        applied = 0
        while job["done_bars"] < len(bars) and time.perf_counter() < deadline:
            chunk = []
            for _ in range(8):
                if job["done_bars"] >= len(bars):
                    break
                chunk.extend(bars[job["done_bars"]][1])
                job["done_bars"] += 1
            if chunk:
                out = s.rebuild_footprint(chunk)
                applied += out["bars"]
        # ---- then the depth ---------------------------------------------
        b = self.bookmaps.get(sym)
        books = job["books"]
        if b is not None:
            while job["done_books"] < len(books) and time.perf_counter() < deadline:
                nxt = min(len(books), job["done_books"] + 400)
                b.rebuild_heatmap(books[job["done_books"]:nxt])
                job["done_books"] = nxt
        if applied:
            for p in self._panes[:self._n_panes]:
                if p.symbol == sym:
                    p.fp.update()
            self._dirty = True
        if job["done_bars"] >= len(bars) and job["done_books"] >= len(books):
            self._fold_q.popleft()
            log.info("history: %s folded - %d bars, %d books", sym,
                     len(bars), len(books))
            self._dirty = True

    def _on_history_failed(self, symbol, err) -> None:
        log.info("history: %s unavailable (%s)", symbol, err)

    def _follow_price(self, bars: list, pane=None) -> None:
        """Keep live price on screen while following, without nagging the view.

        Centring on a symbol change is not enough on its own: price then walks
        out of the frame it was given. Measured on a quiet six-second sample the
        view was 399.89..400.24 while price had already reached 400.83 - off
        screen, and the only cure was Alt+R again. That is the same complaint
        as the symbol switch, with a different cause.

        HYSTERESIS IS THE POINT. Re-fitting every frame would set a slightly
        different range every frame, and every range change is a full repaint -
        it would spend the frame budget the governor just finished protecting,
        on a view that never visibly moves. So we only act when price actually
        nears the edge, and then we re-frame with room to spare so the next
        move does not immediately trip it again.
        """
        pane = pane or self._active_pane
        vb = pane.price_plot.getViewBox()
        y0, y1 = vb.viewRange()[1]
        span = y1 - y0
        if span <= 0:
            return
        last = bars[-1].close
        # Comfortable zone: the middle 70% of the view. Outside it, re-frame.
        pad = span * 0.15
        if y0 + pad <= last <= y1 - pad:
            return
        # Re-frame on what is actually visible, not on a fixed bar count, so a
        # zoomed-out view does not snap back to a narrow one.
        x0, x1 = vb.viewRange()[0]
        lo_i = max(0, int(x0))
        hi_i = min(len(bars), int(x1) + 1)
        vis = bars[lo_i:hi_i] or bars[-24:]
        lo = min(b.low for b in vis)
        hi = max(b.high for b in vis)
        # Preserve the user's zoom LEVEL where we can - re-centre a span they
        # chose rather than imposing one, unless the data no longer fits.
        need = (hi - lo) * 1.24 or 1.0
        keep = max(span, need)
        mid = (hi + lo) * 0.5
        pane.price_plot.setYRange(mid - keep * 0.5, mid + keep * 0.5, padding=0)

    def _center(self, pane=None) -> None:
        """Frame the latest bars. Alt+R and the Center button hit the ACTIVE
        pane; the redraw passes the pane it is drawing."""
        pane = pane or self._active_pane
        s = self.series.get(pane.symbol) if pane.symbol else None
        if s is None:
            return
        bars = s.view(pane.tf_s)
        if not bars:
            return
        vis = bars[-24:]
        lo = min(b.low for b in vis)
        hi = max(b.high for b in vis)
        margin = (hi - lo) * 0.12 or 1.0
        pane.price_plot.setYRange(lo - margin, hi + margin, padding=0)
        pane.price_plot.setXRange(max(-1, len(bars) - 22), len(bars) + 3, padding=0)
        pane.auto_scroll = True
        pane.auto_y = True

    def changeEvent(self, ev):
        if (ev.type() == QEvent.Type.ActivationChange
                and self.isActiveWindow()):
            GOVERNOR.set_focus(id(self))
        super().changeEvent(ev)

    def closeEvent(self, event) -> None:
        workspace.save(self)                  # remember the desk for next time
        # Wait for the history workers. A QThread still running when the
        # interpreter tears down aborts the process, so closing the app mid
        # backfill would crash on exit rather than shut down.
        for f in list(self._bf_fetchers.values()) + list(self._bf_zombies):
            try:
                f.cancel()
                f.wait(3000)
            except RuntimeError:
                pass
        self._bf_fetchers = {}
        self._bf_zombies = []
        self.feed.stop()
        super().closeEvent(event)
