# -*- coding: utf-8 -*-
"""QGIS plugin class: action, menu, shortcut and the orchestration of one paste."""

import os
import traceback

from qgis.core import QgsMessageLog, QgsVectorLayer
from qgis.gui import QgsProjectionSelectionDialog
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMessageBox, QProgressDialog

from . import compat, report, settings
from .applier import apply_plan
from .clipboard import ClipboardCanceled, ClipboardError, read_clipboard
from .model import ApplyError, PlanError, PlanOptions, StalePlanError
from .planner import Feedback, PlanCanceled, build_plan, check_layer_preconditions
from .preview_dialog import PreviewDialog

LOG_TAG = "Topological Paste"
ACTION_TEXT = "Topological Paste"
# Registered through QGIS, so it can be changed in Settings > Keyboard Shortcuts.
# Ctrl+V is deliberately left alone (normal paste, text pasting in forms).
DEFAULT_SHORTCUT = "Ctrl+Shift+Alt+V"


class _ProgressFeedback(Feedback):
    """Progress + cancellation through an application-modal dialog.

    QProgressDialog.setValue() processes events only once the modal dialog is
    shown; being application modal it blocks input to the rest of QGIS, so the
    action cannot be re-triggered and the target cannot be edited meanwhile.
    No other processEvents() call exists in the plugin.
    """

    def __init__(self, parent):
        self.dialog = QProgressDialog("Planning topological paste...", "Cancel", 0, 100, parent)
        self.dialog.setWindowTitle(ACTION_TEXT)
        self.dialog.setWindowModality(compat.APPLICATION_MODAL)
        self.dialog.setMinimumDuration(700)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.setValue(0)

    def progress(self, fraction, text=None):
        if text:
            self.dialog.setLabelText(text)
        self.dialog.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def canceled(self):
        return self.dialog.wasCanceled()

    def close(self):
        self.dialog.reset()
        self.dialog.hide()
        self.dialog.deleteLater()


class TopologicalPastePlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.settings_action = None
        self._running = False
        self._in_edit_menu = False
        # test hook: callable(dialog) -> outcome; replaces the modal exec of the preview
        self.preview_runner = None

    # -- QGIS plugin interface -----------------------------------------------------------------
    def initGui(self):  # noqa: N802
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        main_window = self.iface.mainWindow()
        self.action = compat.QAction(icon, ACTION_TEXT, main_window)
        self.action.setObjectName("mActionTopologicalPaste")
        tip = (
            "Topological Paste: paste the clipboard features into the active layer; pasted coverage replaces "
            "what lies underneath, without duplicates, as one undoable edit"
        )
        self.action.setToolTip(tip)
        self.action.setStatusTip(tip)
        self.action.triggered.connect(self.run)
        self.iface.registerMainWindowAction(self.action, DEFAULT_SHORTCUT)
        self.iface.addToolBarIcon(self.action)

        # Edit menu, directly below "Paste Features"; plugin (Vector) menu as the fallback.
        try:
            menu = self.iface.editMenu()
            actions = menu.actions()
            paste = self.iface.actionPasteFeatures()
            position = actions.index(paste) + 1 if paste in actions else len(actions)
            if position < len(actions):
                menu.insertAction(actions[position], self.action)
            else:
                menu.addAction(self.action)
            self._in_edit_menu = True
        except Exception:
            self._in_edit_menu = False
        self.iface.addPluginToVectorMenu(ACTION_TEXT, self.action)

        self.settings_action = compat.QAction("Topological Paste settings...", main_window)
        self.settings_action.triggered.connect(self.show_settings)
        self.iface.addPluginToVectorMenu(ACTION_TEXT, self.settings_action)

        self.iface.currentLayerChanged.connect(self._update_enabled)
        self._update_enabled()

    def unload(self):
        try:
            self.iface.currentLayerChanged.disconnect(self._update_enabled)
        except (TypeError, RuntimeError):
            pass
        if self.action is not None:
            if self._in_edit_menu:
                try:
                    self.iface.editMenu().removeAction(self.action)
                except Exception:
                    pass
            self.iface.removePluginVectorMenu(ACTION_TEXT, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.iface.unregisterMainWindowAction(self.action)
            self.action.deleteLater()
            self.action = None
        if self.settings_action is not None:
            self.iface.removePluginVectorMenu(ACTION_TEXT, self.settings_action)
            self.settings_action.deleteLater()
            self.settings_action = None

    def _update_enabled(self, *_args):
        if self.action is None:
            return
        layer = self.iface.activeLayer()
        self.action.setEnabled(
            not self._running and isinstance(layer, QgsVectorLayer) and layer.isSpatial()
        )

    # -- messaging ---------------------------------------------------------------------------------
    def _log(self, text, level=None):
        QgsMessageLog.logMessage(text, LOG_TAG, compat.MSG_INFO if level is None else level)

    def _message(self, text, level, duration=8):
        self.iface.messageBar().pushMessage(ACTION_TEXT, text, level, duration)
        self._log(text, level)

    # -- settings ---------------------------------------------------------------------------------------
    def show_settings(self):
        from .settings_dialog import SettingsDialog

        compat.exec_dialog(SettingsDialog(self.iface.mainWindow()))

    # -- the action ---------------------------------------------------------------------------------------
    def run(self, *_args):
        if self._running:  # re-entrancy guard (shortcut auto-repeat, nested event loops)
            return
        self._running = True
        self._update_enabled()
        try:
            self._run()
        except Exception:
            self._log(traceback.format_exc(), compat.MSG_CRITICAL)
            self._message("Unexpected error - nothing was changed. See the log for details.", compat.MSG_CRITICAL)
        finally:
            self._running = False
            self._update_enabled()

    def _ask_crs(self, snapshot):
        dialog = QgsProjectionSelectionDialog(self.iface.mainWindow())
        dialog.setWindowTitle("CRS of the clipboard data")
        try:
            dialog.setMessage(
                "The clipboard holds %s without a coordinate reference system. "
                "Choose the CRS the coordinates are in. It is never assumed from the layer or the map."
                % snapshot.origin
            )
        except Exception:
            pass
        if not compat.exec_dialog(dialog):
            return None
        crs = dialog.crs()
        return crs if crs.isValid() else None

    def _plan(self, layer, snapshot, options):
        feedback = _ProgressFeedback(self.iface.mainWindow())
        try:
            return build_plan(layer, snapshot, options, feedback)
        finally:
            feedback.close()

    def _run(self):
        # The destination is captured NOW. The staging step temporarily activates
        # a scratch layer; that must never redirect the paste.
        layer = self.iface.activeLayer()
        try:
            check_layer_preconditions(layer)
        except PlanError as exc:
            self._message(str(exc), compat.MSG_WARNING, 12)
            return
        layer_id = layer.id()

        try:
            snapshot = read_clipboard(self.iface, self._ask_crs)
        except ClipboardCanceled:
            self._message("Cancelled - nothing was changed.", compat.MSG_INFO, 4)
            return
        except ClipboardError as exc:
            self._message(str(exc), compat.MSG_WARNING)
            return

        options = PlanOptions(settings.grid_size(), None)
        while True:
            try:
                plan = self._plan(layer, snapshot, options)
            except PlanCanceled:
                self._message("Cancelled - nothing was changed.", compat.MSG_INFO, 4)
                return
            except PlanError as exc:
                self._message("%s Nothing was changed." % exc, compat.MSG_WARNING, 12)
                return
            self._log("Plan for layer '%s':\n%s" % (layer.name(), report.details_text(plan)))

            if plan.is_noop() and not plan.blocking():
                if plan.reused:
                    layer.selectByIds(sorted(plan.reused.keys()))
                self._message(
                    "Nothing to do: the clipboard features already exist in '%s' with identical geometry and "
                    "attributes (%d feature(s)). The layer was not modified." % (layer.name(), len(plan.reused)),
                    compat.MSG_INFO,
                )
                return

            trivial = (
                settings.get_bool("skip_preview_when_only_adding")
                and not plan.modifies_existing()
                and not [i for i in plan.issues if i.severity != "info"]
            )
            if trivial:
                outcome = PreviewDialog.APPLY
            else:
                dialog = PreviewDialog(self.iface, layer, plan)
                if self.preview_runner is not None:
                    outcome = self.preview_runner(dialog)
                    dialog.done(0)
                else:
                    compat.exec_dialog(dialog)
                    outcome = dialog.outcome
                if outcome == PreviewDialog.REPLAN:
                    options = dialog.new_options or options
                    dialog.deleteLater()
                    continue
                dialog.deleteLater()
            if outcome != PreviewDialog.APPLY:
                self._message("Cancelled - nothing was changed.", compat.MSG_INFO, 4)
                return

            try:
                if layer_id != layer.id():
                    raise StalePlanError("The target layer changed.")
                result = apply_plan(layer, plan)
            except RuntimeError:
                self._message("The target layer no longer exists. Nothing was changed.", compat.MSG_WARNING)
                return
            except StalePlanError as exc:
                answer = QMessageBox.question(
                    self.iface.mainWindow(),
                    ACTION_TEXT,
                    "%s\n\nNothing was changed. Recalculate the preview?" % exc,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    continue
                return
            except ApplyError as exc:
                self._message(
                    "%s The edit command was removed; the layer is exactly as before." % exc, compat.MSG_CRITICAL, 0
                )
                return

            layer.selectByIds(result.output_fids())
            layer.triggerRepaint()
            self._message(
                "%s into '%s' (one Undo step; edits are not saved yet)." % (report.result_message(plan, result), layer.name()),
                compat.MSG_SUCCESS,
            )
            return
