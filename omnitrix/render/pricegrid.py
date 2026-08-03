"""Shared price-row aggregation: how many ticks collapse into one drawn row.

Both the footprint grid and the Bookmap liquidity field face the same problem —
a 1-tick row is right when zoomed in and unreadable when zoomed out, where
hundreds of levels overlap into a stripe — and both solve it by bucketing ticks
into thicker rows. Keeping the ladder here means the two views cannot drift into
different ideas of what "10¢" or "Auto" means.

The ladder is deliberately "nice": a trader reads prices in cents, dimes and
quarters, not in 7s and 13s, so an auto-chosen grid has to land on round money
or the price axis stops being scannable.
"""

from __future__ import annotations

# Ticks per row the auto mode may choose. On a penny tick these are
# 1¢, 2¢, 5¢, 10¢, 25¢, 50¢, $1, $2.50, $5, $10, $25, $50.
AUTO_STEPS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

# Screen pixels per row each view wants before it widens the grid.
# The footprint has to fit a number inside a row, so it needs about a line of
# text; the heat field only has to stay visible as a distinct band, and forcing
# text-sized rows there would throw away most of the depth resolution.
TARGET_PX_LABELLED = 14.0
TARGET_PX_BAND = 3.0


def auto_step_ticks(px_h: float, tick: float,
                    target_px: float = TARGET_PX_LABELLED) -> int:
    """Ticks per row for the current zoom.

    `px_h` is price units per screen pixel (pyqtgraph's `viewPixelSize()[1]`),
    so `target_px * px_h` is the price height a comfortable row wants; dividing
    by the tick turns that into ticks. Rounds UP to the next rung so a row is
    never thinner than asked for.
    """
    if px_h <= 0 or tick <= 0:
        return 1
    want = (target_px * px_h) / tick
    for s in AUTO_STEPS:
        if s >= want:
            return s
    return AUTO_STEPS[-1]


def step_ticks(price_step: float, tick: float, px_h: float,
               target_px: float = TARGET_PX_LABELLED) -> int:
    """Resolve a user selection to ticks per row.

    `price_step` is a PRICE (dollars) so a label means the same money on any
    instrument, not the same tick count. <= 0 selects auto.
    """
    if price_step > 0:
        return max(1, int(round(price_step / tick)))
    return auto_step_ticks(px_h, tick, target_px)
