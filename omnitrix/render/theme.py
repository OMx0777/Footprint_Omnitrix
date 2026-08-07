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
    buy_bar: QColor    # footprint histogram, buy wing
    sell_bar: QColor   # footprint histogram, sell wing
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
    # DESATURATED, NOT PRIMARY. The old pair were a bright teal and a bright
    # coral - the palette a chart gets when the colours are picked one at a
    # time rather than as a set. These are the same hues pulled toward the
    # background and given a common value, so a screen full of them reads as
    # one image instead of a set of stickers, and the eye is free for the
    # things that are DELIBERATELY loud: an imbalance, the POC, an alert.
    bull="#2E9E7E",
    bear="#D4564F",
    bid_bg=QColor(52, 18, 22),      # muted red field
    ask_bg=QColor(14, 45, 38),      # muted green field
    # The histogram wings. Darker than the candle so the candle stays the
    # brightest thing in its own column, and matched in value to each other so
    # neither side looks heavier than it is.
    buy_bar=QColor(38, 122, 100),
    sell_bar=QColor(150, 62, 58),
    poc_bg=QColor(230, 232, 238),
    poc_text="#05070C",
    cell_text="#D6DAE2",
    # These two STAY loud. An imbalance is the one thing on the chart that is
    # meant to interrupt you, and muting it with everything else would remove
    # the only reason it is drawn differently at all.
    buy_imb=QColor(64, 224, 148),
    sell_imb=QColor(255, 86, 96),
    va_wash=QColor(70, 110, 180, 42),
    va_line="#6E93C8",
    vwap="#E0A03C",
    cvd="#5B93D6",
    delta_up="#2E9E7E",
    delta_dn="#D4564F",
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
    buy_bar=QColor(120, 200, 170),
    sell_bar=QColor(230, 150, 150),
    va_wash=QColor(41, 121, 255, 30),
    va_line="#2962FF",
    vwap="#F57C00",
    cvd="#1976D2",
    delta_up="#00897B",
    delta_dn="#E53935",
)
