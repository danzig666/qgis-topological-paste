# -*- coding: utf-8 -*-
"""Plugin settings (stored in the QGIS profile under ``TopologicalPaste/``)."""

from qgis.core import QgsSettings

_PREFIX = "TopologicalPaste/"

DEFAULTS = {
    # Coordinates are preserved by default; the grid is an explicit, advanced opt-in.
    "precision_grid_enabled": False,
    "precision_grid_size": 0.001,
    "highlight_preview": True,
    # Optional shortcut for the ordinary case; destructive pastes always show the preview.
    "skip_preview_when_only_adding": False,
}


def _to_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_bool(key):
    return _to_bool(QgsSettings().value(_PREFIX + key, DEFAULTS[key]), DEFAULTS[key])


def get_float(key):
    try:
        return float(QgsSettings().value(_PREFIX + key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def set_value(key, value):
    QgsSettings().setValue(_PREFIX + key, value)


def grid_size():
    """Effective precision grid: 0.0 unless the user explicitly enabled it."""
    if not get_bool("precision_grid_enabled"):
        return 0.0
    return max(0.0, get_float("precision_grid_size"))
