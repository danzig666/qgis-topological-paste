# Manual verification checklist

Use a copy of real data (e.g. a cadastral GeoPackage in EPSG:23700). Tick every item per QGIS version you release for.

## Installation / UI
- [ ] *Install from ZIP* works; the plugin loads without Python errors (Qt5 build, Qt6 build / QGIS 4).
- [ ] Toolbar button with a legible icon, *Edit ▸ Topological Paste* below *Paste Features*, *Vector ▸ Topological Paste*.
- [ ] Shortcut Ctrl+Alt+Shift+V works and is listed/editable in *Settings ▸ Keyboard Shortcuts*.
- [ ] Ctrl+V still performs the normal QGIS paste; Ctrl+V inside attribute forms / text fields pastes text.
- [ ] The action is disabled for raster / non-spatial layers.

## Native clipboard
- [ ] Copy from layer A, paste into layer B (different field order, different CRS): geometry, values, NULLs, dates, Unicode (ő, ű) arrive.
- [ ] Copy and paste within the same layer without moving anything: "Nothing to do", layer not put into edit mode.
- [ ] Change or clear the selection in the source layer after copying: the copied features are pasted, not the new selection.
- [ ] Remove the source layer after copying: paste still works.
- [ ] *Settings ▸ Options ▸ Data Sources ▸ Copy features as* = "Plain text, no geometry": paste still brings geometries.
- [ ] During the paste no scratch layer stays in the layer tree, the active layer is still the destination, a clean project stays clean after Cancel.
- [ ] After the paste, the normal QGIS paste (Ctrl+V) and pasting the text into a text editor both still work.
- [ ] Copy WKT text from a text editor: the plugin asks for the CRS; Cancel changes nothing.
- [ ] Copy features in a *second* QGIS instance: the plugin treats it as text and asks for the CRS.
- [ ] GeoJSON text (lon/lat) is accepted as EPSG:4326 and says so in the preview notes.

## Overwrite semantics (polygons)
- [ ] Partial overlap: old feature trimmed, attributes kept; pasted feature complete; no overlap, no gap.
- [ ] Pasted polygon covering an old one completely: old one deleted.
- [ ] Pasted polygon inside an old one: old one gets a hole.
- [ ] Strip across an old polygon: singlepart layer → two features (one keeps the id); multipart layer → one multipart feature.
- [ ] Polygon with a hole pasted over data: features inside the hole are untouched.
- [ ] Neighbour sharing only an edge/vertex: not trimmed; if segmentation differs it only receives vertices ("Neighbours receiving boundary vertices only").
- [ ] Two overlapping features copied together: the last one wins inside the overlap; preview names the last one.
- [ ] Paste the same clipboard three times: second and third paste are no-ops; feature count and ids stay.
- [ ] Off-screen and unselected features under the pasted area are trimmed as well.

## Editing safety
- [ ] Layer not in edit mode: edit mode starts only after *Apply*; nothing is saved automatically.
- [ ] With earlier unsaved edits (including newly digitised, unsaved features under the pasted area): one Ctrl+Z removes the whole paste and leaves the earlier edits; Ctrl+Shift+Z reapplies it.
- [ ] Cancel in the preview / in the progress dialog: nothing changes.
- [ ] Layer with a provider filter: refused with an explanation; the filter is still set.
- [ ] Project with transaction groups enabled (PostGIS/GeoPackage): refused with an explanation.
- [ ] Save edits to GeoPackage / PostGIS after a paste with split features: new keys are generated, no key collision.
- [ ] Unique business id + split on a singlepart layer: paste is blocked with a clear message, nothing changed.

## Project settings
- [ ] *Avoid overlap* = active layer / advanced configuration and *topological editing* ON: result identical (pasted wins, incoming not clipped), no other layer modified or put into edit mode.

## Precision / repair
- [ ] Default: pasted coordinates identical to the source (check with the vertex editor).
- [ ] Precision grid ON: acceptance box appears; all involved vertices lie on the grid; units are displayed.
- [ ] Invalid (bow-tie) source polygon: repair is listed and needs acceptance.
- [ ] Layer with *geometry precision* set in its properties: preview reports that this precision is used.

## Lines / points
- [ ] Line pasted over part of an existing line: only the coincident portion of the old line disappears.
- [ ] Crossing lines: both remain, both get a vertex at the crossing.
- [ ] Point pasted on an existing point: the existing point's attributes are updated, no second point.
- [ ] Polygons on the clipboard, line layer active: refused before any edit.
