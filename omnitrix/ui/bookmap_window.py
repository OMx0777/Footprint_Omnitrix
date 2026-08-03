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
)

from ..engine import BookmapBuffer, SRTracker
from ..render import (
    BookHeatmapItem, BBOItem, BubbleItem, PieItem, BarsItem, ProjectionItem,
    DomLadderItem, VolumeBarsItem, SRLinesItem,
)
from ..render.bookmap import LOOK_LUTS as LOOKS

# label -> live-gradient strength for the heatmap's recency weighting
RECENCY = {"Off": 0.0, "Light": 0.35, "Medium": 0.65, "Strong": 1.0}

from ..render.bookmap import BOOKMAP_BG as BG
from ..render.pricegrid import auto_step_ticks, TARGET_PX_BAND

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
        self.buffer = buffer
        self.tick = tick
        self.agg = 1
        self.bubble_bin = 1.0
        self.row_ticks = 1
        # Must agree with the step combo's default item, which is the first key
        # of PRICE_STEP: the handler is connected after addItems, so selecting
        # the default never fires it and nothing else would sync this.
        # The first refresh() resolves the actual grid.
        self.auto_step = next(iter(PRICE_STEP.values())) < 0
        self.setWindowTitle(f"Omnitrix Bookmap — {buffer.symbol}")
        self.resize(1500, 860)
        self._follow = True
        self._auto_y = True
        # Deepen this symbol's ring so the long timeframes have data to
        # aggregate; a 1-hour view over a 23-minute ring is one column.
        if buffer.max_cols < HISTORY_COLS:
            buffer.max_cols = HISTORY_COLS

        pg.setConfigOptions(useOpenGL=False, antialias=False)
        self._build_toolbar()
        self._build_plots()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(80)                 # ~12.5 fps data refresh
        self.refresh(initial=True)

    # ---- toolbar ---------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
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

        tb.addWidget(QLabel("   Walls "))
        self.wall_combo = QComboBox()
        self.wall_combo.addItems(["Sensitive", "Normal", "Strict"])
        self.wall_combo.setCurrentText("Normal")
        self.wall_combo.currentTextChanged.connect(self._on_wall)
        tb.addWidget(self.wall_combo)

        # Honesty switch. The heatmap forward-fills a ladder across columns that
        # received no sweep, which is right for a wall that is genuinely still
        # resting - but it makes unmeasured time indistinguishable from stable
        # time. On (the default) fades those columns; off restores the solid
        # field for a cleaner screenshot.
        self.chk_gaps = QCheckBox("Fade gaps")
        self.chk_gaps.setChecked(True)
        self.chk_gaps.setToolTip(
            "Fade columns that received no book sweep, so liquidity that was "
            "measured is visibly distinct from liquidity that was assumed")
        self.chk_gaps.toggled.connect(self._on_gaps)
        tb.addWidget(self.chk_gaps)

        self.chk_sr = QCheckBox("S/R")
        self.chk_sr.setChecked(True)
        self.chk_sr.setToolTip("Show the persistence-weighted support and "
                               "resistance lines")
        self.chk_sr.toggled.connect(self._on_sr)
        tb.addWidget(self.chk_sr)

        self.chk_vol = QCheckBox("Volume")
        self.chk_vol.setChecked(True)
        self.chk_vol.setToolTip("Show the bottom volume pane")
        self.chk_vol.toggled.connect(self._on_volpane)
        tb.addWidget(self.chk_vol)

        tb.addWidget(QLabel("   Look "))
        self.look_combo = QComboBox()
        self.look_combo.addItems(list(LOOKS))
        self.look_combo.setToolTip("Colour scheme for the liquidity field")
        self.look_combo.currentTextChanged.connect(self._on_look)
        tb.addWidget(self.look_combo)

        tb.addWidget(QLabel(" Focus "))
        self.recency_combo = QComboBox()
        self.recency_combo.addItems(list(RECENCY))
        self.recency_combo.setToolTip(
            "Live gradient: fade older columns so the field is dominated by "
            "current liquidity — makes the magnets in front of price stand out "
            "instead of competing with history")
        self.recency_combo.currentTextChanged.connect(self._on_recency)
        tb.addWidget(self.recency_combo)

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
            "QMainWindow{background:#1A2226;}"
            "QToolBar{background:#0C111A;border:none;padding:4px;spacing:4px;}"
            "QLabel{color:#C7CCD6;font-size:13px;font-weight:600;}"
            "QCheckBox{color:#C7CCD6;font-size:13px;font-weight:600;"
            " padding:0 6px;}"
            "QComboBox{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:3px 8px;font-size:13px;}"
            "QPushButton{background:#1C2230;color:#EFEFEF;border:1px solid #2A3140;"
            " border-radius:4px;padding:4px 10px;font-weight:600;}"
            "QPushButton:hover{background:#263042;}"
        )

    # ---- plots -----------------------------------------------------------
    def _build_plots(self) -> None:
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(BG)
        self.setCentralWidget(self.glw)

        self.main = self.glw.addPlot(row=0, col=0)
        self.main.showAxis("right"); self.main.hideAxis("left")
        self.main.hideAxis("bottom")
        # No grid on the liquidity pane: Bookmap keeps the canvas clean so the
        # heat field is the only thing carrying colour. Gridlines over a black
        # background read as banding in the thin-liquidity tail.
        self.main.showGrid(x=False, y=False)
        vb = self.main.getViewBox()
        vb.setMouseMode(pg.ViewBox.PanMode)          # left-drag pans
        vb.setMouseEnabled(x=True, y=True)

        self.dom = self.glw.addPlot(row=0, col=1)
        self.dom.hideAxis("left"); self.dom.showAxis("right")
        self.dom.hideAxis("bottom")
        self.dom.setYLink(self.main)
        self.dom.setMouseEnabled(x=False, y=False)

        self.vol_axis = TimeAxisSecs(orientation="bottom", win=self)
        self.vol = self.glw.addPlot(row=1, col=0, axisItems={"bottom": self.vol_axis})
        self.vol.hideAxis("left"); self.vol.showAxis("right")
        self.vol.setXLink(self.main)
        self.vol.setMouseEnabled(y=False)

        self.glw.ci.layout.setRowStretchFactor(0, 5)
        self.glw.ci.layout.setRowStretchFactor(1, 1)
        self.glw.ci.layout.setColumnStretchFactor(0, 14)
        self.glw.ci.layout.setColumnStretchFactor(1, 1)

        for plot in (self.main, self.dom, self.vol):
            for ax in ("right", "bottom"):
                a = plot.getAxis(ax)
                a.setPen(pg.mkPen("#243040")); a.setTextPen(pg.mkPen("#8A93A6"))

        self.heat = BookHeatmapItem(self.tick)
        self.bbo = BBOItem(self.tick)
        self.bubbles = BubbleItem(self.tick, self.buffer)
        self.main.addItem(self.heat)
        self.main.addItem(self.bbo)
        self.main.addItem(self.bubbles)

        self.pie = PieItem(self.tick, self.buffer)
        self.bars = BarsItem(self.tick, self.buffer)
        self.main.addItem(self.pie)
        self.main.addItem(self.bars)

        self.bubbles.min_size = 100          # default noise filter (matches combo)
        self.pie.min_size = 100
        self.bars.min_size = 100
        self.style = "Bubbles"               # Bookmap-style volume dots
        self.bubbles.setVisible(True)
        self.bars.setVisible(False)
        self.pie.setVisible(False)

        self.dom_item = DomLadderItem(self.tick)
        self.dom.addItem(self.dom_item)
        self.vol_item = VolumeBarsItem(self.tick)
        self.vol.addItem(self.vol_item)

        self.cursor = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen("#E8C13A", width=1))
        self.main.addItem(self.cursor, ignoreBounds=True)
        self.price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#D8DCE4", width=1, style=Qt.PenStyle.DashLine),
            label="{value:.2f}",
            labelOpts={"position": 0.98, "color": "#0A0E16",
                       "fill": "#D8DCE4", "movable": False})
        self.main.addItem(self.price_line, ignoreBounds=True)

        # resting limit orders projected as fat bands just ahead of price
        # (heatmap-coloured; strongest support GREEN, resistance RED).
        self.projection = ProjectionItem(self.tick)
        self.main.addItem(self.projection)
        self.proj_width = 7

        # Absolute support / resistance: the levels that have actually held,
        # drawn full width so price can be watched approaching them.
        self.sr = SRTracker()
        self.sr_item = SRLinesItem(self.tick)
        self.main.addItem(self.sr_item)

        # ---- crosshair with live price / time / liquidity readout ----
        self.cx_v = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen("#7E8AA0", width=1,
                                                 style=Qt.PenStyle.DashLine))
        self.cx_h = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#7E8AA0", width=1, style=Qt.PenStyle.DashLine),
            label="{value:.2f}",
            labelOpts={"position": 0.02, "color": "#0A0E16",
                       "fill": "#7E8AA0", "movable": False})
        for ln in (self.cx_v, self.cx_h):
            ln.setVisible(False)
            self.main.addItem(ln, ignoreBounds=True)

        self.readout = pg.TextItem(anchor=(0, 0), color="#D8DCE4",
                                   fill=pg.mkBrush(12, 24, 40, 215))
        self.readout.setZValue(50)
        self.readout.setVisible(False)
        self.main.addItem(self.readout, ignoreBounds=True)

        # TradingView-style ticker search: type a letter anywhere on the chart
        # and a floating box appears; Enter opens that symbol's bookmap, Escape
        # cancels. A child of the chart widget so it floats over the plot.
        self.sym_search = QLineEdit(self.glw)
        self.sym_search.setPlaceholderText("Type ticker, Enter to open")
        self.sym_search.setStyleSheet(
            "QLineEdit{background:#12161F;color:#F0F0F0;border:2px solid #26A69A;"
            " border-radius:8px;padding:8px 14px;font-size:15px;font-weight:700;"
            " letter-spacing:1px;}")
        self.sym_search.setFixedSize(240, 40)
        self.sym_search.hide()
        self.sym_search.returnPressed.connect(self._apply_sym_search)
        self.sym_search.installEventFilter(self)

        vb.sigRangeChangedManually.connect(self._on_manual)
        self.glw.scene().sigMouseClicked.connect(self._on_click)
        self.glw.scene().sigMouseMoved.connect(self._on_mouse_move)

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
    def _on_manual(self):
        vb = self.main.getViewBox()
        vr = vb.viewRect()
        cols = self.buffer.view(self.agg)
        
        # If they manually panned away from the live edge, stop following.
        # But if they just zoomed while near the live edge, keep following the x-axis, 
        # just disable auto_y so they keep their vertical zoom.
        if cols:
            latest = cols[-1]
            x_hi = latest.bucket + self.proj_width + 3
            if abs(vr.right() - x_hi) < max(5.0, (vr.right() - vr.left()) * 0.1):
                self._auto_y = False
                return

        self._follow = False
        self._auto_y = False
        self.btn_follow.setText("⏸ Live")

    def _on_click(self, ev):
        if ev.double():
            self._auto_y = True
            self._reset_view()

    def _on_mouse_move(self, pos) -> None:
        """Crosshair + readout: price, clock time, resting size at that price
        and its distance from the last trade."""
        vb = self.main.getViewBox()
        if not self.main.sceneBoundingRect().contains(pos):
            for it in (self.cx_v, self.cx_h, self.readout):
                it.setVisible(False)
            return
        mp = vb.mapSceneToView(pos)
        price, x = mp.y(), mp.x()
        self.cx_v.setPos(x)
        self.cx_h.setPos(price)

        ti = int(round(price / self.tick))
        cols = self.buffer.view(self.agg)
        latest = cols[-1] if cols else None

        secs = x * self.buffer.col_dt * self.agg
        when = time.strftime("%H:%M:%S", time.localtime(secs))

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
        self._follow = True
        # We don't force _auto_y = True here so the user keeps their vertical zoom!
        # Double-clicking the chart will re-enable _auto_y.
        self.btn_follow.setText("⏵ Follow")
        self.refresh()

    def _zoom(self, factor: float):
        self._follow = False
        vb = self.main.getViewBox()
        vb.scaleBy((factor, 1.0))            # zoom time axis about centre

    def _on_tf(self, txt: str):
        self.agg = TF.get(txt, 1)
        self._apply_tape()
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
        self.bubble_bin = float(BUBBLE_TF.get(txt, 1))
        self._apply_tape()
        self.refresh()

    def _on_step(self, txt: str):
        dollars = PRICE_STEP.get(txt, 0.0)
        # -1 = Auto (resolved per refresh from the zoom); 0 = exactly one tick,
        # whatever the instrument's tick happens to be.
        self.auto_step = dollars < 0
        if not self.auto_step:
            self.lbl_step.setText("")          # the combo already names it
            self._set_row_ticks(1 if dollars <= 0 else
                                max(1, int(round(dollars / self.tick))))
        else:
            self._resolve_auto_step()
        self.refresh()

    def _set_row_ticks(self, rt: int) -> bool:
        """Push one price grid to every consumer. Returns True if it changed.

        Five items draw on this grid - the heat field, the DOM ladder and the
        three tape overlays - and they must agree: a ladder bucketed differently
        from the field behind it lines up with nothing, which is worse than
        either grid alone.
        """
        if rt == self.row_ticks:
            return False
        self.row_ticks = rt
        self.heat.row_ticks = rt
        self.dom_item.row_ticks = rt
        self._apply_tape()
        return True

    def _resolve_auto_step(self) -> bool:
        """Pick the grid from the current zoom. No-op unless Auto is selected.

        Resolved here rather than inside each item's paint (as the footprint
        does) precisely because five items share this grid - letting each derive
        its own from its own viewport would let the DOM ladder and the field
        disagree. The DOM y-axis is linked to the main plot, so one reading
        serves both.

        Targets a band rather than a text row: the heat field only has to stay
        a visible band, and forcing footprint-sized rows here would throw away
        most of the depth resolution the feed provides.
        """
        if not self.auto_step:
            return False
        vb = self.main.getViewBox()
        if vb is None:
            return False
        px_h = vb.viewPixelSize()[1]
        changed = self._set_row_ticks(
            auto_step_ticks(px_h, self.tick, TARGET_PX_BAND))
        px = self.row_ticks * self.tick
        self.lbl_step.setText(f"({px * 100:.0f}¢)" if px < 1.0
                              else f"(${px:,.2f})".replace(".00", ""))
        return changed

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
        """Push the tape binning onto all three overlays at once."""
        for it in (self.bubbles, self.pie, self.bars):
            it.bin_cols = self.bubble_bin
            it.row_ticks = self.row_ticks
            it.xscale = 1.0 / self.agg
            it.update()

    def _on_wall(self, txt: str):
        mult, floor = {"Sensitive": (2.5, 2000),
                       "Normal": (4.0, 4000),
                       "Strict": (7.0, 10000)}.get(txt, (4.0, 4000))
        self.projection.wall_mult = mult
        self.projection.wall_floor = floor
        self.projection.update()

    def _on_style(self, txt: str):
        self.style = txt
        self.bubbles.setVisible(txt == "Bubbles")
        self.pie.setVisible(txt == "Pie")
        self.bars.setVisible(txt == "Bars")
        self.refresh()

    # ---- data + view -----------------------------------------------------
    def refresh(self, initial: bool = False) -> None:
        # Before anything reads row_ticks: a zoom changes the right grid, and
        # this timer is what notices.
        self._resolve_auto_step()
        cols = self.buffer.view(self.agg)
        if not cols:
            return
        self.heat.set_cols(cols)
        self.bbo.set_cols(cols)
        # All three overlays read the tape directly now, so they only need the
        # columns for their bounding rect - and they need it whether visible or
        # not, so switching type does not show a stale extent for one frame.
        for it in (self.bubbles, self.pie, self.bars):
            it.xscale = 1.0 / self.agg
            it.bin_cols = self.bubble_bin
            it.row_ticks = self.row_ticks
            it.set_cols(cols)
        self.vol_item.set_cols(cols)
        latest = cols[-1]
        # The newest column may have been created by a trade and carry no book;
        # fall back to the newest one that does so the DOM/projection hold.
        book_col = latest if latest.book else (self.buffer.latest_book() or latest)
        self.dom_item.set_col(book_col, self.tick)
        self.cursor.setPos(latest.bucket + 1)

        mid_ti = None
        if latest.bid_ti is not None and latest.ask_ti is not None:
            mid_ti = (latest.bid_ti + latest.ask_ti) / 2
        elif self.buffer.trades:
            mid_ti = self.buffer.trades[-1][1]
        if mid_ti is not None:
            self.price_line.setPos(mid_ti * self.tick)

        # project the resting book as fat bands just ahead of the latest pie
        vmax = 1
        for c in cols[-60:]:
            if c.book:
                m = c.book.max_size()
                if m > vmax:
                    vmax = m
        self.projection.set_projection(book_col, latest.bucket + 1, mid_ti, vmax)

        # Persistence-weighted S/R, shared by the projection bands and the
        # full-width lines so the two always name the same levels.
        sup, res = self.sr.update(cols, mid_ti)
        self.projection.set_sr(sup, res)
        self.sr_item.set_levels(sup, res, cols[0].bucket,
                                latest.bucket + self.proj_width + 1)

        if book_col.book:
            # Exact, no margin: the ladder's bars are right-anchored at mx, so
            # the deepest level must land flush against the price axis. Taken
            # from the ladder item, which has already summed levels into the
            # selected price buckets - the raw per-tick max would under-scale
            # the axis and push aggregated bars off the edge.
            self.dom.setXRange(0, self.dom_item.vmax or 1, padding=0)

        if initial or self._follow:
            width = self._view_width(cols, default=60)
            x_hi = latest.bucket + self.proj_width + 3    # room for projection
            self.main.setXRange(x_hi - width, x_hi, padding=0)
        if initial or self._auto_y:
            self._fit_price(cols)

    def _view_width(self, cols, default: int) -> float:
        try:
            r = self.main.getViewBox().viewRange()[0]
            w = r[1] - r[0]
            return w if w > 2 else default
        except Exception:
            return default

    def _fit_price(self, cols) -> None:
        """Follow the traded price path and pad it with ~16 ticks of context
        each side, so resting walls above/below (support/resistance) stay in
        frame without the deep book shrinking the pies."""
        width = int(self._view_width(cols, default=60))
        vis = cols[-width:] if len(cols) > width else cols
        lo = hi = None
        for c in vis:
            if c.bid_ti is not None and c.ask_ti is not None:
                m = (c.bid_ti + c.ask_ti) / 2
                lo = m if lo is None else min(lo, m)
                hi = m if hi is None else max(hi, m)
        if lo is None:                       # fallback: full book range
            for c in vis:
                for ti in (c.book or {}):
                    lo = ti if lo is None else min(lo, ti)
                    hi = ti if hi is None else max(hi, ti)
            if lo is None:
                return
        pad = max(16.0, (hi - lo) * 0.6) * self.tick
        y0, y1 = lo * self.tick - pad, hi * self.tick + pad

        # Hysteresis: re-fitting on every 80ms refresh made the chart micro-jitter
        # as price wobbled. Only move the view when it drifts meaningfully.
        prev = getattr(self, "_y_range", None)
        if prev is not None:
            span = max(1e-9, prev[1] - prev[0])
            if (abs(y0 - prev[0]) / span < 0.04 and
                    abs(y1 - prev[1]) / span < 0.04):
                return
        self._y_range = (y0, y1)
        self.main.setYRange(y0, y1, padding=0)


class TimeAxisSecs(pg.AxisItem):
    """Formats aggregated column-bucket x values as HH:MM:SS."""

    def __init__(self, *a, win=None, **k):
        super().__init__(*a, **k)
        self.win = win

    def tickStrings(self, values, scale, spacing):
        import time
        dt = (self.win.buffer.col_dt * self.win.agg) if self.win else 1.0
        return [time.strftime("%H:%M:%S", time.localtime(v * dt)) for v in values]
