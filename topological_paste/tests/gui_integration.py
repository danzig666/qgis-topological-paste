# -*- coding: utf-8 -*-
"""GUI integration tests (acceptance tests M and O, plus the full plugin path).

These need the real QGIS application because the native feature clipboard only
exists there. Run with (see run_gui_tests.cmd):

    qgis --profiles-path <scratch dir> --code topological_paste/tests/gui_integration.py

The script drives the plugin exactly like a user would (Copy Features, make
another layer active, trigger the action), auto-answers the preview, writes a
report to the path in TOPOPASTE_GUI_REPORT (default: next to this file) and
quits QGIS. It uses a throw-away profile, never the user's.
"""

import os
import sys
import traceback

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import QT_VERSION_STR, QDate, QThread, QTimer
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface

HERE = os.environ.get("TOPOPASTE_TEST_DIR") or os.path.dirname(os.path.abspath(globals().get("__file__", "")))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
REPORT = os.environ.get("TOPOPASTE_GUI_REPORT") or os.path.join(HERE, "gui_report_qt%s.txt" % QT_VERSION_STR[0])

RESULTS = []


def rect(x0, y0, x1, y1):
    return "POLYGON((%s %s,%s %s,%s %s,%s %s,%s %s))" % (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)


def new_layer(name, rows, fields="name:string(80)&field=n:integer&field=d:double(20,10)&field=dt:date&field=b:boolean",
              wkb="Polygon", crs="EPSG:23700"):
    layer = QgsVectorLayer("%s?crs=%s&field=%s" % (wkb, crs, fields), name, "memory")
    features = []
    for wkt, attributes in rows:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttributes(attributes)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def named(layer, value):
    return [f for f in layer.getFeatures() if f["name"] == value]


def check(condition, text):
    if not condition:
        raise AssertionError(text)


class Harness(object):
    def __init__(self):
        from topological_paste.plugin import TopologicalPastePlugin
        from topological_paste.preview_dialog import PreviewDialog

        self.PreviewDialog = PreviewDialog
        self.plugin = TopologicalPastePlugin(iface)
        self.plugin.initGui()
        self.messages = []
        self.dialogs = 0
        self.crs_questions = 0
        self.clipboard_retries = 0
        original_message = self.plugin._message

        def record(text, level, duration=8):
            self.messages.append(text)
            original_message(text, level, duration)

        self.plugin._message = record
        self.plugin.preview_runner = self.auto_apply

    def auto_apply(self, dialog):
        self.dialogs += 1
        dialog._zoom()  # exercise the preview helpers on the real canvas
        dialog.highlight_box.setChecked(True)
        dialog.highlight_box.setChecked(False)
        if dialog.accept_box.isVisible() or dialog.plan.needs_acceptance():
            dialog.accept_box.setChecked(True)
        self.last_plan = dialog.plan
        return self.PreviewDialog.APPLY if not dialog.plan.blocking() else self.PreviewDialog.CANCEL

    def copy(self, layer, fids=None):
        """Copy Features, and make sure Windows really accepted the clipboard content.

        Another program (clipboard manager, a second QGIS reacting to clipboard changes) can
        hold the clipboard open for a moment; the copy then silently fails at OS level. A
        user would simply press Ctrl+C again - so does the test."""
        if fids is None:
            layer.selectAll()
        else:
            layer.selectByIds(fids)
        clipboard = QApplication.clipboard()
        for _attempt in range(20):
            iface.copySelectionToClipboard(layer)
            QApplication.processEvents()
            if clipboard.ownsClipboard() and list(clipboard.mimeData().formats()):
                return
            self.clipboard_retries += 1
            QThread.msleep(150)
        raise AssertionError("the system clipboard stays blocked by another program")

    def set_text(self, text):
        clipboard = QApplication.clipboard()
        for _attempt in range(20):
            clipboard.setText(text)
            QApplication.processEvents()
            if clipboard.ownsClipboard() and clipboard.text() == text:
                return
            self.clipboard_retries += 1
            QThread.msleep(150)
        raise AssertionError("the system clipboard stays blocked by another program")

    def paste_into(self, layer):
        iface.setActiveLayer(layer)
        for _attempt in range(4):
            self.messages = []
            self.plugin.action.trigger()
            # Another program (clipboard manager, a second QGIS) may lock the Windows clipboard
            # for a moment; the plugin then refuses safely and the user simply tries again.
            if not any("could not be read" in m or "try again" in m for m in self.messages):
                break
            self.clipboard_retries += 1
            QApplication.processEvents()
        return self.messages


def test_ui_registration(h):
    action = h.plugin.action
    check(action.text() == "Topological Paste", "action text")
    check(not action.icon().isNull(), "icon loads without compiled resources")
    check(action in iface.editMenu().actions(), "action is in the Edit menu")
    shortcut = action.shortcut().toString()
    check(shortcut != "" and shortcut != "Ctrl+V", "shortcut registered: %r" % shortcut)
    clashes = [
        a.objectName()
        for a in iface.mainWindow().findChildren(type(action))
        if a is not action and a.shortcut().toString() == shortcut
    ]
    check(not clashes, "shortcut clashes with %s" % clashes)
    paste = iface.actionPasteFeatures()
    check(paste.shortcut().toString() in ("Ctrl+V", ""), "normal paste shortcut untouched")
    return "shortcut %s" % shortcut


def test_m_cross_layer_native_copy(h):
    source = new_layer("m_source", [
        (rect(5, 0, 15, 10), ["árvíztűrő őű", 0, 0.123456789, QDate(2024, 2, 29), True]),
        (rect(30, 0, 40, 10), [None, None, None, None, None]),
    ])
    other = new_layer("m_bystander", [(rect(0, 0, 100, 100), ["bystander", 1, 1.0, None, None])])
    target = new_layer("m_target", [(rect(0, 0, 10, 10), ["old", 1, 1.0, None, False])])
    bystander_before = [f.geometry().asWkt() for f in other.getFeatures()]
    project = QgsProject.instance()
    layer_ids = set(project.mapLayers().keys())

    h.copy(source)
    clipboard_text = QApplication.clipboard().text()
    # the source selection changes after copying: must not matter
    source.removeSelection()
    project.setDirty(False)
    messages = h.paste_into(target)

    check(iface.activeLayer() is not None and iface.activeLayer().id() == target.id(), "destination stays the active layer")
    check(set(project.mapLayers().keys()) == layer_ids, "no staging layer left behind")
    check(not [l for l in project.mapLayers().values() if l.name().startswith("__topological_paste")], "staging layer removed")
    check(QApplication.clipboard().text() == clipboard_text, "system clipboard untouched")
    check(target.featureCount() == 3, "two pasted + one trimmed, got %d (%s)" % (target.featureCount(), messages))
    old = named(target, "old")[0]
    check(abs(old.geometry().area() - 50.0) < 1e-9, "existing feature trimmed to 50, got %s" % old.geometry().area())
    new = named(target, "árvíztűrő őű")
    check(len(new) == 1, "unicode name arrived")
    new = new[0]
    check(new["n"] == 0 and new["d"] == 0.123456789 and new["dt"] == QDate(2024, 2, 29) and new["b"] is True,
          "typed values arrived: %r" % new.attributes())
    nulls = [f for f in target.getFeatures() if abs(f.geometry().boundingBox().xMinimum() - 30) < 1e-9][0]
    check(all(v is None or v == None or str(v) == "NULL" for v in nulls.attributes()), "NULLs preserved: %r" % nulls.attributes())  # noqa: E711
    check([f.geometry().asWkt() for f in other.getFeatures()] == bystander_before, "other layers untouched")
    check(not other.isEditable(), "other layers not put into edit mode")
    check(target.isEditable() and target.isModified(), "target left in edit mode, not committed")
    check(target.undoStack().count() == 1, "one undo command")
    check(len(target.selectedFeatureIds()) == 2, "the pasted features are selected")
    target.undoStack().undo()
    check(target.featureCount() == 1 and abs(named(target, "old")[0].geometry().area() - 100.0) < 1e-9, "undo restores")
    target.undoStack().redo()
    check(target.featureCount() == 3, "redo reapplies")

    # the native clipboard is still usable by QGIS itself afterwards
    probe = new_layer("m_probe", [])
    probe.startEditing()
    iface.pasteFromClipboard(probe)
    check(probe.featureCount() == 2, "QGIS' own paste still works after staging (%d)" % probe.featureCount())
    probe.rollBack()
    for layer in (source, other, target, probe):
        if layer.isEditable():
            layer.rollBack()
        project.removeMapLayer(layer.id())
    return "%d preview(s)" % h.dialogs


def test_m_same_layer_copy_is_noop(h):
    layer = new_layer("m_same", [(rect(0, 0, 10, 10), ["a", 1, 1.0, None, True]), (rect(10, 0, 20, 10), ["b", 2, 2.0, None, False])])
    h.copy(layer)
    before = sorted((f.id(), f.geometry().asWkt(), [str(v) for v in f.attributes()]) for f in layer.getFeatures())
    messages = h.paste_into(layer)
    after = sorted((f.id(), f.geometry().asWkt(), [str(v) for v in f.attributes()]) for f in layer.getFeatures())
    check(before == after, "same-layer paste changes nothing")
    check(not layer.isEditable(), "no edit session opened for a no-op")
    check(any("Nothing to do" in m for m in messages), "no-op is reported: %s" % messages)
    QgsProject.instance().removeMapLayer(layer.id())


def test_m_attributes_only_text_format(h):
    settings = QgsSettings()
    previous = settings.value("qgis/copyFeatureFormat")
    settings.setValue("qgis/copyFeatureFormat", 0)  # QgsClipboard::AttributesOnly
    try:
        source = new_layer("m_attr_source", [(rect(0, 0, 10, 10), ["geom kept", 7, 1.5, None, True])])
        target = new_layer("m_attr_target", [])
        h.copy(source)
        text = QApplication.clipboard().text()
        check("wkt_geom" not in text and "Polygon" not in text, "text clipboard really has no geometry: %r" % text)
        h.paste_into(target)
        check(target.featureCount() == 1, "feature pasted from the internal clipboard")
        feature = next(target.getFeatures())
        check(abs(feature.geometry().area() - 100.0) < 1e-9 and feature["n"] == 7, "geometry + attributes arrived")
    finally:
        if previous is None:
            settings.remove("qgis/copyFeatureFormat")
        else:
            settings.setValue("qgis/copyFeatureFormat", previous)
    for layer in (source, target):
        if layer.isEditable():
            layer.rollBack()
        QgsProject.instance().removeMapLayer(layer.id())


def test_m_source_layer_gone(h):
    source = new_layer("m_gone_source", [(rect(0, 0, 10, 10), ["survivor", 3, 1.5, None, True])], crs="EPSG:4326")
    target = new_layer("m_gone_target", [], crs="EPSG:4326")
    h.copy(source)
    iface.setActiveLayer(target)
    QgsProject.instance().removeMapLayer(source.id())  # copied data must outlive the source layer
    h.paste_into(target)
    check(target.featureCount() == 1 and next(target.getFeatures())["name"] == "survivor", "pasted after source removal")
    target.rollBack()
    QgsProject.instance().removeMapLayer(target.id())


def test_n_external_text_asks_for_crs(h):
    # 1) a native copy in EPSG:4326 leaves a CRS behind inside QGIS' clipboard ...
    source = new_layer("n_source", [(rect(19, 47, 19.1, 47.1), ["native", 1, 1.0, None, True])], crs="EPSG:4326")
    target = new_layer("n_target", [])
    h.copy(source)
    # 2) ... then external WKT (HD72 metres) replaces the system clipboard
    h.set_text(rect(650000, 240000, 650100, 240100))
    asked = []

    def fake_question(snapshot):
        asked.append(snapshot.origin)
        return QgsCoordinateReferenceSystem("EPSG:23700")

    h.plugin._ask_crs = fake_question
    h.paste_into(target)
    check(asked, "the CRS was requested instead of reusing the stale native CRS")
    check(target.featureCount() == 1, "external WKT pasted")
    box = next(target.getFeatures()).geometry().boundingBox()
    check(abs(box.xMinimum() - 650000) < 1e-6, "coordinates taken as EPSG:23700, not transformed from 4326: %s" % box.toString())

    # cancelling the question changes nothing
    target.rollBack()
    h.plugin._ask_crs = lambda snapshot: None
    messages = h.paste_into(target)
    check(target.featureCount() == 0 and not target.isEditable(), "cancelled CRS question -> nothing pasted")
    check(any("Cancelled" in m for m in messages), "cancel reported")
    for layer in (source, target):
        QgsProject.instance().removeMapLayer(layer.id())


def test_o_project_settings_do_not_reverse_priority(h):
    project = QgsProject.instance()
    other = new_layer("o_other", [(rect(0, 0, 100, 100), ["other", 1, 1.0, None, None])])
    source = new_layer("o_source", [(rect(5, 0, 15, 10), ["new", 2, 1.0, None, None])])
    target = new_layer("o_target", [(rect(0, 0, 10, 10), ["old", 1, 1.0, None, None])])
    other_before = [f.geometry().asWkt() for f in other.getFeatures()]
    previous_mode = project.avoidIntersectionsMode()
    previous_layers = project.avoidIntersectionsLayers()
    previous_topological = project.topologicalEditing()
    try:
        project.setTopologicalEditing(True)
        for mode_name in ("AvoidIntersectionsCurrentLayer", "AvoidIntersectionsLayers"):
            mode = getattr(Qgis.AvoidIntersectionsMode, mode_name)
            project.setAvoidIntersectionsMode(mode)
            project.setAvoidIntersectionsLayers([other, target])
            h.copy(source)
            h.paste_into(target)
            new = named(target, "new")
            check(len(new) == 1 and abs(new[0].geometry().area() - 100.0) < 1e-9,
                  "%s: incoming polygon was NOT clipped by existing features" % mode_name)
            check(abs(named(target, "old")[0].geometry().area() - 50.0) < 1e-9, "%s: pasted wins" % mode_name)
            check([f.geometry().asWkt() for f in other.getFeatures()] == other_before, "%s: other layer untouched" % mode_name)
            check(not other.isEditable(), "%s: other layer not in edit mode" % mode_name)
            target.rollBack()
    finally:
        project.setAvoidIntersectionsMode(previous_mode)
        project.setAvoidIntersectionsLayers(previous_layers)
        project.setTopologicalEditing(previous_topological)
    for layer in (other, source, target):
        project.removeMapLayer(layer.id())


def test_clean_project_stays_clean_when_cancelled(h):
    project = QgsProject.instance()
    source = new_layer("c_source", [(rect(5, 0, 15, 10), ["new", 2, 1.0, None, None])])
    target = new_layer("c_target", [(rect(0, 0, 10, 10), ["old", 1, 1.0, None, None])])
    h.copy(source)
    h.plugin.preview_runner = lambda dialog: h.PreviewDialog.CANCEL
    project.setDirty(False)
    h.paste_into(target)
    check(not project.isDirty(), "staging + cancelled preview leave a clean project clean")
    check(target.featureCount() == 1 and not target.isEditable(), "cancel changes nothing")
    h.plugin.preview_runner = h.auto_apply
    for layer in (source, target):
        project.removeMapLayer(layer.id())


def test_fallback_when_staging_slot_is_missing(h):
    """A QGIS without the scratch-layer slot: text clipboard is used, CRS is asked, the user is told."""
    from topological_paste import compat

    source = new_layer("f_source", [(rect(5, 0, 15, 10), ["from text", 2, 1.0, None, None])])
    target = new_layer("f_target", [(rect(0, 0, 10, 10), ["old", 1, 1.0, None, None])])
    h.copy(source)
    original = compat.invoke_slot_with_string
    asked = []
    compat.invoke_slot_with_string = lambda obj, slot, text: False
    h.plugin._ask_crs = lambda snapshot: asked.append(1) or QgsCoordinateReferenceSystem("EPSG:23700")
    try:
        h.paste_into(target)
    finally:
        compat.invoke_slot_with_string = original
    check(asked, "CRS requested for the text fallback")
    check(any("scratch layer" in note for note in h.last_plan.snapshot.notes), "fallback is announced in the preview")
    pasted = named(target, "from text")
    check(len(pasted) == 1 and pasted[0]["n"] == 2, "text values converted into the typed target fields")
    check(abs(named(target, "old")[0].geometry().area() - 50.0) < 1e-9, "overwrite semantics unchanged")
    target.rollBack()
    for layer in (source, target):
        QgsProject.instance().removeMapLayer(layer.id())


def test_dialogs_construct(h):
    from topological_paste.preview_dialog import FieldMappingDialog
    from topological_paste.settings_dialog import SettingsDialog

    SettingsDialog(iface.mainWindow()).deleteLater()
    FieldMappingDialog(h.last_plan, ["name", "n"], iface.mainWindow()).deleteLater()


TESTS = [
    test_ui_registration,
    test_m_cross_layer_native_copy,
    test_m_same_layer_copy_is_noop,
    test_m_attributes_only_text_format,
    test_m_source_layer_gone,
    test_n_external_text_asks_for_crs,
    test_o_project_settings_do_not_reverse_priority,
    test_clean_project_stays_clean_when_cancelled,
    test_fallback_when_staging_slot_is_missing,
    test_dialogs_construct,
]


def main():
    lines = ["QGIS %s / Qt %s" % (Qgis.QGIS_VERSION, QT_VERSION_STR)]
    failed = 0
    try:
        harness = Harness()
        for test in TESTS:
            try:
                note = test(harness)
                lines.append("PASS  %s%s" % (test.__name__, "  (%s)" % note if note else ""))
            except Exception:
                failed += 1
                lines.append("FAIL  %s\n%s" % (test.__name__, traceback.format_exc()))
        harness.plugin.unload()
        lines.append("PASS  plugin unload")
        lines.append("clipboard-busy retries: %d" % harness.clipboard_retries)
    except Exception:
        failed += 1
        lines.append("FAIL  harness\n%s" % traceback.format_exc())
    lines.append("%d test(s), %d failed" % (len(TESTS), failed))
    with open(REPORT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    QgsProject.instance().setDirty(False)
    os._exit(0 if not failed else 1)


QTimer.singleShot(2500, main)
