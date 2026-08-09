"""Interactive chart drawing tools: Fibonacci retracement, long/short position
planner, fixed-range volume profile.

**Coordinate contract — this is what was previously wrong.** A `pg.ROI` paints
in its own *local* coordinate system: `boundingRect()` is `(0, 0, w, h)` and the
item's data position is `self.pos()`. Painting a price (546.10) straight into
that system puts the mark hundreds of units outside the item, where Qt clips it
away — the old `FixedVolumeProfile` drew nothing visible and `PositionDrawer`
labelled its boxes with the ROI's pixel height instead of a price.

Every tool here therefore:
  * draws geometry in local space only, always inside `boundingRect()`;
  * converts local -> data (`self.pos() + local`) *only* to build label text;
  * renders text with the transform reset, so labels stay upright and unscaled.

pyqtgraph ViewBoxes run y-up, so local y = 0 is the ROI's **bottom** edge and
local y = h is its top.
"""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import (QColor, QPen, QBrush, QPainter, QFont, QPainterPath)
from ..paintguard import safe_paint

_LABEL_FONT = QFont("Consolas", 8, QFont.Weight.Bold)


def _norm(p1, p2) -> tuple[list[float], list[float]]:
    """Two arbitrary corners -> (origin, positive size). A pg.ROI with negative
    width or height inverts its handles and reports a mirrored boundingRect, so
    every tool is built from a normalised rectangle and remembers direction
    separately."""
    x0, x1 = sorted((float(p1[0]), float(p2[0])))
    y0, y1 = sorted((float(p1[1]), float(p2[1])))
    return [x0, y0], [max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)]


class _DrawTool(pg.ROI):
    """Shared plumbing: repaint on move/resize, upright text, select + delete.

    Every tool takes TWO DATA POINTS, never a point plus a size. Mixing the two
    was the bug behind "the drawings are broken": `_norm` sorts its two
    arguments as absolute coordinates, so a caller passing a delta placed the
    box somewhere else entirely — `_norm([100, 550], [20, 5])` yields a box at
    x=20..100 rather than 100..120. Fib passed points and behaved; Long, Short
    and Volume Profile passed deltas and landed in the wrong place.
    """

    SEL_PEN = pg.mkPen("#FFC43C", width=2)

    # Screen-pixel margin the labels are allowed to spill outside the ROI box.
    # `_text` resets the transform and draws in device space, so a label sits
    # OUTSIDE `pg.ROI.boundingRect()`, which is exactly (0, 0, w, h).
    #
    # Qt only repaints the area an item's boundingRect covered, so anything
    # painted beyond it is never invalidated: drag a drawing and the old labels
    # stay burnt into the canvas. That is the "garbage colours left behind when
    # I move them" glitch. Widening the reported rect to cover everything we
    # actually paint is the fix - the rect must be a superset of the painted
    # area or artifacts are guaranteed.
    PAD_L_PX, PAD_R_PX = 12.0, 210.0      # labels extend to the RIGHT of x
    PAD_T_PX, PAD_B_PX = 16.0, 16.0

    def __init__(self, pos, size, **kw):
        kw.setdefault("pen", pg.mkPen("#5C9DFF", width=1))
        # ROI ships a right-click "Remove" entry and the matching signal; the
        # window wires it up. There was previously no way to delete one drawing
        # short of clearing them all.
        kw.setdefault("removable", True)
        super().__init__(pos, size, **kw)
        # Not overriding acceptedMouseButtons: ROI derives it from `translatable`
        # and forcing it here breaks dragging the shape.
        self._base_pen = self.pen
        self.selected = False
        self.sigRegionChanged.connect(self._changed)

    def set_selected(self, on: bool) -> None:
        """Amber outline + visible handles while selected, so it is obvious
        which drawing Delete is about to remove."""
        self.selected = on
        self.setPen(self.SEL_PEN if on else self._base_pen)
        for h in self.handles:
            h["item"].setVisible(on)
        self.update()

    def _changed(self, *_) -> None:
        self.prepareGeometryChange()
        self.update()

    # ---- geometry --------------------------------------------------------
    def shape_rect(self) -> QRectF:
        """The ROI box itself, in local coords - what the tools draw against."""
        return super().boundingRect()

    def boundingRect(self) -> QRectF:
        """The box PLUS room for the labels (see PAD_*_PX).

        The pad is specified in pixels and converted through the view scale, so
        it covers the text at any zoom rather than being a data-space guess that
        is too small when zoomed out.
        """
        r = super().boundingRect()
        vb = self.getViewBox()
        if vb is None:
            return r
        try:
            px_w, px_h = vb.viewPixelSize()
        except Exception:
            return r
        if not (px_w > 0 and px_h > 0):
            return r
        return r.adjusted(-self.PAD_L_PX * px_w, -self.PAD_T_PX * px_h,
                          self.PAD_R_PX * px_w, self.PAD_B_PX * px_h)

    def viewTransformChanged(self) -> None:
        # boundingRect() is measured in pixels, so a zoom changes it even though
        # the ROI has not moved. Without this Qt keeps the stale rect from the
        # previous scale and clips - or fails to clear - the labels.
        self.prepareGeometryChange()
        super().viewTransformChanged()

    def data_y(self, local_y: float) -> float:
        """Local y -> price."""
        return self.pos().y() + local_y

    def data_x(self, local_x: float) -> float:
        return self.pos().x() + local_x

    @staticmethod
    def _text(p: QPainter, tr, x: float, y: float, s: str, color,
              dx: int = 4, dy: int = -3) -> None:
        """Draw `s` at data-space (x, y) without inheriting the view scale."""
        pt = tr.map(QPointF(x, y))
        p.save()
        p.resetTransform()
        p.setFont(_LABEL_FONT)
        p.setPen(pg.mkPen(color))
        p.drawText(QPointF(pt.x() + dx, pt.y() + dy), s)
        p.restore()


class FibRetracement(_DrawTool):
    """Fibonacci retracement between two points.

    Level 0 sits on the first point clicked and level 1 on the second, so
    dragging downward flips the ladder exactly as it does in TradingView.
    """

    LEVELS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    COLORS = ("#787B86", "#F44336", "#4CAF50", "#40E094",
              "#2196F3", "#9C27B0", "#787B86")

    def __init__(self, p1, p2, **kw):
        pos, size = _norm(p1, p2)
        super().__init__(pos, size, **kw)
        # p1 above p2 means the user drew top-down: level 0 belongs at the top.
        self._flip = float(p1[1]) > float(p2[1])
        self.addScaleHandle([0, 0], [1, 1])
        self.addScaleHandle([1, 1], [0, 0])

    def _local_y(self, level: float, h: float) -> float:
        return h * (1.0 - level) if self._flip else h * level

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tr = p.transform()

        prev_y = None
        for level, col in zip(self.LEVELS, self.COLORS):
            ly = self._local_y(level, h)
            colour = QColor(col)
            # Shade the band between consecutive levels, TradingView-style.
            if prev_y is not None:
                band = QColor(colour)
                band.setAlpha(26)
                p.fillRect(QRectF(0, min(prev_y, ly), w, abs(ly - prev_y)), band)
            prev_y = ly

            p.setPen(pg.mkPen(colour, width=1, style=Qt.PenStyle.DashLine))
            p.drawLine(QPointF(0, ly), QPointF(w, ly))
            self._text(p, tr, self.data_x(w), self.data_y(ly),
                       f"{level:.3f}  {self.data_y(ly):,.2f}", colour)


class PositionDrawer(_DrawTool):
    """Long/short planner: entry line with take-profit and stop-loss zones.

    The entry sits on a free handle the user can slide; risk/reward and all
    three prices are reported in *data* units, which is what makes the tool
    usable — the previous version printed the ROI's height as the TP price.
    """

    def __init__(self, p1, p2, is_long: bool = True, **kw):
        # p1/p2 are two DATA POINTS (see _DrawTool); passing a delta here was
        # the placement bug.
        pos, size = _norm(p1, p2)
        super().__init__(pos, size, **kw)
        self.is_long = is_long
        self.entry_frac = 0.5
        self._syncing = False
        self.addScaleHandle([0.5, 1], [0.5, 0])
        self.addScaleHandle([0.5, 0], [0.5, 1])
        self.entry_handle = self.addFreeHandle([0.5, 0.5])

    def _changed(self, *_) -> None:
        # sigRegionChanged is connected by the base constructor, so this can fire
        # while addScaleHandle() runs — before entry_handle exists.
        handle = getattr(self, "entry_handle", None)
        if handle is not None and not self._syncing:
            r = self.shape_rect()
            h = max(r.height(), 1e-9)
            hp = handle.pos()
            self.entry_frac = min(1.0, max(0.0, hp.y() / h))
            cx = r.width() / 2
            if abs(hp.x() - cx) > 1e-9 or not (0.0 <= hp.y() <= h):
                # setPos re-emits sigRegionChanged; the flag stops the recursion.
                self._syncing = True
                try:
                    handle.setPos(cx, self.entry_frac * h)
                finally:
                    self._syncing = False
        super()._changed()

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        tr = p.transform()

        entry_y = h * self.entry_frac
        # For a long, profit is above the entry and risk below; inverted short.
        if self.is_long:
            tp_rect = QRectF(0, entry_y, w, h - entry_y)
            sl_rect = QRectF(0, 0, w, entry_y)
            tp_y, sl_y = h, 0.0
        else:
            tp_rect = QRectF(0, 0, w, entry_y)
            sl_rect = QRectF(0, entry_y, w, h - entry_y)
            tp_y, sl_y = 0.0, h

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 230, 118, 55)))
        p.drawRect(tp_rect)
        p.setBrush(QBrush(QColor(255, 23, 68, 55)))
        p.drawRect(sl_rect)

        p.setPen(pg.mkPen("#E8ECF2", width=2))
        p.drawLine(QPointF(0, entry_y), QPointF(w, entry_y))

        entry = self.data_y(entry_y)
        tp = self.data_y(tp_y)
        sl = self.data_y(sl_y)
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = (reward / risk) if risk > 1e-12 else 0.0

        side = "LONG" if self.is_long else "SHORT"
        self._text(p, tr, self.data_x(0), entry,
                   f"{side}  entry {entry:,.2f}   R/R {rr:.2f}", "#E8ECF2")
        self._text(p, tr, self.data_x(0), tp, f"TP {tp:,.2f}  (+{reward:,.2f})",
                   "#40E094")
        self._text(p, tr, self.data_x(0), sl, f"SL {sl:,.2f}  (-{risk:,.2f})",
                   "#FF5252")


POC_COL = "#FFC43C"
VA_COL = "#5C9DFF"


def _value_area(totals: dict, poc: int, pct: float):
    """(VAH, VAL) tick indices holding `pct` of the volume around the POC.

    Grown one level at a time toward the heavier adjacent side - the standard
    construction, and the same one Bar.value_area uses, so a drawn profile and
    a bar's own value area cannot disagree about the same prices.
    """
    if not totals or poc is None:
        return None, None
    idxs = sorted(totals)
    target = sum(totals.values()) * pct
    pos = idxs.index(poc)
    lo = hi = pos
    acc = totals[poc]
    n = len(idxs)
    while acc < target and (lo > 0 or hi < n - 1):
        up = totals[idxs[hi + 1]] if hi < n - 1 else -1
        dn = totals[idxs[lo - 1]] if lo > 0 else -1
        if up < 0 and dn < 0:
            break
        if up >= dn:
            hi += 1
            acc += totals[idxs[hi]]
        else:
            lo -= 1
            acc += totals[idxs[lo]]
    return idxs[hi], idxs[lo]


class PriceLevel(pg.InfiniteLine):
    """A horizontal price level, dragged by its line and labelled with its price.

    Not a `_DrawTool`: a level has no box, and forcing it into an ROI would
    give it a width it does not have and handles that mean nothing. It instead
    implements the small protocol the window needs from a drawing -
    `set_selected`, `sigClicked`, `sigRemoveRequested` - so selection, Delete
    and right-click Remove all work on it exactly as on the box tools.

    InfiniteLine already renders a value label and spans the view at any pan or
    zoom, which is the whole behaviour wanted here.
    """

    sigRemoveRequested = QtCore.pyqtSignal(object)

    BASE = "#5C9DFF"
    SEL = "#FFC43C"

    def __init__(self, price: float, colour: str = BASE, **kw):
        kw.setdefault("movable", True)
        super().__init__(
            pos=price, angle=0,
            pen=pg.mkPen(colour, width=1, style=Qt.PenStyle.DashLine),
            hoverPen=pg.mkPen(colour, width=2),
            label="{value:,.2f}",
            labelOpts={"position": 0.02, "color": colour,
                       "fill": (10, 13, 20, 215), "movable": False},
            **kw)
        self.colour = colour
        self.selected = False

    def set_selected(self, on: bool) -> None:
        self.selected = on
        c = self.SEL if on else self.colour
        self.setPen(pg.mkPen(c, width=2 if on else 1,
                             style=Qt.PenStyle.DashLine))
        if self.label is not None:
            self.label.setColor(pg.mkColor(c))
        self.update()

    def mouseClickEvent(self, ev) -> None:
        # Right-click removes, matching pg.ROI's built-in Remove entry so the
        # gesture is the same on every drawing.
        if ev.button() == Qt.MouseButton.RightButton:
            ev.accept()
            self.sigRemoveRequested.emit(self)
            return
        super().mouseClickEvent(ev)


class MeasureTool(_DrawTool):
    """TradingView-style measure: price move, %, bars, duration and volume.

    Reports the SIGNED move from the first corner to the second, so dragging
    down reads negative and colours red - the direction is the point of the
    tool. `_norm` throws that away (it sorts the corners), so the direction is
    captured at construction, exactly as FibRetracement does for its ladder.
    """

    UP = QColor(0, 230, 118)
    DOWN = QColor(255, 82, 82)

    PAD_R_PX = 250.0

    def __init__(self, p1, p2, get_bars_cb, **kw):
        pos, size = _norm(p1, p2)
        super().__init__(pos, size, **kw)
        self.get_bars_cb = get_bars_cb
        self._down = float(p2[1]) < float(p1[1])
        self.addScaleHandle([0, 0], [1, 1])
        self.addScaleHandle([1, 1], [0, 0])

    def stats(self) -> dict:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        y0 = self.data_y(h if self._down else 0.0)     # start price
        y1 = self.data_y(0.0 if self._down else h)     # end price
        move = y1 - y0
        pct = (move / y0 * 100.0) if y0 else 0.0
        bars = self.get_bars_cb(self.data_x(0), self.data_x(w)) or []
        vol = sum(b.volume for b in bars)
        secs = 0
        if len(bars) >= 2:
            secs = int(bars[-1].start_ts - bars[0].start_ts
                       + getattr(bars[-1], "tf_s", 0))
        elif len(bars) == 1:
            secs = int(getattr(bars[0], "tf_s", 0))
        return {"move": move, "pct": pct, "bars": len(bars),
                "secs": secs, "volume": vol}

    @staticmethod
    def _dur(secs: int) -> str:
        if secs <= 0:
            return "0s"
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        parts = [f"{d}d" if d else "", f"{h}h" if h else "",
                 f"{m}m" if m else "", f"{s}s" if s else ""]
        return " ".join(p for p in parts if p) or "0s"

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        st = self.stats()
        col = self.DOWN if st["move"] < 0 else self.UP

        band = QColor(col)
        band.setAlpha(38)
        p.fillRect(r, band)
        p.setPen(pg.mkPen(col, width=1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

        # Direction arrow down the middle, from the start price to the end.
        cx = w / 2
        y_from = h if self._down else 0.0
        y_to = 0.0 if self._down else h
        p.setPen(pg.mkPen(col, width=2))
        p.drawLine(QPointF(cx, y_from), QPointF(cx, y_to))
        head = (y_from - y_to) * 0.12
        p.drawLine(QPointF(cx, y_to), QPointF(cx - w * 0.06, y_to + head))
        p.drawLine(QPointF(cx, y_to), QPointF(cx + w * 0.06, y_to + head))

        tr = p.transform()
        sign = "+" if st["move"] >= 0 else ""
        lines = (f"{sign}{st['move']:,.2f}  ({sign}{st['pct']:.2f}%)",
                 f"{st['bars']:,} bars  {self._dur(st['secs'])}",
                 f"vol {st['volume']:,}")
        # Stacked in SCREEN space off the arrow head, so the block reads the
        # same at any zoom instead of collapsing as the box shrinks.
        pt = tr.map(QPointF(cx, y_to))
        p.save()
        p.resetTransform()
        p.setFont(_LABEL_FONT)
        p.setPen(pg.mkPen(col))
        fm = p.fontMetrics()
        step = fm.height()
        top = pt.y() - (step * len(lines) + 8) if not self._down else pt.y() + 8
        for i, s in enumerate(lines):
            p.drawText(QPointF(pt.x() + 8, top + i * step), s)
        p.restore()


class PenDrawing(_DrawTool):
    """Freehand stroke.

    Stored as points in LOCAL coordinates against a normalised bounding box, so
    dragging the ROI moves the whole stroke and the coordinate contract at the
    top of this file still holds. No scale handles: a freehand mark is an
    annotation on a price and a time, and rescaling it would move every point
    off the thing it was drawn on.
    """

    # No labels, so the box needs only enough slack for the pen width.
    PAD_L_PX = PAD_R_PX = PAD_T_PX = PAD_B_PX = 4.0

    def __init__(self, points, colour: str = "#FFC43C", width: int = 2, **kw):
        pts = [(float(x), float(y)) for x, y in points]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pos = [min(xs), min(ys)]
        size = [max(max(xs) - pos[0], 1e-9), max(max(ys) - pos[1], 1e-9)]
        kw.setdefault("pen", pg.mkPen(colour, width=width))
        super().__init__(pos, size, **kw)
        self.colour = colour
        self.width = width
        self.points = [(x - pos[0], y - pos[1]) for x, y in pts]

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        if len(self.points) < 2:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = pg.mkPen(self.SEL_PEN.color() if self.selected else self.colour,
                       width=self.width)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(self.points[0][0], self.points[0][1])
        for x, y in self.points[1:]:
            path.lineTo(x, y)
        p.drawPath(path)


class CprDrawing(_DrawTool):
    """Central Pivot Range over a user-selected bar range.

    The CPR overlay in `indicators.py` derives its levels from the PREVIOUS
    calendar session, which is the textbook read. This tool answers the other
    question a scalper asks - "what is the pivot of *this* leg?" - by computing
    the same formula over whatever range is boxed, and projecting the levels
    across the box so they can be dragged onto the next move.

        P  = (H + L + C) / 3     of the selected bars
        BC = (H + L) / 2
        TC = 2P - BC             (swapped if inverted)
    """

    P_COL = "#FF4081"
    C_COL = "#00BCD4"

    def __init__(self, p1, p2, get_bars_cb, **kw):
        pos, size = _norm(p1, p2)
        super().__init__(pos, size, **kw)
        self.get_bars_cb = get_bars_cb
        self.addScaleHandle([0, 0.5], [1, 0.5])
        self.addScaleHandle([1, 0.5], [0, 0.5])
        self.addScaleHandle([0.5, 1], [0.5, 0])
        self.addScaleHandle([0.5, 0], [0.5, 1])

    def levels(self) -> tuple[float, float, float] | None:
        """(pivot, tc, bc) in PRICE, or None when the box holds no bars."""
        bars = self.get_bars_cb(self.data_x(0),
                                self.data_x(self.shape_rect().width())) or []
        if not bars:
            return None
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        close = bars[-1].close
        pv = (hi + lo + close) / 3.0
        bc = (hi + lo) / 2.0
        tc = 2.0 * pv - bc
        if tc < bc:
            tc, bc = bc, tc
        return pv, tc, bc

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        p.setPen(pg.mkPen("#5C9DFF", width=1, style=Qt.PenStyle.DashLine))
        p.setBrush(QBrush(QColor(41, 98, 255, 14)))
        p.drawRect(r)

        lv = self.levels()
        if lv is None:
            return
        pv, tc, bc = lv
        tr = p.transform()
        y_base = self.pos().y()
        # price -> local y; the levels are real prices and need not sit inside
        # the box the user happened to draw.
        ly_p, ly_tc, ly_bc = (pv - y_base), (tc - y_base), (bc - y_base)

        band = QColor(self.C_COL)
        band.setAlpha(38)
        p.fillRect(QRectF(0, min(ly_bc, ly_tc), w, abs(ly_tc - ly_bc)), band)

        p.setPen(pg.mkPen(self.P_COL, width=2))
        p.drawLine(QPointF(0, ly_p), QPointF(w, ly_p))
        p.setPen(pg.mkPen(self.C_COL, width=1, style=Qt.PenStyle.DashLine))
        for ly in (ly_tc, ly_bc):
            p.drawLine(QPointF(0, ly), QPointF(w, ly))

        for tag, ly, price, col in (("P", ly_p, pv, self.P_COL),
                                    ("TC", ly_tc, tc, self.C_COL),
                                    ("BC", ly_bc, bc, self.C_COL)):
            self._text(p, tr, self.data_x(w), self.data_y(ly),
                       f"{tag} {price:,.2f}", col)


class FixedVolumeProfile(_DrawTool):
    """Volume-at-price over a user-selected bar range.

    Bars grow leftward from the right edge of the box, in local coordinates, so
    the profile is actually visible inside the ROI — the previous version placed
    every bar at its absolute price and drew off-screen.
    """

    def __init__(self, p1, p2, get_bars_cb, tick_size: float, **kw):
        pos, size = _norm(p1, p2)
        super().__init__(pos, size, **kw)
        self.get_bars_cb = get_bars_cb
        self.tick_size = max(float(tick_size), 1e-9)
        self.va_pct = 0.70          # share of the range's volume in the band
        self.addScaleHandle([0, 0.5], [1, 0.5])
        self.addScaleHandle([1, 0.5], [0, 0.5])
        self.addScaleHandle([0.5, 1], [0.5, 0])
        self.addScaleHandle([0.5, 0], [0.5, 1])

    @safe_paint
    def paint(self, p: QPainter, *args) -> None:
        r = self.shape_rect()
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        p.setPen(pg.mkPen("#5C9DFF", width=1, style=Qt.PenStyle.DashLine))
        p.setBrush(QBrush(QColor(41, 98, 255, 18)))
        p.drawRect(r)

        bars = self.get_bars_cb(self.data_x(0), self.data_x(w)) or []
        if not bars:
            return

        tick = self.tick_size
        buy: dict[int, int] = {}
        sell: dict[int, int] = {}
        for b in bars:
            bti, bsell, bbuy = b.arrays()
            for ti, sell_v, buy_v in zip(bti.tolist(), bsell.tolist(),
                                         bbuy.tolist()):
                if sell_v:
                    sell[ti] = sell.get(ti, 0) + sell_v
                if buy_v:
                    buy[ti] = buy.get(ti, 0) + buy_v
        tis = set(buy) | set(sell)
        if not tis:
            return

        totals = {ti: buy.get(ti, 0) + sell.get(ti, 0) for ti in tis}
        mx = max(totals.values()) or 1
        # Ties go to the lowest price, matching Bar._compute - a POC that moves
        # with dict insertion order is a POC that changes on replay.
        poc = min(k for k, v in totals.items() if v == mx)
        vah, val = _value_area(totals, poc, self.va_pct)
        y_base = self.pos().y()
        # One row per price level, always tick-tall. Scaling rows to fill the box
        # made them fat and overlapping whenever the ROI spanned more price than
        # actually traded.
        row_h = tick

        for ti, tot in totals.items():
            ly = (ti * tick) - y_base          # price -> local y
            if ly < -row_h or ly > h + row_h:
                continue                        # level outside the box
            full = (tot / mx) * (w * 0.86)
            s_w = full * (sell.get(ti, 0) / tot) if tot else 0.0
            b_w = full - s_w
            y = ly - row_h / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(239, 83, 80, 190)))
            p.fillRect(QRectF(w - full, y, s_w, row_h), QColor(239, 83, 80, 190))
            p.fillRect(QRectF(w - full + s_w, y, b_w, row_h),
                       QColor(38, 166, 154, 190))
        # Value area: the band holding `va_pct` of the range's volume, grown
        # from the POC toward whichever neighbour is heavier. Drawn AFTER the
        # bars so the levels are readable over them.
        tr = p.transform()
        vy = {}
        for tag, lvl, col in (("VAH", vah, VA_COL), ("VAL", val, VA_COL),
                              ("POC", poc, POC_COL)):
            if lvl is None:
                continue
            vy[tag] = (lvl * tick) - y_base

        if "VAH" in vy and "VAL" in vy:
            band = QColor(VA_COL)
            band.setAlpha(28)
            lo, hi = sorted((vy["VAL"], vy["VAH"]))
            p.setPen(Qt.PenStyle.NoPen)
            p.fillRect(QRectF(0, lo, w, hi - lo), band)

        for tag, ly2 in vy.items():
            is_poc = tag == "POC"
            colour = POC_COL if is_poc else VA_COL
            p.setPen(pg.mkPen(colour, width=2 if is_poc else 1,
                              style=Qt.PenStyle.SolidLine if is_poc
                              else Qt.PenStyle.DashLine))
            p.drawLine(QPointF(0, ly2), QPointF(w, ly2))
            self._text(p, tr, self.data_x(w), self.data_y(ly2),
                       f"{tag} {(ly2 + y_base):,.2f}", colour)
