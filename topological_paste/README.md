# Topological Paste (QGIS plugin)

Paste the features on the clipboard – geometries **and** attributes – into the active vector
layer with *overwrite* semantics:

```
PASTE NEW COVERAGE → REMOVE OLD COVERAGE UNDER IT → RETAIN OLD COVERAGE OUTSIDE IT
→ PRESERVE ATTRIBUTE OWNERSHIP → AVOID DUPLICATES → VALIDATE → APPLY AS ONE UNDOABLE EDIT
```

* Pasted features win. Their coverage is cut out of the existing features underneath.
* An existing feature is deleted only when nothing of it remains; what survives keeps its attributes and its feature id.
* A pasted geometry that already exists (topologically equal) is **reused**, never duplicated – repeated pastes are no-ops.
* Shared boundaries are noded consistently on both sides.
* Everything is **one** QGIS edit command: one Undo reverts the whole paste, Redo reapplies it. Nothing is ever saved for you.
* A compact preview is shown before anything changes.

"Underneath" is a spatial relationship inside the active layer. Drawing order, selection,
canvas visibility and other layers play no role. Main use case: polygon layers (cadastre,
zoning); lines and points are supported with the explicit rules below.

## Installation

1. *Plugins ▸ Manage and Install Plugins… ▸ Install from ZIP* and choose `topological_paste-<version>.zip`
   (or unzip it into `<profile>/python/plugins/` so that `…/plugins/topological_paste/metadata.txt` exists).
2. Enable **Topological Paste**.

No pip packages, no external programs, no other plugin, no resource compilation. Only Python and the
libraries bundled with QGIS are used (`qgis.core`, `qgis.gui`, `qgis.PyQt`, standard library, bundled GEOS).

## Use

1. Select features and copy them with the normal QGIS *Copy Features* (Ctrl+C).
2. Activate the destination layer (another layer or the same one).
3. Run **Topological Paste**: toolbar button, *Edit ▸ Topological Paste* (directly below *Paste Features*),
   *Vector ▸ Topological Paste*, or the shortcut **Ctrl+Alt+Shift+V** (registered with QGIS – change it in
   *Settings ▸ Keyboard Shortcuts*). Ctrl+V and text pasting in forms are left alone.
4. Check the preview (counts, warnings, optional map highlight: green = pasted, orange = trimmed, red = deleted)
   and press **Apply** or **Cancel**.

The layer is put into edit mode only after you press Apply. Save or roll back as usual.

## Behaviour in detail

### Polygon overwrite rule
For incoming polygons `P1…Pn` in clipboard order: `Q_i = P_i − ⋃(P_j, j>i)` (where incoming features overlap
each other the **last** one wins, inside the overlap only); `U = ⋃P_i` is used purely as a cutting mask – the
incoming features are never dissolved and keep their own attributes. For every existing polygon `E` that shares
*area* with `U`: `R = E − U`; empty ⇒ delete `E`, otherwise `E` keeps its id and attributes with geometry `R`.
Holes of incoming polygons stay holes in the mask. Edge/vertex contact is not overlap.

A fully covered incoming feature is reported as *superseded*, not as an error.

### Duplicates and identity
The **final** incoming geometries (after priority resolution and type adaptation) are compared with existing ones
by GEOS topological equality (ring direction, start vertex, part order, redundant collinear vertices do not matter;
bounding boxes only pre-filter). An equal existing feature is reused: id and managed key kept, geometry
representation untouched, mapped attributes updated; if nothing differs the paste is a true no-op (the layer is
not even put into edit mode). Several identical existing features are consolidated to one (lowest committed id),
with the attribute conflicts listed. Nothing outside the pasted footprint is de-duplicated.

### Multipart / singlepart
Multipart targets keep all surviving parts in one feature. On strictly singlepart targets a disconnected result is
split: the largest part (ties: lower-left bounding-box corner, then WKB) keeps the feature id, every other part
becomes a new feature with the same ordinary attributes and a regenerated managed key. No part is ever dropped.
The same adaptation applies to incoming multipart features.

### Lines and points
* **Points** – coincidence means identical coordinates. A coincident existing point is updated (reused); duplicates
  inside the batch: last wins; from multipoints only the covered components are removed.
* **Lines** – only *coincident portions* (shared 1‑D parts) are removed from existing lines; the rest keeps its
  attributes and digitising direction. A crossing at a point removes nothing; instead the crossing point is
  inserted as a vertex into both lines. No buffer width is ever invented.

### Z/M and curves (explicit policy)
* Target layers with Z or M, and curved target types, are **rejected before editing** (a 2‑D overlay would have to
  invent Z/M values or flatten curves).
* Incoming curved geometries are rejected. Incoming Z/M values pasted into a 2‑D layer are dropped **only after
  you accept that in the preview**.
* A different geometry family (e.g. lines into a polygon layer) is rejected before editing.

### CRS and precision
Overlay runs in the destination layer's CRS with planar operations; transformation uses the project's transform
context and failures abort before editing. **Coordinates are preserved by default**: no buffering, snapping,
rounding or removal of small polygons. *Advanced ▸ precision grid* (off by default, units shown) snaps the pasted
features, the trimmed features **and** their touching neighbours to one grid, validates again and requires your
acceptance. If the layer itself has a *geometry precision* set in its properties, QGIS snaps every edit anyway, so
that value is used as the grid (reported).

Tolerances: the only computational tolerance is the noding tolerance `1e-12 × largest coordinate`
(≈ 0.65 µm for EOV/HD72 coordinates). It decides whether an existing vertex lies on a neighbouring segment
(the vertex is then inserted there, at its own coordinates – nothing is moved) and scales the validation limits
(`tolerance × boundary length`). It never deletes or merges anything.

Invalid geometries are repaired with `makeValid` on copies only; all parts of the family are kept; a repair needs
your acceptance; a repair that collapses the dimension, or an invalid existing geometry that cannot be repaired,
blocks the paste. A failed geometry operation (error, null, invalid result, failed size bookkeeping) is never read
as "nothing remains": the feature is left alone and the paste is blocked.

### Shared boundaries and scope
After cutting, vertices are inserted so that pasted features, trimmed remainders and merely *touching* neighbours
carry identical nodes (neighbours only receive vertices of the pasted/trimmed set – this is not a layer clean-up).
The report separates: topology **validated in the affected area**, **pre-existing issues left unchanged**
(e.g. old features that already overlap outside the pasted coverage – no ownership decision is made for them),
and **blocking** issues. The plugin never claims that the whole layer is topologically correct.

### Attributes
Mapping is by exact field name (optional explicit *Field mapping…*; nothing is guessed, not even by letter case).
New features: mapped values, then legitimate defaults (layer default expression, provider default clause/literal)
for fields *absent* from the source, else NULL. An explicit incoming NULL is a value: it overwrites on reuse unless a
NOT NULL constraint forbids it. Joined, virtual, read-only and provider-managed key fields are never written.
Managed keys are recognised from provider metadata (primary key **with** a provider generator such as GeoPackage
`fid`/`Autogenerate`, serial/identity columns), not from names; they are regenerated for new features/fragments and
preserved for reused features. NOT NULL, UNIQUE and expression constraints are checked against the *planned final
state including pending edits*; hard violations block, soft ones warn. Business identifiers are never altered to
dodge a unique constraint (note: splitting a feature on a singlepart layer duplicates its attributes – with a hard
unique constraint that paste is blocked). Unmapped fields, failed and lossy conversions are listed before applying.
Trimmed features keep their attributes; only fields configured in QGIS as *apply default value on update* are
recomputed (by QGIS itself) and this is reported. Provider-side rules (triggers, foreign keys, CHECK) run only when
you save; the plugin says so and never claims they passed. Related layers are never touched.

### Clipboard
The native QGIS feature clipboard is not exposed to Python. The plugin invokes the main window's own slot
`pasteAsNewMemoryVector(name)` (*Paste Features As ▸ Temporary Scratch Layer*) to let QGIS paste its internal
clipboard into a private scratch layer, reads it (typed fields, NULLs, multipart geometry, source CRS, clipboard
order) and removes it again; active layer, project dirty flag and message bar are restored, only objects created
by the operation are removed, and neither the QGIS clipboard nor the system clipboard is written. The real
destination is never used for staging. This works when the text copy format is *attributes only*, after the source
selection changed and after the source layer was removed. Avoid-intersection and topological-editing project
settings do not influence staging or the result.

QGIS itself has two sources: its internal feature clipboard or – if the *system* clipboard changed after the last
feature copy – the system text, which it parses **while reusing the CRS of the previous copy**. The plugin therefore
classifies the staged features: if they could not have been produced from the current system text they can only come
from the internal clipboard (native, CRS reliable – this also covers the case where Windows refused the copy because
another program had the clipboard open); if they are identical to the system text they count as native only when this
QGIS still owns the clipboard and the text carries the staged field names, or when the staged fields are typed although
the text is tabular. Everything else is external text: GeoJSON (CRS from its `crs` member, else WGS 84 per RFC 7946 –
only if the coordinates can be lon/lat), QGIS tabular `wkt_geom` text (e.g. copied in another QGIS instance), WKT and
EWKT (`SRID=…;`). If the CRS cannot be derived you are asked for it; it is never assumed from the layer or the map.
The system clipboard is read before and after staging; if it changed in between, the paste is refused ("try again").

### Safety
Two phases. *Plan*: clipboard and target are snapshotted (pending edits and temporary ids included), all geometry
work happens on copies, the result is an explicit list of geometry changes, attribute changes, deletions and
insertions. *Apply*: refused if fields, CRS, filter, edit state, undo position or any feature in the affected
boxes changed (also *new* features) – you are offered a recalculation. One `beginEditCommand`/`endEditCommand`;
every edit call is checked and the result is read back **before** the command is closed; on any failure
`destroyEditCommand()` removes exactly this command's partial edits, the previous selection is restored and earlier
unsaved edits stay intact. `commitChanges()` is never called, `rollBack()` only closes an empty session the plugin
itself opened, the provider is never written directly. Re-entrancy is guarded; the only event processing happens
inside an application-modal progress dialog.

**Rejected configurations:** layers in transaction groups (automatic or buffered), layers with a provider filter
(hidden features could collide unseen – the filter is not removed for you), read-only layers, active foreign edit
command, Z/M or curved targets.

## Limitations

* Clipboard order defines priority among overlapping incoming features. QGIS fills the clipboard from the selection
  set, so the order of a multi-selection is fixed per copy but not meaningful; the preview tells you which one is last.
* Staging uses a QGIS memory layer: a value that violates its *own source field's* declared length/precision is
  normalised by QGIS exactly as with the built-in paste.
* Values from text clipboards are text; empty cells are read as NULL.
* Staging relies on the `pasteAsNewMemoryVector(QString)` slot of the QGIS main window. If a future QGIS removes it,
  the plugin falls back to the text clipboard (WKT formats, CRS requested) and says so; it does not fail silently.
* Python-level noding loops over vertices of the affected features; extremely dense geometries take noticeable time
  (progress + Cancel are provided).
* Geometry-collection layers, Z/M layers, curved layers and mixed-geometry clipboards are not supported (rejected).

## Compatibility matrix

| Runtime | Status |
|---|---|
| QGIS 3.44.9 LTR, Qt 5.15.13, GEOS 3.14.1, Python 3.12, Windows 11 | **executed**: 74 headless tests + 10 GUI integration tests, all pass |
| QGIS 3.44.4 **Qt6** build (Qt 6.8.1, PyQt6), GEOS 3.13.1, Windows 11 | **executed**: 74 headless tests + 10 GUI integration tests, all pass |
| QGIS 3.28 – 3.42 (Qt5) | not executed. Declared minimum 3.28 from an API audit (all APIs used exist there; newer ones are guarded in `compat.py`) |
| QGIS 4.x (Qt6) | **not executed** (no QGIS 4 runtime was available). Expected to work: Qt6/PyQt6 paths are exercised on the 3.44 Qt6 build, `qgisMaximumVersion=4.99` is set as QGIS 4 requires |
| QGIS 5 | does not exist at the time of writing; nothing is claimed. Raise `qgisMaximumVersion` after testing |
| Linux / macOS | not executed; no platform-specific code is used |

`metadata.txt`: `qgisMinimumVersion=3.28`, `qgisMaximumVersion=4.99`, `supportsQt6=True` (needed by Qt6 builds of 3.x).

## Tests

```
run_tests.cmd qt5        headless suite (python-qgis), acceptance tests A–L, N, P–S
run_tests.cmd qt6
run_gui_tests.cmd qt5    real QGIS with a throw-away profile: native clipboard (M), project settings (O),
run_gui_tests.cmd qt6    CRS question, UI registration, undo/redo through the plugin action
```

Both scripts accept `QGIS_QT5_ROOT` / `QGIS_QT6_ROOT`. The GUI tests replace the system clipboard content; they retry a
copy when another program (clipboard manager, a second QGIS) blocks the Windows clipboard and report the number of retries.
Executed vs. not executed: everything listed above was executed on the two 3.44 builds; nothing was executed on
QGIS < 3.44, on QGIS 4.x, on Linux/macOS or against PostGIS. A forced OS-level clipboard failure cannot be scripted;
its classification rule is covered by the unit test `test_native_detection_rules`.
See `MANUAL_CHECKLIST.md` for the manual verification list. Tests run on memory layers and temporary GeoPackages.

## Module map

| Module | Responsibility |
|---|---|
| `compat.py` | Qt5/Qt6 and QGIS API differences (QAction, scoped enums, dialog exec, field types, topological equality, JSON helpers, transactions) |
| `clipboard.py` | native staging + text formats → `ClipboardSnapshot` |
| `geometry_ops.py` | checked GEOS operations, type adaptation, shared-boundary noding |
| `planner.py` | phase A: the change plan, validation, stale fingerprint |
| `attributes.py` | mapping, conversion, defaults, managed keys, constraints |
| `applier.py` | phase B: stale check, single edit command, verification, rollback of the command |
| `preview_dialog.py`, `settings_dialog.py`, `report.py`, `plugin.py` | UI |

License: GPL-2.0-or-later.
