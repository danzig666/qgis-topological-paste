# Topological Paste – QGIS plugin

Paste clipboard features (geometry **and** attributes) into the active layer with overwrite semantics:
the pasted coverage wins, existing features underneath are trimmed instead of duplicated, identical
geometries are reused, shared boundaries are noded, and the whole paste is **one** Undo step – after a preview.

* Full documentation: [topological_paste/README.md](topological_paste/README.md)
* Manual verification checklist: [topological_paste/MANUAL_CHECKLIST.md](topological_paste/MANUAL_CHECKLIST.md)
* Original specification: [design.txt](design.txt)

## Install

Download `topological_paste-<version>.zip` from the [Releases](../../releases) page and use
*Plugins ▸ Manage and Install Plugins… ▸ Install from ZIP* in QGIS (3.28+; tested on 3.44 Qt5 and Qt6 builds).

## Develop

```
python build_zip.py          build dist/topological_paste-<version>.zip
run_tests.cmd qt5|qt6        headless test-suite (python-qgis)
run_gui_tests.cmd qt5|qt6    GUI integration tests in a real QGIS with a throw-away profile
```

`QGIS_QT5_ROOT` / `QGIS_QT6_ROOT` override the QGIS install locations used by the scripts.

License: GPL-2.0-or-later
