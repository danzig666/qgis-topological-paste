# -*- coding: utf-8 -*-
"""Clipboard snapshot: native QGIS feature clipboard first, text formats second.

Why a staging layer?
--------------------
The QGIS feature clipboard (``QgsClipboard``) lives in the application, is not
part of the Python API and is not reachable through ``iface``. What *is*
reachable is the main window's public slot ``pasteAsNewMemoryVector(QString)``
("Paste Features As > Temporary Scratch Layer"). Invoked with a layer name it
runs without any dialog and builds a memory layer straight from the internal
clipboard: typed fields, real values and NULLs, multipart geometries, the
source CRS - independent of the "copy as text" format setting (it also works
when that is *attributes only*), of the source layer's current selection and
of whether the source layer still exists. It does not apply "avoid overlap" or
topological-editing settings, and it never touches the real destination.
This behaviour was verified on QGIS 3.44 with Qt5 and with Qt6 (see README).

Side effects of that slot and how they are undone (only our own objects):
the scratch layer is added to the project -> it is identified by id, read and
removed again; the active layer changes -> restored; the project is flagged
dirty -> the flag is cleared only if the project was clean before; a "features
pasted" message is pushed -> only message-bar items that appeared during
staging are dismissed. Neither the QGIS clipboard nor the system clipboard is
written, so both stay usable.

Native or external?
-------------------
QGIS has exactly two sources: its internal feature clipboard, or - when the
*system* clipboard changed after the last feature copy - the system text, which
it parses **while reusing the CRS of the previous feature copy** (a silent CRS
guess). The staged layer itself does not say which source was used, so it is
inferred:

* staged features that could NOT have been produced from the current system
  text (different count/geometry, unreadable or empty text) can only come from
  the internal clipboard -> native, CRS reliable. This also covers a system
  clipboard that Windows refused to update because another program had it open;
* staged features identical to the system text are native only if this process
  still owns the clipboard and the text carries the staged field names (it is
  the text QGIS generated itself), or if the staged fields are typed although
  the text is tabular (parsing text can only yield string fields);
* everything else is external text and is parsed here, with the CRS taken from
  the data (EWKT SRID, GeoJSON rules) or requested from the user.
The system clipboard is read before and after staging; if it changed in
between, the operation is refused instead of mixing two states.
"""

import csv
import io
import json
import re
import uuid

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsProject
from qgis.PyQt.QtCore import QThread
from qgis.PyQt.QtWidgets import QApplication

from . import compat
from .model import ClipboardSnapshot, IncomingFeature

STAGING_PREFIX = "__topological_paste_staging_"


class ClipboardError(Exception):
    """Nothing usable on the clipboard."""


class ClipboardCanceled(Exception):
    """The user cancelled a question that was needed to interpret the clipboard."""


class StagedData(object):
    def __init__(self, field_names, field_types, crs, rows, string_only=False):
        self.field_names = field_names
        self.field_types = field_types
        self.crs = crs
        self.rows = rows  # [(QgsGeometry, [values])] in clipboard order
        self.string_only = string_only  # every field is a text field (or there is none)

    def has_geometry(self):
        return any(not geometry.isNull() for geometry, _values in self.rows)


# ---------------------------------------------------------------------------------------
# native staging
# ---------------------------------------------------------------------------------------
def stage_native(iface):
    """Let QGIS paste its feature clipboard into a private scratch layer and read it.

    Returns (StagedData or None, slot_available). StagedData is None when nothing was
    pasted. The real destination layer is not involved in any way.
    """
    project = QgsProject.instance()
    main_window = iface.mainWindow()
    name = STAGING_PREFIX + uuid.uuid4().hex
    was_dirty = project.isDirty()
    active_before = iface.activeLayer()
    active_before_id = active_before.id() if active_before is not None else None
    canvas = iface.mapCanvas()
    was_frozen = canvas.isFrozen()

    message_bar = iface.messageBar()
    try:
        items_before = list(message_bar.items())
    except AttributeError:
        items_before = None

    added_ids = []

    def on_layers_added(layers):
        for layer in layers:
            if layer.name() == name:
                added_ids.append(layer.id())

    staged = None
    invoked = False
    project.layersAdded.connect(on_layers_added)
    canvas.freeze(True)  # the scratch layer is never meant to be drawn
    try:
        try:
            invoked = compat.invoke_slot_with_string(main_window, "pasteAsNewMemoryVector", name)
        except Exception:
            invoked = False
        if invoked:
            for layer_id in added_ids:
                layer = project.mapLayer(layer_id)
                if layer is not None and staged is None:
                    staged = _read_staged_layer(layer)
    finally:
        try:
            project.layersAdded.disconnect(on_layers_added)
        except (TypeError, RuntimeError):
            pass
        # remove only what this operation created
        for layer_id in added_ids:
            if project.mapLayer(layer_id) is not None:
                project.removeMapLayer(layer_id)
        if active_before_id is not None:
            layer = project.mapLayer(active_before_id)
            current = iface.activeLayer()
            if layer is not None and (current is None or current.id() != active_before_id):
                iface.setActiveLayer(layer)
        if added_ids and not was_dirty:
            project.setDirty(False)
        if items_before is not None:
            try:
                for item in list(message_bar.items()):
                    if item not in items_before:
                        message_bar.popWidget(item)
            except Exception:
                pass
        canvas.freeze(was_frozen)
    return staged, invoked


def _read_staged_layer(layer):
    fields = layer.fields()
    names = [f.name() for f in fields]
    types = dict((f.name(), f.typeName()) for f in fields)
    rows = []
    for feature in sorted(layer.getFeatures(), key=lambda f: f.id()):
        geometry = QgsGeometry(feature.geometry()) if feature.hasGeometry() else QgsGeometry()
        rows.append((geometry, list(feature.attributes())))
    string_only = all(compat.field_kind(f) == "string" for f in fields)
    return StagedData(names, types, QgsCoordinateReferenceSystem(layer.crs()), rows, string_only)


def _strip_geometry_heading(cells):
    if cells and cells[0] in ("wkt_geom", "wkb_geom"):
        return cells[1:]
    return cells


def staged_matches_system_clipboard(staged, text, html):
    """Is the system clipboard text the one QGIS generated for the staged features?"""
    if staged is None or not staged.rows:
        return False
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped[0] in "{[":
        try:
            data = json.loads(stripped)
        except ValueError:
            return False
        features = data.get("features") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("type") == "Feature":
            features = [data]
        if not isinstance(features, list) or len(features) != len(staged.rows):
            return False
        keys = set()
        for feature in features:
            keys.update((feature.get("properties") or {}).keys())
        return keys == set(staged.field_names) or not keys
    first_line = text.split("\n", 1)[0].rstrip("\r")
    if staged.field_names:
        return _strip_geometry_heading(first_line.split("\t")) == staged.field_names
    # source layer without attributes: QGIS writes bare WKT lines but still an HTML table
    return bool(html) and "<td>wkt_geom</td>" in html


def staged_derivable_from_text(staged, text):
    """Could QGIS have built exactly these staged features by parsing the system text?

    QGIS parses text with the same QgsGeometry/OGR routines used here, so text-born
    staged geometries are bit-identical to our own parse of the same text.
    """
    if not (text or "").strip():
        return False
    try:
        parsed = parse_clipboard_text(text)
    except ClipboardError:
        return False
    if len(parsed.features) != len(staged.rows):
        return False
    for feature, (geometry, _values) in zip(parsed.features, staged.rows):
        if feature.geometry.isNull() != geometry.isNull():
            return False
        if not geometry.isNull() and not geometry.equals(feature.geometry):
            return False
    return True


def staged_is_native(staged, text, html, owns_clipboard):
    """Decide whether the staged features come from the internal QGIS feature clipboard
    (CRS reliable) or from the system text (CRS unknown to QGIS). See module docstring."""
    if staged is None or not staged.rows or not staged.has_geometry():
        return False
    if not staged_derivable_from_text(staged, text):
        return True  # only the internal clipboard can have produced them
    if staged_matches_system_clipboard(staged, text, html):
        if owns_clipboard:
            return True  # the text QGIS generated itself for these very features
        tabular = (text or "").lstrip()[:1] not in ("{", "[")
        if tabular and staged.field_names and not staged.string_only:
            return True  # tabular text only yields string fields; typed fields prove the internal origin
    return False


def _snapshot_from_staged(staged, text):
    features = []
    for order, (geometry, values) in enumerate(staged.rows):
        attributes = dict(zip(staged.field_names, values))
        features.append(IncomingFeature(order, geometry, attributes))
    snapshot = ClipboardSnapshot(
        ClipboardSnapshot.ORIGIN_NATIVE, staged.crs, staged.field_names, features, field_types=staged.field_types
    )
    # Mixed geometry types cannot be staged: QGIS keeps one type and drops the other
    # geometries. If the text copy still shows WKT for such a row, refuse instead of
    # pasting a partial batch.
    table = _tabular_rows(text)
    if table is not None and len(table[1]) == len(features):
        for feature, (wkt, _values) in zip(features, table[1]):
            if feature.geometry.isNull() and wkt.strip():
                raise ClipboardError(
                    "The clipboard mixes different geometry types; QGIS cannot stage them together. "
                    "Copy one geometry type at a time."
                )
    return snapshot


# ---------------------------------------------------------------------------------------
# text formats (secondary)
# ---------------------------------------------------------------------------------------
_SRID = re.compile(r"^\s*SRID\s*=\s*(\d+)\s*;\s*", re.IGNORECASE)
_EPSG = re.compile(r"EPSG:{1,2}(?:[\d.]*:)?(\d+)", re.IGNORECASE)


def _tabular_rows(text):
    """QGIS' tab separated 'wkt_geom' format -> (field names, [(wkt, {name: value})]), else None."""
    if not text or not text.startswith("wkt_geom"):
        return None
    reader = csv.reader(io.StringIO(text), delimiter="\t", quotechar='"')
    try:
        header = next(reader)
    except StopIteration:
        return None
    names = header[1:]
    rows = []
    for cells in reader:
        if not cells:
            continue
        values = {}
        for i, name in enumerate(names):
            cell = cells[i + 1] if i + 1 < len(cells) else ""
            values[name] = cell if cell != "" else None  # the text format writes NULL as empty
        rows.append((cells[0], values))
    return names, rows


def _parse_tabular(text):
    table = _tabular_rows(text)
    if table is None:
        return None
    names, rows = table
    features = []
    for order, (wkt, values) in enumerate(rows):
        geometry = QgsGeometry.fromWkt(wkt) if wkt.strip() else QgsGeometry()
        if wkt.strip() and geometry.isNull():
            raise ClipboardError("Row %d of the clipboard table does not contain valid WKT." % (order + 1))
        features.append(IncomingFeature(order, geometry, values))
    snapshot = ClipboardSnapshot(
        ClipboardSnapshot.ORIGIN_TABULAR,
        QgsCoordinateReferenceSystem(),
        names,
        features,
        notes=["Text clipboard: all attribute values are text; empty cells are read as NULL."],
    )
    return snapshot


def _parse_wkt(text):
    def one(chunk):
        srid = None
        match = _SRID.match(chunk)
        if match:
            srid = int(match.group(1))
            chunk = chunk[match.end():]
        geometry = QgsGeometry.fromWkt(chunk.strip())
        return (geometry, srid) if not geometry.isNull() else None

    stripped = text.strip()
    if not stripped:
        return None
    whole = one(" ".join(stripped.split()))
    parsed = [whole] if whole else []
    if not parsed:
        for line in stripped.splitlines():
            if not line.strip():
                continue
            item = one(line)
            if item is None:
                return None  # strict: every non-empty line must be WKT
            parsed.append(item)
    if not parsed:
        return None
    srids = set(srid for _g, srid in parsed)
    crs = QgsCoordinateReferenceSystem()
    notes = []
    if len(srids) == 1 and None not in srids:
        crs = QgsCoordinateReferenceSystem("EPSG:%d" % srids.pop())
        notes.append("CRS taken from the EWKT SRID.")
    elif len(srids) > 1:
        if None in srids:
            raise ClipboardError("Some EWKT geometries carry an SRID and others do not; the CRS is ambiguous.")
        raise ClipboardError("The EWKT geometries on the clipboard use different SRIDs.")
    features = [IncomingFeature(order, geometry, {}) for order, (geometry, _s) in enumerate(parsed)]
    return ClipboardSnapshot(ClipboardSnapshot.ORIGIN_WKT, crs, [], features, notes=notes)


def _parse_geojson(text):
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    fields = compat.json_string_to_fields(stripped)
    parsed = compat.json_string_to_features(stripped, fields)
    if not parsed:
        return None
    names = [f.name() for f in fields]
    features = []
    for order, feature in enumerate(parsed):
        values = feature.attributes()
        attributes = dict((name, values[i] if i < len(values) else None) for i, name in enumerate(names))
        geometry = QgsGeometry(feature.geometry()) if feature.hasGeometry() else QgsGeometry()
        features.append(IncomingFeature(order, geometry, attributes))

    crs = QgsCoordinateReferenceSystem()
    notes = []
    declared = data.get("crs") if isinstance(data, dict) else None
    if isinstance(declared, dict):
        name = str((declared.get("properties") or {}).get("name", ""))
        match = _EPSG.search(name)
        if match:
            crs = QgsCoordinateReferenceSystem("EPSG:%s" % match.group(1))
            notes.append("CRS taken from the GeoJSON 'crs' member (%s)." % name)
        elif "CRS84" in name.upper():
            crs = QgsCoordinateReferenceSystem("EPSG:4326")
            notes.append("CRS taken from the GeoJSON 'crs' member (CRS84).")
    else:
        # RFC 7946: GeoJSON without a crs member IS WGS 84 lon/lat. That is a rule of the
        # format, not a guess - but only believed when the coordinates can be lon/lat.
        plausible = True
        for feature in features:
            if feature.geometry.isNull():
                continue
            box = feature.geometry.boundingBox()
            if box.xMinimum() < -180.0 or box.xMaximum() > 180.0 or box.yMinimum() < -90.0 or box.yMaximum() > 90.0:
                plausible = False
                break
        if plausible:
            crs = QgsCoordinateReferenceSystem("EPSG:4326")
            notes.append("GeoJSON without 'crs' member: WGS 84 (EPSG:4326) as defined by RFC 7946.")
        else:
            notes.append("GeoJSON coordinates are outside the lon/lat range, so RFC 7946 (WGS 84) cannot apply.")
    field_types = dict((f.name(), f.typeName()) for f in fields)
    return ClipboardSnapshot(ClipboardSnapshot.ORIGIN_GEOJSON, crs, names, features, notes=notes, field_types=field_types)


def parse_clipboard_text(text):
    """Parse external clipboard text. The CRS of the result may be invalid (= unknown)."""
    if not text or not text.strip():
        raise ClipboardError("The clipboard is empty.")
    for parser in (_parse_geojson, _parse_tabular, _parse_wkt):
        snapshot = parser(text)
        if snapshot is not None and snapshot.features:
            if all(f.geometry.isNull() for f in snapshot.features):
                raise ClipboardError("The clipboard text contains attributes but no geometry.")
            return snapshot
    raise ClipboardError(
        "The clipboard contains neither QGIS features nor WKT / EWKT / GeoJSON text that could be read."
    )


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------
def _read_system_clipboard(system_clipboard):
    """(text, html, readable). Retries briefly: on Windows the clipboard can be locked for a
    moment by another program (clipboard managers, a second QGIS reacting to the change),
    in which case Qt reports no formats at all. No events are processed while waiting."""
    text = html = ""
    formats = []
    for _attempt in range(8):
        mime = system_clipboard.mimeData()
        formats = list(mime.formats()) if mime is not None else []
        if formats:
            text = mime.text() if mime.hasText() else ""
            html = mime.html() if mime.hasHtml() else ""
            if text or html:
                break
        QThread.msleep(60)
    return text, html, bool(formats)


def read_clipboard(iface, ask_crs=None):
    """Snapshot the clipboard.

    ``ask_crs(snapshot)`` is called for external data whose CRS cannot be derived
    from the data; it returns a valid QgsCoordinateReferenceSystem or None (cancel).
    The destination CRS or the canvas CRS is never assumed.
    """
    system_clipboard = QApplication.clipboard()
    text, html, readable = _read_system_clipboard(system_clipboard)

    # QGIS itself decides between its internal clipboard and the system text, exactly as
    # its own paste would; the staged result is then classified.
    staged, slot_available = stage_native(iface)
    text_after, _html_after, _readable_after = _read_system_clipboard(system_clipboard)
    if text_after != text:
        raise ClipboardError(
            "The clipboard content changed while it was being read. Nothing was changed - please try again."
        )

    if staged_is_native(staged, text, html, system_clipboard.ownsClipboard()):
        snapshot = _snapshot_from_staged(staged, text)
        if not staged_matches_system_clipboard(staged, text, html):
            snapshot.notes.append(
                "The system clipboard holds other content than the QGIS feature clipboard (for example because "
                "another program blocked the clipboard while copying); the QGIS feature clipboard was used, "
                "exactly as the normal QGIS paste would."
            )
        if not snapshot.crs.isValid():
            snapshot.notes.append("The copied layer has no valid CRS.")
    else:
        if not text.strip():
            raise ClipboardError(
                "The clipboard is empty." if readable else "The clipboard is empty or could not be read."
            )
        snapshot = parse_clipboard_text(text)
        if not slot_available:
            snapshot.notes.append(
                "This QGIS version does not offer the 'paste as temporary scratch layer' slot used to read the "
                "native feature clipboard; the text clipboard was used instead (attribute values are text)."
            )

    if not snapshot.features:
        raise ClipboardError("The clipboard contains no features.")
    if not snapshot.crs.isValid():
        crs = ask_crs(snapshot) if ask_crs is not None else None
        if crs is None or not crs.isValid():
            raise ClipboardCanceled()
        snapshot.crs = crs
        snapshot.crs_note = "chosen by the user"
    return snapshot
