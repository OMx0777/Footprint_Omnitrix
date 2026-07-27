"""Main ATAS-style window: footprint price pane + cumulative-delta pane,
fed by any engine Feed via a thread-safe queue drained on the GUI thread.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QMainWindow, QToolBar, QLabel, QComboBox, QCheckBox, QPushButton, QWidget,
    QSizePolicy, QDockWidget,
)

from ..engine import (
    Instruments, BarSeries, BookmapBuffer, SessionProfile, Feed,
)
from ..engine.model import Trade, BookSnapshot
from ..render import (
    FootprintItem, HeatmapItem, DARK, LIGHT, TimeAxis,
    FibRetracement, PositionDrawer, FixedVolumeProfile, EMAItem, CPRItem
)
from .settings_dialog import SettingsDialog
from .bookmap_window import BookmapWindow
from .profile_window import ProfileWindow
from .analytics_window import AnalyticsWindow
from .monitor_window import MarketMonitorWindow
from .dom_ladder import DomLadderWindow
from . import workspace
from .tape_widget import TapeWidget
from .stats_panel import StatsPanel
from .signals_panel import SignalsPanel

# Roughly 15 s of a very busy 100-symbol basket. Beyond this the GUI is not
# keeping up and holding more events only makes the lag worse.
EVENT_QUEUE_MAX = 600_000

TF_CHOICES = {
    "10s": 10, "30s": 30, "1m": 60, "2m": 120, "3m": 180, "5m": 300,
    "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400,
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
        self.active_symbol = ""
        self.tf_s = 60
        self.theme = DARK
        self.auto_scroll = True
        self._dirty = False
        self._centered_once = False
        self._known_symbols: set[str] = set()

        # thread-safe hand-off: feed thread appends, GUI timer drains.
        # ONE queue keeps trades and books in their true time order, so a book
        # always routes to a bar the preceding trades already created.
        #
        # BOUNDED on purpose. Live mode accepts every symbol the DLL sends (a
        # 100-name basket at snapshot rate), and an unbounded deque under a slow
        # drain grows without limit: RAM climbs, latency climbs, and the chart
        # silently falls further behind real time with nothing to show for it.
        # A maxlen sheds the oldest events instead and reports the loss.
        self._event_q: deque = deque(maxlen=EVENT_QUEUE_MAX)
        self._dropped = 0

        # Custom footprint painting is viewport-culled, so GL buys nothing and
        # only adds driver-dependent bugs. Software raster is crisp + portable.
        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_ui()
        self._apply_theme()

        feed.on_trade(self._enqueue)
        feed.on_book(self._enqueue)

        self._pending_symbol = ""
        workspace.restore(self)               # reapply the last saved desk

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)                 # ~30 fps drain + redraw

    def start_feed(self) -> None:
        """Begin streaming.

        Deliberately NOT called from __init__: SyntheticFeed.start() emits its
        entire 90-minute prefill synchronously, so a Recorder attached after
        construction used to miss every one of those events. The caller starts
        the feed once all taps are in place.
        """
        self.feed.start()

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

        tb.addSeparator()
        self.chk_imb = QCheckBox("Imbalance")
        self.chk_imb.setChecked(True)
        self.chk_imb.toggled.connect(lambda v: self.fp.set_show_imbalance(v))
        tb.addWidget(self.chk_imb)

        tb.addWidget(QLabel(" ×"))
        self.imb_combo = QComboBox()
        self.imb_combo.addItems(["2.0", "3.0", "4.0", "5.0"])
        self.imb_combo.setCurrentText("3.0")
        self.imb_combo.currentTextChanged.connect(
            lambda s: self.fp.set_imbalance_factor(float(s)))
        tb.addWidget(self.imb_combo)

        self.chk_va = QCheckBox("Value Area")
        self.chk_va.setChecked(True)
        self.chk_va.toggled.connect(lambda v: self.fp.set_show_va(v))
        tb.addWidget(self.chk_va)

        self.chk_vwap = QCheckBox("VWAP")
        self.chk_vwap.setChecked(True)
        self.chk_vwap.toggled.connect(self._on_vwap_toggled)
        tb.addWidget(self.chk_vwap)

        tb.addSeparator()
        self.chk_cpr = QCheckBox("CPR")
        self.chk_cpr.setChecked(False)
        self.chk_cpr.toggled.connect(self._on_cpr_toggled)
        tb.addWidget(self.chk_cpr)

        self.chk_ema = QCheckBox("EMAs")
        self.chk_ema.setChecked(False)
        self.chk_ema.toggled.connect(self._on_ema_toggled)
        tb.addWidget(self.chk_ema)

        tb.addSeparator()
        tb.addWidget(QLabel(" Theme "))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.currentTextChanged.connect(self._on_theme)
        tb.addWidget(self.theme_combo)

        self.btn_bookmap = QPushButton("Bookmap")
        self.btn_bookmap.clicked.connect(self._open_bookmap)
        tb.addWidget(self.btn_bookmap)

        self.btn_profile = QPushButton("Profile")
        self.btn_profile.clicked.connect(self._open_profile)
        tb.addWidget(self.btn_profile)

        self.btn_analytics = QPushButton("Analytics")
        self.btn_analytics.clicked.connect(self._open_analytics)
        tb.addWidget(self.btn_analytics)

        self.btn_monitor = QPushButton("Monitor")
        self.btn_monitor.clicked.connect(self._open_monitor)
        tb.addWidget(self.btn_monitor)

        self.btn_dom = QPushButton("DOM")
        self.btn_dom.clicked.connect(self._open_dom)
        tb.addWidget(self.btn_dom)

        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.clicked.connect(self._open_settings)
        tb.addWidget(self.btn_settings)

        self.btn_center = QPushButton("Center")
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

        # ---- Drawing Toolbar (Left) ----
        dtb = QToolBar("Drawings")
        dtb.setObjectName("tb_drawings")
        dtb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, dtb)
        
        self.active_drawing_tool = None
        self.drawing_items = []
        self._drawing_start_point = None

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
            ("▤", "VP", "Fixed-range volume profile"),
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

        # ---- two linked panes: price (top), CVD (bottom) ----
        self.glw = pg.GraphicsLayoutWidget()
        self.setCentralWidget(self.glw)

        self.price_plot = self.glw.addPlot(row=0, col=0)
        self.price_plot.showAxis("right")
        self.price_plot.hideAxis("left")
        self.price_plot.hideAxis("bottom")     # time labels live on the CVD pane
        self.price_plot.showGrid(x=True, y=True, alpha=0.25)

        self.time_axis = TimeAxis(orientation="bottom")
        self.cvd_plot = self.glw.addPlot(row=1, col=0,
                                         axisItems={"bottom": self.time_axis})
        self.cvd_plot.showAxis("right")
        self.cvd_plot.hideAxis("left")
        self.cvd_plot.showGrid(x=True, y=True, alpha=0.2)
        self.cvd_plot.setXLink(self.price_plot)
        self.glw.ci.layout.setRowStretchFactor(0, 4)
        self.glw.ci.layout.setRowStretchFactor(1, 1)

        self.heatmap = HeatmapItem(self.instruments.tick("QQQ"))
        self.heatmap.setVisible(False)
        self.price_plot.addItem(self.heatmap)

        self.fp = FootprintItem(self.instruments.tick("QQQ"), self.theme)
        self.price_plot.addItem(self.fp)

        # Indicators
        self.cpr_item = CPRItem()
        self.cpr_item.setVisible(False)
        self.price_plot.addItem(self.cpr_item)

        self.ema9_item = EMAItem(period=9, color=QColor(33, 150, 243))
        self.ema21_item = EMAItem(period=21, color=QColor(255, 193, 7))
        self.ema9_item.setVisible(False)
        self.ema21_item.setVisible(False)
        self.price_plot.addItem(self.ema9_item)
        self.price_plot.addItem(self.ema21_item)

        self.vwap_curve = pg.PlotDataItem(pen=pg.mkPen(self.theme.vwap, width=2))
        self.price_plot.addItem(self.vwap_curve)

        # VWAP standard-deviation bands (±1σ, ±2σ) — institutional mean-reversion
        # envelope; price outside ±2σ is statistically stretched.
        self.vwap_bands = []
        for mult, alpha, dash in ((1, 150, Qt.PenStyle.DashLine),
                                  (2, 90, Qt.PenStyle.DotLine)):
            for _ in range(2):                    # upper + lower
                c = pg.PlotDataItem(pen=pg.mkPen(self.theme.vwap, width=1,
                                                 style=dash))
                c.setOpacity(alpha / 255.0)
                self.price_plot.addItem(c)
                self.vwap_bands.append((mult, c))

        self.cvd_curve = pg.PlotDataItem(pen=pg.mkPen(self.theme.cvd, width=2))
        self.cvd_plot.addItem(self.cvd_curve)
        self.cvd_zero = pg.InfiniteLine(angle=0, pos=0, movable=False,
                                        pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.cvd_plot.addItem(self.cvd_zero, ignoreBounds=True)

        self.price_line = pg.InfiniteLine(angle=0, movable=False,
                                          pen=pg.mkPen(self.theme.cvd, width=1,
                                                       style=Qt.PenStyle.DashLine))
        self.price_plot.addItem(self.price_line, ignoreBounds=True)
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen("#666", style=Qt.PenStyle.DashLine))
        self.price_plot.addItem(self.vline, ignoreBounds=True)
        self.price_plot.addItem(self.hline, ignoreBounds=True)

        self.glw.scene().sigMouseMoved.connect(self._on_mouse_move)
        self.glw.scene().sigMouseClicked.connect(self._on_mouse_click)
        self.price_plot.getViewBox().sigRangeChangedManually.connect(self._on_view)

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
        self.fp.set_theme(t)
        self.vwap_curve.setPen(pg.mkPen(t.vwap, width=2))
        self.cvd_curve.setPen(pg.mkPen(t.cvd, width=2))
        self.glw.setBackground(t.bg)
        # Restrained, terminal-like chrome. Painting every QPushButton in the
        # bull accent turned the toolbars into a wall of teal that competed with
        # the chart for attention; controls are now neutral, with the accent
        # reserved for the armed drawing tool.
        self.setStyleSheet(
            f"QMainWindow {{ background:{t.bg}; }}"
            f" QToolBar {{ background:{t.panel}; border:none; padding:4px;"
            f"   spacing:3px; }}"
            f" QToolBar::separator {{ background:{t.axis}; width:1px; height:1px;"
            f"   margin:4px 6px; }}"
            f" QLabel {{ color:{t.text}; font-size:12px; font-weight:600; }}"
            f" QCheckBox {{ color:{t.text}; font-size:12px; font-weight:600;"
            f"   padding:0 4px; }}"
            f" QComboBox {{ background:{t.grid}; color:{t.text};"
            f"   border:1px solid {t.axis}; border-radius:4px; padding:3px 8px;"
            f"   font-size:12px; }}"
            f" QComboBox::drop-down {{ border:none; }}"
            f" QComboBox QAbstractItemView {{ background:{t.panel};"
            f"   color:{t.text}; selection-background-color:{t.grid};"
            f"   border:1px solid {t.axis}; }}"
            f" QPushButton {{ background:{t.grid}; color:{t.text};"
            f"   border:1px solid {t.axis}; border-radius:4px; padding:4px 10px;"
            f"   font-weight:600; font-size:12px; }}"
            f" QPushButton:hover {{ background:{t.axis}; }}"
            f" QPushButton:checked {{ background:{t.bull}; color:#FFFFFF;"
            f"   border:1px solid {t.bull}; }}"
            f" QDockWidget {{ color:{t.text}; font-size:11px; font-weight:700; }}"
            f" QDockWidget::title {{ background:{t.panel}; padding:5px 8px;"
            f"   border-bottom:1px solid {t.axis}; }}"
            f" QTabBar::tab {{ background:{t.panel}; color:{t.text};"
            f"   padding:5px 12px; border:none; font-size:11px; }}"
            f" QTabBar::tab:selected {{ background:{t.grid};"
            f"   border-bottom:2px solid {t.bull}; }}"
        )
        for plot in (self.price_plot, self.cvd_plot):
            for ax_name in ("bottom", "right"):
                ax = plot.getAxis(ax_name)
                ax.setPen(pg.mkPen(t.axis))
                ax.setTextPen(pg.mkPen(t.text))

    # ---- feed drain + redraw (GUI thread) --------------------------------
    def _tick(self) -> None:
        drained = 0
        q = self._event_q
        while q and drained < 40000:
            ev = q.popleft()
            drained += 1
            if isinstance(ev, Trade):
                s = self.series.get(ev.symbol)
                if s is None:
                    s = self.series[ev.symbol] = BarSeries(ev.symbol, self.instruments)
                s.add_trade(ev)
                self._bookmap(ev.symbol).add_trade(ev)
                self._profile(ev.symbol).add_trade(ev)
                if ev.symbol not in self._known_symbols:
                    self._register_symbol(ev.symbol)
                if ev.symbol == self.active_symbol:
                    self._dirty = True
            else:  # BookSnapshot
                self.latest_book[ev.symbol] = ev
                self._bookmap(ev.symbol).add_book(ev)
                s = self.series.get(ev.symbol)
                if s is not None:
                    s.add_book(ev)
                    if ev.symbol == self.active_symbol:
                        self._dirty = True

        # Refresh the live indicator ~2x/sec even when no data is flowing, so
        # "waiting for Takion" is visible before the first tick arrives.
        self._link_tick = getattr(self, "_link_tick", 0) + 1
        if self._link_tick % 15 == 0:
            self._update_link()

        if self._dirty and self.active_symbol:
            self._redraw()
            self._dirty = False
            _s = self.series.get(self.active_symbol)
            if not self._centered_once and _s is not None and _s.view(self.tf_s):
                self._center()
                self._centered_once = True

    def _register_symbol(self, sym: str) -> None:
        self._known_symbols.add(sym)
        self.sym_combo.blockSignals(True)
        self.sym_combo.addItem(sym)
        self.sym_combo.blockSignals(False)
        # prefer the symbol restored from the saved workspace once it arrives
        if self._pending_symbol and sym == self._pending_symbol:
            self._pending_symbol = ""
            self.active_symbol = sym
            self.sym_combo.setCurrentText(sym)
            self.fp.tick = self.instruments.tick(sym)
            self._dirty = True
        elif not self.active_symbol:
            self.active_symbol = sym
            self.sym_combo.setCurrentText(sym)
            self.fp.tick = self.instruments.tick(sym)

    def _redraw(self) -> None:
        # A tick change (or a symbol selected before its first print) leaves the
        # series absent until the next trade rebuilds it. Every accessor below
        # must tolerate that or the GUI raises on the very next frame.
        s = self.series.get(self.active_symbol)
        if s is None:
            return
        bars = s.view(self.tf_s)
        self.fp.set_bars(bars)
        if self.heatmap.isVisible():
            self.heatmap.set_bars(bars)
        self.time_axis.set_bars(bars)
        
        if self.cpr_item.isVisible(): self.cpr_item.set_bars(bars)
        if self.ema9_item.isVisible(): self.ema9_item.set_bars(bars)
        if self.ema21_item.isVisible(): self.ema21_item.set_bars(bars)
        
        self._update_overlays(s, bars)
        if bars:
            self.price_line.setPos(bars[-1].close)
            if self.auto_scroll:
                n = len(bars)
                vr = self.price_plot.getViewBox().viewRect()
                if vr.right() < n + 1:
                    self.price_plot.setXRange(max(-1, n - 22), n + 3, padding=0)
            self._update_stats(bars[-1])

    def _update_overlays(self, series: BarSeries, bars: list) -> None:
        if not bars:
            self.vwap_curve.setData([], [])
            self.cvd_curve.setData([], [])
            for _, c in self.vwap_bands:
                c.setData([], [])
            return
        tick = self.instruments.tick(self.active_symbol)
        # One memoized pass over the bars' cached moments, not a full walk of
        # every price cell in the history on every frame.
        vy, vstd, cy = series.overlays(self.tf_s, tick)
        xs = list(range(len(vy)))

        self.vwap_curve.setData(xs, vy)
        show_bands = self.vwap_curve.isVisible()
        for k, (mult, curve) in enumerate(self.vwap_bands):
            sign = 1 if k % 2 == 0 else -1
            if show_bands and vstd:
                curve.setData(xs, [m + sign * mult * s
                                   for m, s in zip(vy, vstd)])
            else:
                curve.setData([], [])
        self.cvd_curve.setData(list(range(len(cy))), cy)

    def _update_stats(self, bar) -> None:
        self.lbl_stats.setText(
            f"  {self.active_symbol}   {bar.close:.2f}   "
            f"Vol {bar.volume:,}   Δ {bar.delta:+,}   "
        )
        self._update_link()

    def _update_link(self) -> None:
        """Show live-pipe status; blank for feeds that aren't pipe-based."""
        conn = getattr(self.feed, "connected", None)
        if not isinstance(conn, dict):
            return
        l1, l2 = bool(conn.get("l1")), bool(conn.get("l2"))
        if l1 and l2:
            txt, col = f"● LIVE  {len(self.series)} sym", "#26A69A"
        elif l1 or l2:
            txt, col = f"● PARTIAL  L1:{'✓' if l1 else '×'} L2:{'✓' if l2 else '×'}", "#FFB300"
        else:
            txt, col = "○ waiting for Takion…", "#EF5350"
        if self._dropped:
            # Visible, not silent: if the GUI cannot keep up you need to know the
            # chart is now an incomplete picture.
            txt += f"   ⚠ dropped {self._dropped:,}"
            col = "#FFB300"
        self.lbl_link.setText(f"  {txt}  ")
        self.lbl_link.setStyleSheet(f"color:{col}; font-weight:700;")

    # ---- interactions ----------------------------------------------------
    def _on_symbol(self, sym: str) -> None:
        if sym:
            self.active_symbol = sym
            self.fp.tick = self.instruments.tick(sym)
            self.auto_scroll = True
            self._dirty = True

    def _on_tf(self, txt: str) -> None:
        self.tf_s = TF_CHOICES.get(txt, 60)
        self.auto_scroll = True
        self._dirty = True

    def _on_vwap_toggled(self, on: bool) -> None:
        self.vwap_curve.setVisible(on)
        for _, c in self.vwap_bands:
            c.setVisible(on)
        self._dirty = True

    def _on_cpr_toggled(self, on: bool) -> None:
        self.cpr_item.setVisible(on)
        self._dirty = True

    def _on_ema_toggled(self, on: bool) -> None:
        self.ema9_item.setVisible(on)
        self.ema21_item.setVisible(on)
        self._dirty = True

    def _on_mode(self, name: str) -> None:
        fp_mode, draw_cells, hm_visible = MODES.get(name, ("Footprint", True, False))
        self.fp.set_mode(fp_mode)
        self.fp.set_draw_cells(draw_cells)
        self.heatmap.setVisible(hm_visible)
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

    def _open_bookmap(self) -> None:
        sym = self.active_symbol or "QQQ"
        if (w := self._child(f"bookmap:{sym}")) is not None:
            w.raise_(); w.activateWindow(); return
        win = BookmapWindow(self._bookmap(sym), self.instruments.tick(sym), self)
        self._register_child(f"bookmap:{sym}", win)

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
            self._centered_once = False
        self.fp.configure(
            imbalance_factor=v["imbalance_factor"],
            min_imbalance_vol=v["min_imbalance_vol"],
            stacked_min=v["stacked_min"],
            va_pct=v["va_pct"],
            show_candles=v["show_candles"],
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

    def _on_mouse_move(self, pos) -> None:
        if self.price_plot.sceneBoundingRect().contains(pos):
            mp = self.price_plot.vb.mapSceneToView(pos)
            self.vline.setPos(mp.x())
            self.hline.setPos(mp.y())

    def _on_mouse_click(self, ev) -> None:
        if not self.active_drawing_tool:
            return
            
        pos = ev.scenePos()
        if not self.price_plot.sceneBoundingRect().contains(pos):
            return
            
        mp = self.price_plot.vb.mapSceneToView(pos)
        
        if ev.button() == Qt.MouseButton.LeftButton:
            if not self._drawing_start_point:
                # First click: set start point
                self._drawing_start_point = mp
            else:
                # Second click: finalize drawing
                end_point = mp
                start = self._drawing_start_point
                self._drawing_start_point = None
                
                # Instantiate correct drawing tool
                item = None
                if self.active_drawing_tool == "Fib":
                    item = FibRetracement([start.x(), start.y()], [end_point.x(), end_point.y()])
                elif self.active_drawing_tool == "Long":
                    item = PositionDrawer([start.x(), start.y()], [end_point.x() - start.x(), end_point.y() - start.y()], is_long=True)
                elif self.active_drawing_tool == "Short":
                    item = PositionDrawer([start.x(), start.y()], [end_point.x() - start.x(), end_point.y() - start.y()], is_long=False)
                elif self.active_drawing_tool == "VP":
                    item = FixedVolumeProfile(
                        [start.x(), start.y()], 
                        [end_point.x() - start.x(), end_point.y() - start.y()], 
                        self._get_bars_for_vp, 
                        self.instruments.tick(self.active_symbol)
                    )
                
                if item:
                    self.price_plot.addItem(item)
                    self.drawing_items.append(item)
                    self._set_drawing_tool(None) # Auto-revert to cursor

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
        self._drawing_start_point = None
        for tool, btn in self._tool_buttons.items():
            btn.setChecked(tool == tool_name)
        vb = self.price_plot.getViewBox()
        if tool_name is None:
            vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            vb.setMouseMode(pg.ViewBox.RectMode) # Prevents dragging from panning the chart while drawing

    def _clear_drawings(self):
        for item in self.drawing_items:
            self.price_plot.removeItem(item)
        self.drawing_items.clear()
        self._set_drawing_tool(None)

    def _on_view(self) -> None:
        s = self.series.get(self.active_symbol) if self.active_symbol else None
        if s is None:
            return
        bars = s.view(self.tf_s)
        vr = self.price_plot.getViewBox().viewRect()
        self.auto_scroll = vr.right() >= len(bars) - 1.0

    def _center(self) -> None:
        s = self.series.get(self.active_symbol) if self.active_symbol else None
        if s is None:
            return
        bars = s.view(self.tf_s)
        if not bars:
            return
        vis = bars[-24:]
        lo = min(b.low for b in vis)
        hi = max(b.high for b in vis)
        margin = (hi - lo) * 0.12 or 1.0
        self.price_plot.setYRange(lo - margin, hi + margin, padding=0)
        self.price_plot.setXRange(max(-1, len(bars) - 22), len(bars) + 3, padding=0)
        self.auto_scroll = True

    def closeEvent(self, event) -> None:
        workspace.save(self)                  # remember the desk for next time
        self.feed.stop()
        super().closeEvent(event)
