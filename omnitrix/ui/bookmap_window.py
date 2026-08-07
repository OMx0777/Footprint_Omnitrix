"""Bookmap-style liquidity window: heatmap + BBO + glossy trade bubbles, with a
right-edge DOM depth ladder and a bottom volume histogram.

Interaction is TradingView-like:
  * left-drag pans, mouse-wheel zooms smoothly about the cursor,
  * while "following" it tracks the newest column and keeps YOUR zoom width,
  * any manual pan/zoom drops follow + price-autofit,
  * the Follow button or a double-click snaps back to live and re-fits price.
A timeframe selector aggregates the 1-second base columns (1s…1m).
"""

from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPointF, QEvent
from PyQt6.QtWidgets import (
    QMainWindow, QToolBar, QLabel, QComboBox, QPushButton, QCheckBox, QLineEdit,
    QWidget, QVBoxLayout, QSplitter, QMenu, QToolButton, QWidgetAction,
    QHBoxLayout,
)

from .framegov import GOVERNOR, GovernedTimer, GovernedPlotWidget
from ..engine import BookmapBuffer, SRTracker
from .bookmap_pane import BookmapPane
from ..render import (
    BookHeatmapItem, BBOItem, BubbleItem, PieItem, BarsItem, ProjectionItem,
    DomLadderItem, VolumeBarsItem, SRLinesItem,
)
from ..render.bookmap import LOOK_LUTS as LOOKS

# label -> live-gradient strength for the heatmap's recency weighting
RECENCY = {"Off": 0.0, "Light": 0.35, "Medium": 0.65, "Strong": 1.0}

from ..render.bookmap import BOOKMAP_BG as BG
from ..render.pricegrid import auto_step_ticks, TARGET_PX_BAND
from ..render.crosshair import Crosshair, clock_label

# label -> aggregation factor over the 1s base columns
TF = {"1s": 1, "5s": 5, "10s": 10, "30s": 30, "1m": 60, "5m": 300,
      "10m": 600, "15m": 900, "30m": 1800, "1h": 3600}

# label -> tape time bin, in base columns. Independent of TF on purpose: see
# _TapeItem. "Live" is the finest the buffer can express (one base column).
BUBBLE_TF = {"Live": 1, "1s": 1, "2s": 2, "3s": 3, "5s": 5, "10s": 10,
             "30s": 30, "1m": 60, "5m": 300}

# label -> price bucket in DOLLARS; converted to ticks against the instrument.
# -1 selects Auto (follow the zoom); 0 pins to exactly one tick.
PRICE_STEP = {"Auto": -1.0, "1 tick": 0.0, "1¢": 0.01, "5¢": 0.05, "10¢": 0.10,
              "25¢": 0.25, "50¢": 0.50, "$1": 1.00}

SIZE_STEPS = {"50%": 0.5, "75%": 0.75, "100%": 1.0, "150%": 1.5,
              "200%": 2.0, "300%": 3.0, "400%": 4.0}

# The longest timeframe only means something if the buffer holds that much
# history. The base ring is sized for every streaming symbol at once, but a
# Bookmap window is open on ONE symbol, so that symbol can afford a deep ring:
# at ~2.3 kB per compact ladder this is ~33 MB, against ~3 MB for the default.
HISTORY_COLS = 14400          # 4 hours of 1-second columns

# Books per window. Four is the ceiling for the same reason as the footprint
# grid: below a quarter of a 1920x1080 screen the heat field stops resolving.
LAYOUTS = {"1 book": 1, "2 books": 2, "4 books": 4}
MAX_PANES = 4


def _tf_seconds(agg: int, col_dt: float) -> float:
    return agg * col_dt


def _fmt(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{v / 1000:.0f}K"
    return str(v)


class BookmapWindow(QMainWindow):
    def __init__(self, buffer: BookmapBuffer, tick: float, parent=None):
        super().__init__(parent)
        self._tick0 = tick
        self.setWindowTitle(f"Omnitrix Bookmap \u2014 {buffer.symbol}")
        self.resize(1500, 860)
        self._n_panes = 1

        # Deepen this symbol's ring so the long timeframes have data to
        # aggregate; a 1-hour view over a 23-minute ring is one column.
        if buffer.max_cols < HISTORY_COLS:
            buffer.max_cols = HISTORY_COLS

        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_toolbar()
        self._build_grid(buffer)

        # Priority 1: useful, but not the window being traded from.
        self._timer = GovernedTimer(self, self.refresh, 80, priority=1)
        self._timer.start()
        self.refresh(initial=True)

    @property
    def buffer(self):
        return self._active_pane.buffer

    @buffer.setter
    def buffer(self, v):
        self._active_pane.buffer = v

    @property
    def glw(self):
        return self._active_pane.glw

    @glw.setter
    def glw(self, v):
        self._active_pane.glw = v

    @property
    def main(self):
        return self._active_pane.main

    @main.setter
    def main(self, v):
        self._active_pane.main = v

    @property
    def dom(self):
        return self._active_pane.dom

    @dom.setter
    def dom(self, v):
        self._active_pane.dom = v

    @property
    def vol(self):
        return self._active_pane.vol

    @vol.setter
    def vol(self, v):
        self._active_pane.vol = v

    @property
    def vol_axis(self):
        return self._active_pane.vol_axis

    @vol_axis.setter
    def vol_axis(self, v):
        self._active_pane.vol_axis = v

    @property
    def heat(self):
        return self._active_pane.heat

    @heat.setter
    def heat(self, v):
        self._active_pane.heat = v

    @property
    def bbo(self):
        return self._active_pane.bbo

    @bbo.setter
    def bbo(self, v):
        self._active_pane.bbo = v

    @property
    def bubbles(self):
        return self._active_pane.bubbles

    @bubbles.setter
    def bubbles(self, v):
        self._active_pane.bubbles = v

    @property
    def pie(self):
        return self._active_pane.pie

    @pie.setter
    def pie(self, v):
        self._active_pane.pie = v

    @property
    def bars(self):
        return self._active_pane.bars

    @bars.setter
    def bars(self, v):
        self._active_pane.bars = v

    @property
    def dom_item(self):
        return self._active_pane.dom_item

    @dom_item.setter
    def dom_item(self, v):
        self._active_pane.dom_item = v

    @property
    def vol_item(self):
        return self._active_pane.vol_item

    @vol_item.setter
    def vol_item(self, v):
        self._active_pane.vol_item = v

    @property
    def cursor(self):
        return self._active_pane.cursor

    @cursor.setter
    def cursor(self, v):
        self._active_pane.cursor = v

    @property
    def price_line(self):
        return self._active_pane.price_line

    @price_line.setter
    def price_line(self, v):
        self._active_pane.price_line = v

    @property
    def projection(self):
        return self._active_pane.projection

    @projection.setter
    def projection(self, v):
        self._active_pane.projection = v

    @property
    def sr(self):
        return self._active_pane.sr

    @sr.setter
    def sr(self, v):
        self._active_pane.sr = v

    @property
    def sr_item(self):
        return self._active_pane.sr_item

    @sr_item.setter
    def sr_item(self, v):
        self._active_pane.sr_item = v

    @property
    def cx_v(self):
        return self._active_pane.cx_v

    @cx_v.setter
    def cx_v(self, v):
        self._active_pane.cx_v = v

    @property
    def cx_h(self):
        return self._active_pane.cx_h

    @cx_h.setter
    def cx_h(self, v):
        self._active_pane.cx_h = v

    @property
    def readout(self):
        return self._active_pane.readout

    @readout.setter
    def readout(self, v):
        self._active_pane.readout = v

    @property
    def xhair(self):
        return self._active_pane.xhair

    @xhair.setter
    def xhair(self, v):
        self._active_pane.xhair = v

    @property
    def agg(self):
        return self._active_pane.agg

    @agg.setter
    def agg(self, v):
        self._active_pane.agg = v

    @property
    def bubble_bin(self):
        return self._active_pane.bubble_bin

    @bubble_bin.setter
    def bubble_bin(self, v):
        self._active_pane.bubble_bin = v

    @property
    def row_ticks(self):
        return self._active_pane.row_ticks

    @row_ticks.setter
    def row_ticks(self, v):
        self._active_pane.row_ticks = v

    @property
    def auto_step(self):
        return self._active_pane.auto_step

    @auto_step.setter
    def auto_step(self, v):
        self._active_pane.auto_step = v

    @property
    def proj_width(self):
        return self._active_pane.proj_width

    @proj_width.setter
    def proj_width(self, v):
        self._active_pane.proj_width = v

    @property
    def style(self):
        return self._active_pane.style

    @style.setter
    def style(self, v):
        self._active_pane.style = v

    @property
    def _follow(self):
        return self._active_pane._follow

    @_follow.setter
    def _follow(self, v):
        self._active_pane._follow = v

    @property
    def _auto_y(self):
        return self._active_pane._auto_y

    @_auto_y.setter
    def _auto_y(self, v):
        self._active_pane._auto_y = v

    @property
    def tick(self):
        # __init__ sets this before any pane exists, so it needs a fallback.
        p = getattr(self, "_active_pane", None)
        return p.tick if p is not None else self._tick0

    @tick.setter
    def tick(self, v):
        p = getattr(self, "_active_pane", None)
        if p is None:
            self._tick0 = v
        else:
            p.tick = v

    # ---- pane grid ---------------------------------------------------
    def _build_grid(self, buffer: BookmapBuffer) -> None:
        """Up to four books in one window, resizable, instead of four windows.

        Panes are created ONCE and shown or hidden. Building and destroying
        plots on a layout change would drop each pane's zoom, price grid and
        accumulated S/R state, which is most of what makes a book worth
        watching for more than a few seconds.

        All panes share the WINDOW's frame-budget key, so a 2x2 grid is
        budgeted as one window - which is what it is.
        """
        host = QWidget()
        _cv = QVBoxLayout(host)
        _cv.setContentsMargins(0, 0, 0, 0)
        # Splitters, not a fixed grid, so the panes are draggable. The two
        # rows' column positions are kept in step by _link_rows, so the
        # vertical divider reads as ONE line through the whole grid.
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
        self.setCentralWidget(host)

        self._panes = []
        for i in range(MAX_PANES):
            # Pane 0 gets the buffer we were opened for; the rest start empty
            # and are filled when the user picks a symbol for them.
            buf = buffer if i == 0 else self._empty_buffer()
            pane = BookmapPane(self, id(self), buf, self.tick, i, TimeAxisSecs)
            self._panes.append(pane)
            pane.main.getViewBox().sigRangeChangedManually.connect(
                lambda *_a, p=pane: self._on_manual(p))
            pane.glw.scene().sigMouseClicked.connect(
                lambda ev, p=pane: self._on_click(ev, p))
            pane.glw.scene().sigMouseMoved.connect(
                lambda pos, p=pane: self._on_mouse_move(pos, p))
            pane.sym_combo.currentTextChanged.connect(
                lambda t, p=pane: self._on_pane_symbol(p, t))
            pane.sym_combo.lineEdit().returnPressed.connect(
                lambda p=pane: self._on_pane_symbol(p, p.sym_combo.currentText()))
        self._active_pane = self._panes[0]
        self._bind_pane(self._panes[0])
        self._apply_layout(1)

        # TradingView-style ticker search: type a letter anywhere on the chart
        # and a floating box appears. A child of the plain host, NOT of the
        # splitter - a QLineEdit parented to a QSplitter becomes a splitter
        # section and would occupy a band of the grid.
        self.sym_search = QLineEdit(host)
        self.sym_search.setPlaceholderText("Type ticker, Enter to open")
        self.sym_search.setStyleSheet(
            "QLineEdit{background:#0A0D14;color:#F0F0F0;border:2px solid #2E9E7E;"
            " border-radius:8px;padding:8px 14px;font-size:15px;font-weight:700;"
            " letter-spacing:1px;}")
        self.sym_search.setFixedSize(240, 40)
        self.sym_search.hide()
        self.sym_search.returnPressed.connect(self._apply_sym_search)
        self.sym_search.installEventFilter(self)

    def _empty_buffer(self) -> BookmapBuffer:
        """A placeholder book for a pane with no symbol yet.

        Panes are built up front so a layout change never rebuilds plots, but
        a pane without a buffer would need a None check on every access. An
        empty buffer costs a few hundred bytes and removes that entirely.
        """
        b = BookmapBuffer("", self.tick)
        if b.max_cols < HISTORY_COLS:
            b.max_cols = HISTORY_COLS
        return b

    def _visible_panes(self) -> list:
        return self._panes[:self._n_panes]

    # Everything a pane owns is exposed here by DELEGATION, not by copying.
    # An earlier version assigned these in _bind_pane, and the copies went
    # stale the moment the pane changed one of them - set_row_ticks updated
    # pane.row_ticks while the window still reported the old value, so an
    # explicit price step silently stopped pinning the grid. Properties cannot
    # drift.
    def _bind_pane(self, pane) -> None:
        """Select `pane`. The delegating properties below do the rest."""
        self._active_pane = pane
        multi = self._n_panes > 1
        for p in self._panes:
            p.set_active_look(p is pane, multi)
        self.setWindowTitle(
            f"Omnitrix Bookmap — {pane.buffer.symbol or 'select a symbol'}")

    def _apply_layout(self, n: int) -> None:
        n = max(1, min(MAX_PANES, int(n)))
        rows, cols = {1: (1, 1), 2: (1, 2), 4: (2, 2)}[n]
        self._n_panes = n
        for pane in self._panes:
            pane.container.setVisible(False)
        for i in range(n):
            row = self._rows[i // cols]
            pane = self._panes[i]
            if pane.container.parent() is not row:
                row.addWidget(pane.container)
            pane.container.setVisible(True)
        self._rows[1].setVisible(rows > 1)
        for r in self._rows[:rows]:
            vis = r.count()
            if vis:
                r.setSizes([10_000 // vis] * vis)
        self._grid_host.setSizes([10_000 // rows] * rows)
        if self._active_pane not in self._visible_panes():
            self._bind_pane(self._panes[0])
        else:
            self._bind_pane(self._active_pane)
        for pane in self._visible_panes():
            if pane.buffer.symbol and pane.sym_combo.currentText() != pane.buffer.symbol:
                pane.sym_combo.blockSignals(True)
                pane.sym_combo.setCurrentText(pane.buffer.symbol)
                pane.sym_combo.blockSignals(False)

    def _link_rows(self, moved) -> None:
        """Keep both rows' column split identical, so the vertical divider is
        one continuous line rather than two that drift apart."""
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

    def _on_layout(self, txt: str) -> None:
        self._apply_layout(LAYOUTS.get(txt, 1))

    def _select_pane(self, pane) -> None:
        if pane is self._active_pane or pane not in self._visible_panes():
            return
        self._bind_pane(pane)
        self._sync_toolbar_to(pane)

    def _sync_toolbar_to(self, pane) -> None:
        """The toolbar describes whichever book is selected, so its controls
        are re-read from that pane - otherwise the next change would be applied
        from the wrong starting point."""
        for combo, value in ((self.type_combo, pane.style),):
            if value and combo.currentText() != value:
                combo.blockSignals(True)
                combo.setCurrentText(value)
                combo.blockSignals(False)
        self.lbl_step.setText(pane.lbl_step.text())

    def set_pane_symbol(self, pane, sym: str) -> None:
        """Point one pane at a symbol, leaving the others alone."""
        sym = (sym or "").strip().upper()
        if not sym or sym == pane.buffer.symbol:
            return
        owner = self.parent()
        buf = None
        if owner is not None and hasattr(owner, "_bookmap"):
            buf = owner._bookmap(sym)
            if buf.max_cols < HISTORY_COLS:
                buf.max_cols = HISTORY_COLS
        if buf is None:
            return
        pane.buffer = buf
        pane.tick = (owner.instruments.tick(sym)
                     if hasattr(owner, "instruments") else self.tick)
        # Every item caches the tick for its price->index maths.
        for it in (pane.heat, pane.bbo, pane.bubbles, pane.pie, pane.bars,
                   pane.dom_item, pane.vol_item, pane.projection, pane.sr_item):
            it.tick = pane.tick
        for it in (pane.bubbles, pane.pie, pane.bars):
            it.buffer = buf
        # A different instrument means the accumulated support/resistance and
        # the fitted price range describe the WRONG book. Reset both rather
        # than carry another symbol's levels onto this chart.
        pane.sr = SRTracker()
        pane._y_range = None
        pane._follow = True
        pane._auto_y = True
        if pane.sym_combo.currentText() != sym:
            pane.sym_combo.blockSignals(True)
            pane.sym_combo.setCurrentText(sym)
            pane.sym_combo.blockSignals(False)
        if pane is self._active_pane:
            self._bind_pane(pane)

    def _on_pane_symbol(self, pane, sym: str) -> None:
        self.set_pane_symbol(pane, sym)

    # ---- toolbar ---------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel("  Grid "))
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(list(LAYOUTS))
        self.layout_combo.setToolTip(
            "Show one, two or four order books in THIS window. Each keeps its "
            "own symbol, zoom and price grid; the highlighted one is what the "
            "toolbar acts on. Drag the dividers to resize.")
        self.layout_combo.currentTextChanged.connect(self._on_layout)
        tb.addWidget(self.layout_combo)

        tb.addWidget(QLabel("  Timeframe "))
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(list(TF))
        self.tf_combo.currentTextChanged.connect(self._on_tf)
        tb.addWidget(self.tf_combo)

        # Tape resolution, separate from the heatmap timeframe above.
        tb.addWidget(QLabel("   Tape "))
        self.btf_combo = QComboBox()
        self.btf_combo.addItems(list(BUBBLE_TF))
        self.btf_combo.setToolTip(
            "Time bin for the trade overlay, independent of the heatmap "
            "timeframe — keeps prints spread left-to-right instead of stacking "
            "into one vertical line on a coarse bookmap")
        self.btf_combo.currentTextChanged.connect(self._on_btf)
        tb.addWidget(self.btf_combo)

        tb.addWidget(QLabel("   Price "))
        self.step_combo = QComboBox()
        self.step_combo.addItems(list(PRICE_STEP))
        self.step_combo.setToolTip(
            "Collapse this many ticks into one heatmap row. Coarser rows draw "
            "thicker, readable bands instead of overlapping hairlines, and "
            "sizes are summed within each band.\n"
            "Auto follows the zoom: fine detail zoomed in, thick bands zoomed "
            "out, without touching the control.")
        self.step_combo.currentTextChanged.connect(self._on_step)
        tb.addWidget(self.step_combo)
        # Auto is otherwise opaque - show which grid it settled on.
        self.lbl_step = QLabel("")
        self.lbl_step.setStyleSheet("color:#8A93A6;font-weight:600;")
        tb.addWidget(self.lbl_step)

        tb.addWidget(QLabel("   Type "))
        self.type_combo = QComboBox()
        # Bubbles first = default. Volume dots are what Bookmap actually draws;
        # pies and split-bars are Omnitrix additions and belong behind it.
        self.type_combo.addItems(["Bubbles", "Pie", "Bars"])
        self.type_combo.currentTextChanged.connect(self._on_style)
        tb.addWidget(self.type_combo)

        tb.addWidget(QLabel("   Size "))
        self.size_combo = QComboBox()
        self.size_combo.addItems(list(SIZE_STEPS))
        self.size_combo.setCurrentText("100%")
        self.size_combo.setToolTip("Scale the trade glyphs")
        self.size_combo.currentTextChanged.connect(self._on_size)
        tb.addWidget(self.size_combo)

        tb.addWidget(QLabel("   Min trade "))
        self.min_combo = QComboBox()
        self.min_combo.addItems(["All", "100", "250", "500", "1K", "2.5K", "5K", "10K"])
        # 500 was tuned against the dense synthetic tape; on a live feed quieter
        # symbols fall under it and their pies blink on/off as volume crosses
        # the threshold. 100 keeps the clustering benefit without that.
        self.min_combo.setCurrentText("100")
        self.min_combo.currentTextChanged.connect(self._on_minsize)
        tb.addWidget(self.min_combo)

        # ---- Display menu -------------------------------------------------
        # Walls, Fade gaps, S/R, Volume, Look and Focus were six controls
        # strung across the toolbar, which pushed the zoom and Follow buttons
        # off the right-hand edge in a grid layout. They are settings you
        # change occasionally, not controls you reach for every minute.
        #
        # QAction rather than QCheckBox for the toggles: same
        # isChecked/setChecked/toggled API, so nothing that reads them changes.
        self.menu_display = QMenu("Display", self)
        btn_display = QToolButton()
        btn_display.setText("Display ▾")
        btn_display.setMenu(self.menu_display)
        btn_display.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(btn_display)

        def _combo_row(label, items, current, slot, tip=""):
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(24, 2, 10, 2)
            lay.addWidget(QLabel(label))
            c = QComboBox()
            c.addItems(list(items))
            if current:
                c.setCurrentText(current)
            if tip:
                c.setToolTip(tip)
            c.currentTextChanged.connect(slot)
            lay.addWidget(c)
            act = QWidgetAction(self)
            act.setDefaultWidget(w)
            self.menu_display.addAction(act)
            return c

        def _toggle(label, checked, slot, tip=""):
            act = self.menu_display.addAction(label)
            act.setCheckable(True)
            act.setChecked(checked)
            if tip:
                act.setToolTip(tip)
            act.toggled.connect(slot)
            return act

        self.look_combo = _combo_row(
            "Look", LOOKS, None, self._on_look,
            "Colour scheme for the liquidity field")
        self.recency_combo = _combo_row(
            "Focus", RECENCY, None, self._on_recency,
            "Live gradient: fade older columns so the field is dominated by "
            "current liquidity")
        self.wall_combo = _combo_row(
            "Walls", ["Sensitive", "Normal", "Strict"], "Normal", self._on_wall,
            "How readily a resting level counts as a wall")

        self.menu_display.addSeparator()
        # Honesty switch. The heatmap forward-fills a ladder across columns
        # that received no sweep, which is right for a wall that is genuinely
        # still resting - but it makes unmeasured time indistinguishable from
        # stable time. On (the default) fades those columns.
        self.chk_gaps = _toggle(
            "Fade gaps", True, self._on_gaps,
            "Fade columns that received no book sweep, so liquidity that was "
            "measured is visibly distinct from liquidity that was assumed")
        self.chk_vol = _toggle("Volume pane", True, self._on_volpane,
                               "Show the bottom volume pane")
        # OFF by default: the S/R lines are drawn full width across the field,
        # and on a fresh book they are the least-supported thing on screen -
        # they need minutes of history before they mean anything. Opt in.
        self.chk_sr = _toggle(
            "S/R lines", False, self._on_sr,
            "Show the persistence-weighted support and resistance lines")

        self.btn_follow = QPushButton("⏵ Follow")
        self.btn_follow.clicked.connect(self._reset_view)
        tb.addWidget(self.btn_follow)

        self.btn_out = QPushButton("－")
        self.btn_out.clicked.connect(lambda: self._zoom(1.25))
        tb.addWidget(self.btn_out)
        self.btn_in = QPushButton("＋")
        self.btn_in.clicked.connect(lambda: self._zoom(0.8))
        tb.addWidget(self.btn_in)

        self.setStyleSheet(
            "QMainWindow{background:#0A0D14;}"
            "QToolBar{background:#05070C;border:none;padding:4px;spacing:4px;}"
            "QLabel{color:#C7CCD6;font-size:13px;font-weight:600;}"
            "QCheckBox{color:#C7CCD6;font-size:13px;font-weight:600;"
            " padding:0 6px;}"
            "QComboBox{background:#161A21;color:#EFEFEF;border:1px solid #282C34;"
            " border-radius:4px;padding:3px 8px;font-size:13px;}"
            "QPushButton{background:#161A21;color:#EFEFEF;border:1px solid #282C34;"
            " border-radius:4px;padding:4px 10px;font-weight:600;}"
            "QPushButton:hover{background:#282C34;}"
        )

    # ---- plots -----------------------------------------------------------
    # ---- ticker search ---------------------------------------------------
    def keyPressEvent(self, ev) -> None:
        if not self.sym_search.isVisible():
            t = ev.text()
            if t and t.isalpha():
                se = self.sym_search
                se.move(max(8, (self.glw.width() - se.width()) // 2), 12)
                se.setText(t.upper())
                se.show(); se.raise_(); se.setFocus(); se.end(False)
                return
        super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):
        if obj is self.sym_search and ev.type() == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Escape:
                self.sym_search.hide()
                self.glw.setFocus()
                return True
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        se = getattr(self, "sym_search", None)
        if se is not None and se.isVisible():
            se.move(max(8, (self.glw.width() - se.width()) // 2), 12)

    def _apply_sym_search(self) -> None:
        sym = self.sym_search.text().strip().upper()
        self.sym_search.hide()
        self.glw.setFocus()
        if not sym or sym == self.buffer.symbol:
            return
        # Routed through the main window, which owns the per-symbol buffers and
        # the child-window registry - opening one from here directly would
        # bypass both and leak a window per search.
        owner = self.parent()
        opener = getattr(owner, "open_bookmap_for", None)
        if callable(opener):
            opener(sym)

    # ---- interaction -----------------------------------------------------
    def _on_manual(self, pane=None):
        pane = pane or self._active_pane
        # Touching a book is also how you select it in a grid.
        if pane is not self._active_pane:
            self._select_pane(pane)
        vb = pane.main.getViewBox()
        vr = vb.viewRect()
        cols = pane.buffer.view(pane.agg)

        # If they manually panned away from the live edge, stop following.
        # But if they just zoomed while near the live edge, keep following the
        # x-axis, just disable auto_y so they keep their vertical zoom.
        if cols:
            latest = cols[-1]
            x_hi = latest.bucket + pane.proj_width + 3
            if abs(vr.right() - x_hi) < max(5.0, (vr.right() - vr.left()) * 0.1):
                pane._auto_y = False
                if pane is self._active_pane:
                    self._auto_y = False
                return

        pane._follow = False
        pane._auto_y = False
        if pane is self._active_pane:
            self._follow = False
            self._auto_y = False
            self.btn_follow.setText("⏸ Live")

    def _on_click(self, ev, pane=None):
        # Clicking a book selects it, before anything else acts on it.
        if pane is not None and pane is not self._active_pane:
            self._select_pane(pane)
            return
        if ev.double():
            pane = pane or self._active_pane
            pane._auto_y = self._auto_y = True
            self._reset_view()

    def _on_mouse_move(self, pos, pane=None) -> None:
        # Each pane owns its scene, so a move here IS a move on this pane - no
        # hit-testing across four books, and the readout cannot land on the
        # wrong one. Only the selected book tracks the pointer.
        if pane is not None and pane is not self._active_pane:
            return
        """Crosshair + readout: price, clock time, resting size at that price
        and its distance from the last trade."""
        vb = self.main.getViewBox()
        if not self.main.sceneBoundingRect().contains(pos):
            for it in (self.cx_v, self.cx_h, self.readout):
                it.setVisible(False)
            self.xhair.hide()
            return
        mp = vb.mapSceneToView(pos)
        price, x = mp.y(), mp.x()
        self.cx_v.setPos(x)
        self.cx_h.setPos(price)
        self.xhair.set(x, price)

        ti = int(round(price / self.tick))
        cols = self.buffer.view(self.agg)
        latest = cols[-1] if cols else None

        secs = x * self.buffer.col_dt * self.agg
        when = clock_label(secs)

        size = latest.book.get(ti, 0) if latest and latest.book else 0
        lines = [f"{price:,.{self._dp()}f}   {when}"]
        if size:
            side = ""
            if latest.ask_ti is not None and ti >= latest.ask_ti:
                side = " ask"
            elif latest.bid_ti is not None and ti <= latest.bid_ti:
                side = " bid"
            lines.append(f"resting {size:,}{side}")
        if latest is not None and latest.bid_ti is not None \
                and latest.ask_ti is not None:
            mid = (latest.bid_ti + latest.ask_ti) / 2 * self.tick
            d = price - mid
            lines.append(f"{d:+,.{self._dp()}f} from last")

        # What the trade glyph under the cursor is made of. "A big green circle"
        # is only half the read - the split between aggressive buying and
        # selling inside it is the other half, and there was no way to get it.
        hit = self._glyph_at(x, price, vb)
        if hit is not None:
            gx, gy, gb, gs = hit
            tot = gb + gs
            lines.append(f"── trade  {tot:,} sh")
            lines.append(f"   buy  {gb:,}   ({gb / tot * 100:.0f}%)")
            lines.append(f"   sell {gs:,}   ({gs / tot * 100:.0f}%)")
            lines.append(f"   delta {gb - gs:+,}")

        # How much of the column under the cursor was actually measured. A
        # forward-filled column looks solid, so without this there is no way to
        # tell an unbroken wall from a stretch where no sweep arrived.
        hb = int(x)
        hov = next((c for c in cols if c.bucket == hb), None)
        if hov is None:
            lines.append("no column — carried forward")
        elif hov.sweeps == 0:
            lines.append("no sweep — carried forward")
        else:
            lines.append(f"{hov.sweeps} sweep{'s' if hov.sweeps != 1 else ''}")

        self.readout.setText("\n".join(lines))
        self.readout.setPos(x, price)
        for it in (self.cx_v, self.cx_h, self.readout):
            it.setVisible(True)

    def _glyph_at(self, x: float, price: float, vb):
        """The trade glyph nearest the cursor, or None.

        Matched in SCREEN space, not data space: a bubble is a circle of pixels,
        so a tolerance in ticks would be wrong at one zoom and useless at
        another. Whichever overlay is visible publishes what it drew last frame.
        """
        item = (self.bubbles if self.style == "Bubbles"
                else self.pie if self.style == "Pie" else self.bars)
        drawn = getattr(item, "drawn", None)
        if not drawn:
            return None
        try:
            cur = vb.mapViewToScene(QPointF(x, price))
        except Exception:
            return None
        best, best_d2 = None, 26.0 ** 2        # ~26 px grab radius
        for gx, gy, gb, gs in drawn:
            pt = vb.mapViewToScene(QPointF(gx, gy))
            dx = pt.x() - cur.x()
            dy = pt.y() - cur.y()
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best, best_d2 = (gx, gy, gb, gs), d2
        return best

    def _dp(self) -> int:
        """Decimal places implied by the tick size."""
        t = self.tick
        if t >= 1:
            return 0
        return max(0, min(6, len(f"{t:.6f}".rstrip('0').split('.')[-1])))

    def _reset_view(self):
        p = self._active_pane
        p._follow = self._follow = True
        # We don't force _auto_y = True here so the user keeps their vertical zoom!
        # Double-clicking the chart will re-enable _auto_y.
        self.btn_follow.setText("⏵ Follow")
        self.refresh()

    def _zoom(self, factor: float):
        self._active_pane._follow = self._follow = False
        vb = self.main.getViewBox()
        vb.scaleBy((factor, 1.0))            # zoom time axis about centre

    def _on_tf(self, txt: str):
        p = self._active_pane
        p.agg = self.agg = TF.get(txt, 1)
        p.apply_tape()
        self._reset_view()

    def _on_minsize(self, txt: str):
        mult = {"All": 0, "100": 100, "250": 250, "500": 500, "1K": 1000,
                "2.5K": 2500, "5K": 5000, "10K": 10000}
        m = mult.get(txt, 0)
        self.bubbles.min_size = self.pie.min_size = self.bars.min_size = m
        self.bubbles.update(); self.pie.update(); self.bars.update()

    def _on_gaps(self, on: bool):
        self.heat.dim_unobserved = on
        self.heat.update()

    def _on_btf(self, txt: str):
        p = self._active_pane
        p.bubble_bin = self.bubble_bin = float(BUBBLE_TF.get(txt, 1))
        p.apply_tape()
        self.refresh()

    def _on_step(self, txt: str):
        dollars = PRICE_STEP.get(txt, 0.0)
        # -1 = Auto (resolved per refresh from the zoom); 0 = exactly one tick,
        # whatever the instrument's tick happens to be.
        p = self._active_pane
        p.auto_step = self.auto_step = dollars < 0
        if not p.auto_step:
            self.lbl_step.setText("")          # the combo already names it
            p.lbl_step.setText("")
            p.set_row_ticks(1 if dollars <= 0 else
                            max(1, int(round(dollars / p.tick))))
        else:
            p.resolve_auto_step()
        self.refresh()

    def _on_size(self, txt: str):
        sc = SIZE_STEPS.get(txt, 1.0)
        for it in (self.bubbles, self.pie, self.bars):
            it.size_scale = sc
            it.update()

    def _on_sr(self, on: bool):
        self.sr_item.setVisible(on)

    def _on_volpane(self, on: bool):
        self.vol.setVisible(on)
        # Collapse the row entirely, otherwise hiding the plot leaves its empty
        # band holding a fifth of the window.
        self.glw.ci.layout.setRowStretchFactor(1, 1 if on else 0)
        self.glw.ci.layout.setRowMinimumHeight(1, 0)

    def _on_look(self, txt: str):
        lut, bg = LOOKS.get(txt, LOOKS["Bookmap"])
        self.heat.lut = lut
        self.glw.setBackground(bg)
        self.heat.update()

    def _on_recency(self, txt: str):
        self.heat.recency = RECENCY.get(txt, 0.0)
        self.heat.update()

    def _apply_tape(self) -> None:
        """Push the tape binning onto the ACTIVE pane's three overlays."""
        p = self._active_pane
        p.bubble_bin = self.bubble_bin
        p.agg = self.agg
        p.apply_tape()

    def _on_wall(self, txt: str):
        mult, floor = {"Sensitive": (2.5, 2000),
                       "Normal": (4.0, 4000),
                       "Strict": (7.0, 10000)}.get(txt, (4.0, 4000))
        self.projection.wall_mult = mult
        self.projection.wall_floor = floor
        self.projection.update()

    def _on_style(self, txt: str):
        self._active_pane.style = txt
        self.style = txt
        self.bubbles.setVisible(txt == "Bubbles")
        self.pie.setVisible(txt == "Pie")
        self.bars.setVisible(txt == "Bars")
        self.refresh()

    # ---- lifecycle -------------------------------------------------------
    def changeEvent(self, ev):
        """Follow the user's attention.

        Whichever window is active is the one whose frame rate can actually be
        perceived, so it is the one the budget protects.
        """
        from PyQt6.QtCore import QEvent as _QEvent
        if ev.type() == _QEvent.Type.ActivationChange and self.isActiveWindow():
            GOVERNOR.set_focus(id(self))
        super().changeEvent(ev)

    def closeEvent(self, ev):
        self._timer.release()
        super().closeEvent(ev)

    # ---- data + view -----------------------------------------------------
    def refresh(self, initial: bool = False) -> None:
        # A window you cannot see does not need live data. Measured, paint is
        # 95% of the cost, so skipping an unseen one is the cheapest frame in
        # the app - and returning early is not enough, because the governor
        # would go on counting this window's cost against the shared budget
        # and throttling the window you ARE looking at.
        if not self.isVisible() or self.isMinimized():
            GOVERNOR.set_alive(id(self), False)
            return
        GOVERNOR.set_alive(id(self), True)
        for pane in self._visible_panes():
            self._refresh_pane(pane, initial)

    def _refresh_pane(self, pane, initial: bool = False) -> None:
        # Before anything reads row_ticks: a zoom changes the right grid, and
        # this timer is what notices.
        pane.resolve_auto_step()
        if pane is self._active_pane:
            self.lbl_step.setText(pane.lbl_step.text())
        cols = pane.buffer.view(pane.agg)
        if not cols:
            return
        pane.heat.set_cols(cols)
        pane.bbo.set_cols(cols)
        # All three overlays read the tape directly, so they only need the
        # columns for their bounding rect - and they need it whether visible or
        # not, so switching type does not show a stale extent for one frame.
        for it in (pane.bubbles, pane.pie, pane.bars):
            it.xscale = 1.0 / pane.agg
            it.bin_cols = pane.bubble_bin
            it.row_ticks = pane.row_ticks
            it.set_cols(cols)
        pane.vol_item.set_cols(cols)
        latest = cols[-1]
        # The newest column may have been created by a trade and carry no book;
        # fall back to the newest one that does so the DOM/projection hold.
        book_col = latest if latest.book else (pane.buffer.latest_book() or latest)
        pane.dom_item.set_col(book_col, pane.tick)
        pane.cursor.setPos(latest.bucket + 1)

        mid_ti = None
        if latest.bid_ti is not None and latest.ask_ti is not None:
            mid_ti = (latest.bid_ti + latest.ask_ti) / 2
        elif pane.buffer.trades:
            mid_ti = pane.buffer.trades[-1][1]
        if mid_ti is not None:
            pane.price_line.setPos(mid_ti * pane.tick)

        # project the resting book as fat bands just ahead of the latest pie
        vmax = 1
        for c in cols[-60:]:
            if c.book:
                m = c.book.max_size()
                if m > vmax:
                    vmax = m
        pane.projection.set_projection(book_col, latest.bucket + 1, mid_ti, vmax)

        # Persistence-weighted S/R, shared by the projection bands and the
        # full-width lines so the two always name the same levels.
        sup, res = pane.sr.update(cols, mid_ti)
        pane.projection.set_sr(sup, res)
        pane.sr_item.set_levels(sup, res, cols[0].bucket,
                                latest.bucket + pane.proj_width + 1)

        if book_col.book:
            # Exact, no margin: the ladder's bars are right-anchored, so the
            # deepest level must land flush against the price axis.
            pane.dom.setXRange(0, pane.dom_item.vmax or 1, padding=0)

        if initial or pane._follow:
            width = pane.view_width(cols, default=60)
            x_hi = latest.bucket + pane.proj_width + 3   # room for projection
            pane.main.setXRange(x_hi - width, x_hi, padding=0)
        if initial or pane._auto_y:
            pane.fit_price(cols)

class TimeAxisSecs(pg.AxisItem):
    """Formats aggregated column-bucket x values as HH:MM:SS."""

    def __init__(self, *a, win=None, **k):
        super().__init__(*a, **k)
        self.win = win

    def tickStrings(self, values, scale, spacing):
        """Column bucket -> clock, with the axis range guarded.

        `time.localtime()` raises OSError on a negative or absurdly large
        value, and the axis asks for ticks across the whole VIEW - which
        extends past the data whenever you pan or zoom out beyond it. The
        exception came out of paint(), which aborts the render half-drawn:
        that is both a blank axis and a plausible source of the leftover
        smears on screen. Out-of-range ticks get an empty label instead.
        """
        dt = (self.win.buffer.col_dt * self.win.agg) if self.win else 1.0
        # One shared guard rather than a hand-rolled range test per axis: this
        # bug has now surfaced in four separate places, each with its own
        # slightly different check.
        return [clock_label(v * dt) for v in values]
