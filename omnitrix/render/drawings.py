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
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QColor, QPen, QBrush, QPainter, QFont

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
    COLORS = ("#787B86", "#F44336", "#4CAF50", "#00E676",
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

    def paint(self, p: QPainter, *args) -> None:
        r = self.boundingRect()
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
            r = self.boundingRect()
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

    def paint(self, p: QPainter, *args) -> None:
        r = self.boundingRect()
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
                   "#00E676")
        self._text(p, tr, self.data_x(0), sl, f"SL {sl:,.2f}  (-{risk:,.2f})",
                   "#FF5252")


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
        self.addScaleHandle([0, 0.5], [1, 0.5])
        self.addScaleHandle([1, 0.5], [0, 0.5])
        self.addScaleHandle([0.5, 1], [0.5, 0])
        self.addScaleHandle([0.5, 0], [0.5, 1])

    def paint(self, p: QPainter, *args) -> None:
        r = self.boundingRect()
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
            for ti, (sell_v, buy_v) in b.cells.items():
                if sell_v:
                    sell[ti] = sell.get(ti, 0) + sell_v
                if buy_v:
                    buy[ti] = buy.get(ti, 0) + buy_v
        tis = set(buy) | set(sell)
        if not tis:
            return

        totals = {ti: buy.get(ti, 0) + sell.get(ti, 0) for ti in tis}
        mx = max(totals.values()) or 1
        poc = max(totals, key=totals.get)
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
            if ti == poc:
                p.setPen(pg.mkPen("#FFC43C", width=2))
                p.drawLine(QPointF(0, ly), QPointF(w, ly))
