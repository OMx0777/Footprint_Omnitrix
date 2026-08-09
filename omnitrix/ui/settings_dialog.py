"""Customization dialog — every knob that shapes the chart lives here."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QDoubleSpinBox, QSpinBox, QCheckBox, QPushButton,
    QComboBox, QColorDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QColor

from ..render.footprint import (CELL_STYLES, CELL_STYLE_LABELS,
                                CELL_HISTOGRAM)


class _ColorButton(QPushButton):
    """A button that opens a colour picker and remembers the chosen QColor."""

    def __init__(self, color: QColor):
        super().__init__()
        self._color = QColor(color)
        self.setFixedWidth(80)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self) -> None:
        c = self._color
        self.setStyleSheet(f"background:{c.name()}; border:1px solid #555; border-radius:3px;")
        self.setText(c.name())

    def _pick(self) -> None:
        c = QColorDialog.getColor(self._color, self, "Pick colour")
        if c.isValid():
            self._color = c
            self._refresh()

    def color(self) -> QColor:
        return self._color


class SettingsDialog(QDialog):
    """Reads current state from the window, lets the user edit, and applies."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Chart Settings")
        self.setMinimumWidth(360)
        fp = win.fp
        t = win.theme

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        self.tick = QDoubleSpinBox()
        self.tick.setDecimals(4); self.tick.setRange(0.0001, 100.0)
        self.tick.setSingleStep(0.01)
        self.tick.setValue(win.instruments.tick(win.active_symbol or "QQQ"))
        form.addRow("Tick size", self.tick)

        self.imb = QDoubleSpinBox()
        self.imb.setRange(1.1, 20.0); self.imb.setSingleStep(0.5)
        self.imb.setValue(fp.imbalance_factor)
        form.addRow("Imbalance factor ×", self.imb)

        self.minvol = QSpinBox()
        self.minvol.setRange(0, 100000); self.minvol.setValue(fp.min_imbalance_vol)
        form.addRow("Min imbalance vol", self.minvol)

        self.stacked = QSpinBox()
        self.stacked.setRange(2, 20); self.stacked.setValue(fp.stacked_min)
        form.addRow("Stacked levels ≥", self.stacked)

        self.vapct = QSpinBox()
        self.vapct.setRange(30, 95); self.vapct.setValue(int(fp.va_pct * 100))
        self.vapct.setSuffix(" %")
        form.addRow("Value area", self.vapct)

        self.hm_alpha = QSpinBox()
        self.hm_alpha.setRange(20, 255); self.hm_alpha.setValue(win.heatmap.alpha)
        form.addRow("Heatmap intensity", self.hm_alpha)

        self.hm_gamma = QDoubleSpinBox()
        # Range must cover the >1 contrast values the heatmaps now default to,
        # or the spinbox silently clamps the current setting on open.
        self.hm_gamma.setRange(0.2, 4.0); self.hm_gamma.setSingleStep(0.05)
        self.hm_gamma.setValue(win.heatmap.gamma)
        form.addRow("Heatmap gamma", self.hm_gamma)

        self.chk_candles = QCheckBox(); self.chk_candles.setChecked(fp.show_candles)
        form.addRow("Show candles", self.chk_candles)

        # Footprint cell layout. Both are real choices, not a legacy toggle:
        # the histogram shows the shape of the auction without reading a digit,
        # the classic blocks are denser and are what a long-time reader's eye
        # already parses. Histogram is the default.
        self.cell_style = QComboBox()
        for key in CELL_STYLES:
            self.cell_style.addItem(CELL_STYLE_LABELS[key], key)
        i = self.cell_style.findData(getattr(fp, "cell_style", CELL_HISTOGRAM))
        self.cell_style.setCurrentIndex(max(0, i))
        form.addRow("Footprint cells", self.cell_style)

        # colour pickers
        self.c_bull = _ColorButton(QColor(t.bull))
        self.c_bear = _ColorButton(QColor(t.bear))
        self.c_buy = _ColorButton(t.buy_imb)
        self.c_sell = _ColorButton(t.sell_imb)
        for label, btn in (("Up / buy", self.c_bull), ("Down / sell", self.c_bear),
                           ("Buy imbalance", self.c_buy), ("Sell imbalance", self.c_sell)):
            form.addRow(label, btn)

        btns = QHBoxLayout()
        ok = QPushButton("Apply"); cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        btns.addStretch(1); btns.addWidget(cancel); btns.addWidget(ok)
        root.addLayout(btns)

    def values(self) -> dict:
        return {
            "tick": self.tick.value(),
            "imbalance_factor": self.imb.value(),
            "min_imbalance_vol": self.minvol.value(),
            "stacked_min": self.stacked.value(),
            "va_pct": self.vapct.value() / 100.0,
            "hm_alpha": self.hm_alpha.value(),
            "hm_gamma": self.hm_gamma.value(),
            "show_candles": self.chk_candles.isChecked(),
            "cell_style": self.cell_style.currentData(),
            "bull": self.c_bull.color().name(),
            "bear": self.c_bear.color().name(),
            "buy_imb": self.c_buy.color(),
            "sell_imb": self.c_sell.color(),
        }
