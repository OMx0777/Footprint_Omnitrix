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

    bull: str          # up candle
    bear: str          # down candle

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


# Near-black, not charcoal. Every colour below is one step down from what it
# was; the chrome moves furthest and the DATA colours barely move at all,
# because darkening the bars and cells along with the background would just
# lower the whole image and gain nothing. What it buys is contrast: on a
# trading desk the chart is the only thing that should be emitting light.
#
# `text` goes UP rather than down - the same grey that read as comfortable
# against #0B0E14 reads as dim against #05070C, so it is lifted to keep the
# contrast ratio where it was.
DARK = Theme(
    name="dark",
    bg="#05070C",
    panel="#0A0D14",
    grid="#161A21",
    text="#CFD5E0",
    axis="#282C34",
    bull="#26A69A",
    bear="#EF5350",
    bid_bg=QColor(52, 18, 22),      # muted red field
    ask_bg=QColor(14, 45, 38),      # muted green field
    poc_bg=QColor(230, 232, 238),
    poc_text="#05070C",
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
    grid="#E5E5E7",
    text="#1B1F27",
    axis="#BCBCC0",
    bull="#00897B",
    bear="#E53935",
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
