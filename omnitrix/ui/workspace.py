"""Workspace persistence — remember the desk exactly as it was left.

Stores window geometry, dock layout, the active symbol / timeframe / chart mode
/ theme and the order-flow settings in a small JSON file, and restores them on
the next launch. Corrupt or older files are ignored rather than crashing the
app, so a bad save can never lock the user out.
"""

from __future__ import annotations

import base64
import json
import os
from ..render.footprint import CELL_STYLES

PATH = os.path.join(os.path.expanduser("~"), ".omnitrix_workspace.json")

# Bumped to 2: version-1 files were written before the toolbars and docks had
# objectNames, so the Qt state blob inside them cannot be matched back to the
# widgets and restoring it corrupts the layout. Old files are ignored outright.
WS_VERSION = 2


def save(win, path: str = PATH) -> bool:
    try:
        fp = win.fp
        data = {
            "version": WS_VERSION,
            "geometry": base64.b64encode(bytes(win.saveGeometry())).decode(),
            "state": base64.b64encode(bytes(win.saveState())).decode(),
            "symbol": win.active_symbol,
            "timeframe": win.tf_combo.currentText(),
            "mode": win.mode_combo.currentText(),
            "price_step": win.step_combo.currentText(),
            "theme": win.theme_combo.currentText(),
            "imbalance": win.chk_imb.isChecked(),
            "imbalance_factor": fp.imbalance_factor,
            "min_imbalance_vol": fp.min_imbalance_vol,
            "stacked_min": fp.stacked_min,
            "va_pct": fp.va_pct,
            "value_area": win.chk_va.isChecked(),
            "vwap": win.chk_vwap.isChecked(),
            "show_candles": fp.show_candles,
            "cell_style": getattr(fp, "cell_style", "histogram"),
            # Alerts are levels somebody chose to watch. Losing them on a
            # restart is losing work, and the app restarts more than it should.
            "alerts": win.alerts.to_list() if hasattr(win, "alerts") else [],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)               # atomic: never leave a half file
        return True
    except Exception:
        return False


def restore(win, path: str = PATH) -> bool:
    """Apply a saved workspace. Returns False (silently) if unavailable."""
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") != WS_VERSION:
            return False

        if d.get("geometry"):
            win.restoreGeometry(base64.b64decode(d["geometry"]))
        if d.get("state"):
            win.restoreState(base64.b64decode(d["state"]))

        for combo, key in ((win.tf_combo, "timeframe"),
                           (win.mode_combo, "mode"),
                           (win.step_combo, "price_step"),
                           (win.theme_combo, "theme")):
            v = d.get(key)
            if v and combo.findText(v) >= 0:
                combo.setCurrentText(v)

        if "alerts" in d and hasattr(win, "alerts"):
            win.alerts.load(d.get("alerts"))

        for chk, key in ((win.chk_imb, "imbalance"),
                         (win.chk_va, "value_area"),
                         (win.chk_vwap, "vwap")):
            if key in d:
                chk.setChecked(bool(d[key]))

        win.fp.configure(
            imbalance_factor=float(d.get("imbalance_factor",
                                         win.fp.imbalance_factor)),
            min_imbalance_vol=int(d.get("min_imbalance_vol",
                                        win.fp.min_imbalance_vol)),
            stacked_min=int(d.get("stacked_min", win.fp.stacked_min)),
            va_pct=float(d.get("va_pct", win.fp.va_pct)),
            show_candles=bool(d.get("show_candles", win.fp.show_candles)),
            # Validated against the known styles rather than trusted: a
            # hand-edited or older workspace must not put the renderer into a
            # branch that does not exist.
            cell_style=(d.get("cell_style")
                        if d.get("cell_style") in CELL_STYLES
                        else win.fp.cell_style),
        )
        win._pending_symbol = d.get("symbol") or ""
        return True
    except Exception:
        return False
