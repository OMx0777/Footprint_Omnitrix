"""Colour themes. Defaults to an ATAS-style dark palette."""

from __future__ import annotations

from dataclasses import dataclass
from PyQt6.QtGui import QColor


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str            # chart background
    panel: str         # toolbar / panels
    grid: str
    text: str
    axis: str

    bull: str          # semantic "up": delta, CVD, imbalance, depth
    bear: str          # semantic "down"

    # Candle styling is SEPARATE from the semantic bull/bear above. Tying them
    # together means restyling the candles also repaints delta bars, the CVD
    # line and the imbalance highlights - which is not what "make the down
    # candles purple" should do.
    candle_up: str
    candle_down: str
    candle_wick: str   # thin, neutral: the wick is a range, not a direction

    bid_bg: QColor     # sell column background
    ask_bg: QColor     # buy column background
    poc_bg: QColor     # point of control cell
    poc_text: str
    cell_text: str

    buy_imb: QColor    # bright buy imbalance
    sell_imb: QColor   # bright sell imbalance
    va_wash: QColor    # value-area translucent overlay
    va_line: str

    vwap: str
    cvd: str
    delta_up: str
    delta_dn: str


DARK = Theme(
    name="dark",
    bg="#0B0E14",
    panel="#12161F",
    grid="#1C2230",
    text="#C7CCD6",
    axis="#2A3140",
    bull="#26A69A",
    bear="#EF5350",
    candle_up="#26A69A",        # green  — closed above the open
    candle_down="#9C5CFF",      # purple — closed below the open
    candle_wick="#8A8F9A",      # grey   — the high/low range
    bid_bg=QColor(60, 22, 26),      # muted red field
    ask_bg=QColor(18, 52, 44),      # muted green field
    poc_bg=QColor(230, 232, 238),
    poc_text="#0B0E14",
    cell_text="#D6DAE2",
    buy_imb=QColor(0, 230, 118),
    sell_imb=QColor(255, 45, 85),
    va_wash=QColor(41, 121, 255, 55),
    va_line="#5C9DFF",
    vwap="#FFB300",
    cvd="#42A5F5",
    delta_up="#26A69A",
    delta_dn="#EF5350",
)


LIGHT = Theme(
    name="light",
    bg="#FFFFFF",
    panel="#F2F3F5",
    grid="#E3E6EB",
    text="#1B1F27",
    axis="#B7BDC7",
    bull="#00897B",
    bear="#E53935",
    candle_up="#00897B",
    candle_down="#7B3FE4",
    candle_wick="#6E7480",
    bid_bg=QColor(252, 228, 232),
    ask_bg=QColor(224, 242, 237),
    poc_bg=QColor(26, 26, 26),
    poc_text="#FFFFFF",
    cell_text="#12161F",
    buy_imb=QColor(0, 200, 100),
    sell_imb=QColor(230, 30, 60),
    va_wash=QColor(41, 121, 255, 30),
    va_line="#2962FF",
    vwap="#F57C00",
    cvd="#1976D2",
    delta_up="#00897B",
    delta_dn="#E53935",
)
