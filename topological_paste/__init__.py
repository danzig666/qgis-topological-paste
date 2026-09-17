# -*- coding: utf-8 -*-
"""Topological Paste - QGIS plugin entry point."""


def classFactory(iface):  # noqa: N802 (QGIS naming convention)
    from .plugin import TopologicalPastePlugin

    return TopologicalPastePlugin(iface)
