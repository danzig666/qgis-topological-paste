# -*- coding: utf-8 -*-
"""Compact preview shown before anything is changed.

The dialog only *reads* the plan. Highlighting uses rubber bands on the map
canvas - no preview layer is ever added to the project - and they are removed
when the dialog closes, whatever the outcome.
"""

from qgis.core import QgsRectangle

try:
    from qgis.core import QgsUnitTypes
except ImportError:  # pragma: no cover - future API removal
    QgsUnitTypes = None
from qgis.gui import QgsCollapsibleGroupBox, QgsRubberBand
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import compat, report, settings
from .model import PlanOptions

MAX_HIGHLIGHTS = 1500


class FieldMappingDialog(QDialog):
    """Explicit source -> target field mapping. Nothing is guessed: unmatched fields start as 'not pasted'."""

    NOT_PASTED = "(not pasted)"

    def __init__(self, plan, target_field_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Topological Paste - field mapping")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the target field for every clipboard field. Fields are never created."))
        source_names = plan.snapshot.field_names
        current = dict((source, target.name) for source, target in plan.mapping.pairs)
        self.table = QTableWidget(len(source_names), 2, self)
        self.table.setHorizontalHeaderLabels(["Clipboard field", "Target field"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self._combos = []
        for row, name in enumerate(source_names):
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~compat.ITEM_IS_EDITABLE)
            self.table.setItem(row, 0, item)
            combo = QComboBox(self.table)
            combo.addItem(self.NOT_PASTED)
            combo.addItems(target_field_names)
            if name in current:
                combo.setCurrentIndex(combo.findText(current[name]))
            self.table.setCellWidget(row, 1, combo)
            self._combos.append((name, combo))
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(460, 380)

    def override(self):
        result = {}
        for name, combo in self._combos:
            text = combo.currentText()
            result[name] = None if text == self.NOT_PASTED else text
        return result


class PreviewDialog(QDialog):
    APPLY, CANCEL, REPLAN = "apply", "cancel", "replan"

    def __init__(self, iface, layer, plan, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.layer = layer
        self.plan = plan
        self.outcome = self.CANCEL
        self.new_options = None
        self._bands = []
        self.setWindowTitle("Topological Paste - preview")

        layout = QVBoxLayout(self)
        title = QLabel("Paste into layer: <b>%s</b>" % _escape(layer.name()))
        layout.addWidget(title)
        source = QLabel("Source: %s → layer CRS %s" % (report.source_line(plan), layer.crs().authid() or "custom"))
        source.setTextFormat(compat.PLAIN_TEXT)
        source.setWordWrap(True)
        layout.addWidget(source)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(2)
        bold = QFont()
        bold.setBold(True)
        for row, (label, value) in enumerate(report.summary_rows(plan)):
            grid.addWidget(QLabel(label), row, 0)
            number = QLabel(str(value))
            if value:
                number.setFont(bold)
            grid.addWidget(number, row, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)

        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setPlainText(report.details_text(plan))
        layout.addWidget(self.details, 1)

        self.accept_box = QCheckBox("I have read the items that need acceptance and accept them", self)
        self.accept_box.setVisible(bool(plan.needs_acceptance()) and not plan.blocking())
        self.accept_box.toggled.connect(self._update_apply_enabled)
        layout.addWidget(self.accept_box)

        tools = QHBoxLayout()
        self.highlight_box = QCheckBox("Highlight on map", self)
        self.highlight_box.setToolTip("green: pasted coverage, orange: trimmed remainders, red: deleted features")
        self.highlight_box.setChecked(settings.get_bool("highlight_preview"))
        self.highlight_box.toggled.connect(self._update_highlight)
        tools.addWidget(self.highlight_box)
        zoom = QPushButton("Zoom to affected area", self)
        zoom.clicked.connect(self._zoom)
        tools.addWidget(zoom)
        mapping = QPushButton("Field mapping...", self)
        mapping.setEnabled(bool(plan.snapshot.field_names))
        mapping.clicked.connect(self._edit_mapping)
        tools.addWidget(mapping)
        tools.addStretch(1)
        layout.addLayout(tools)

        layout.addWidget(self._build_advanced())

        self.buttons = QDialogButtonBox(self)
        self.apply_button = self.buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.resize(640, 620)
        self._update_apply_enabled()
        self._update_highlight()

    # -- advanced: precision grid -------------------------------------------------------------
    def _build_advanced(self):
        box = QgsCollapsibleGroupBox("Advanced: precision grid (off = coordinates are preserved)", self)
        box.setCollapsed(self.plan.options.grid_size <= 0)
        layout = QVBoxLayout(box)
        try:
            units = QgsUnitTypes.toString(self.layer.crs().mapUnits())
        except Exception:
            units = "map units"
        row = QHBoxLayout()
        self.grid_box = QCheckBox("Snap all involved geometries to a grid of", box)
        self.grid_box.setChecked(self.plan.options.grid_size > 0)
        row.addWidget(self.grid_box)
        self.grid_spin = QDoubleSpinBox(box)
        self.grid_spin.setDecimals(9)
        self.grid_spin.setRange(0.000000001, 1000000.0)
        self.grid_spin.setValue(
            self.plan.options.grid_size if self.plan.options.grid_size > 0 else settings.get_float("precision_grid_size")
        )
        row.addWidget(self.grid_spin)
        self.grid_box.toggled.connect(lambda _checked: self._update_apply_enabled())
        self.grid_spin.valueChanged.connect(lambda _value: self._update_apply_enabled())
        row.addWidget(QLabel(units + " (layer CRS units)", box))
        row.addStretch(1)
        layout.addLayout(row)
        note = QLabel(
            "Warning: this CHANGES COORDINATES. The pasted features, the trimmed features and their touching "
            "neighbours are all snapped to the same grid, and the result is validated again. "
            "It never deletes small features. Leave it off to keep every coordinate exactly as it is.",
            box,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        recalc = QPushButton("Recalculate preview", box)
        recalc.clicked.connect(self._replan_with_grid)
        button_row = QHBoxLayout()
        button_row.addWidget(recalc)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        return box

    def _current_grid(self):
        return self.grid_spin.value() if self.grid_box.isChecked() else 0.0

    def _replan_with_grid(self):
        settings.set_value("precision_grid_enabled", self.grid_box.isChecked())
        settings.set_value("precision_grid_size", self.grid_spin.value())
        self.new_options = PlanOptions(self._current_grid(), self.plan.options.mapping_override)
        self._finish(self.REPLAN)

    def _edit_mapping(self):
        from .attributes import describe_target_fields

        targets = [t.name for t in describe_target_fields(self.layer) if t.writable]
        dialog = FieldMappingDialog(self.plan, targets, self)
        if compat.exec_dialog(dialog):
            self.new_options = PlanOptions(self.plan.options.grid_size, dialog.override())
            self._finish(self.REPLAN)

    # -- outcome ---------------------------------------------------------------------------------------
    def _update_apply_enabled(self):
        ok = not self.plan.blocking() and not self.plan.is_noop()
        if ok and self.plan.needs_acceptance():
            ok = self.accept_box.isChecked()
        if ok and self._current_grid() != self.plan.options.grid_size:
            ok = False  # grid setting edited but the preview was not recalculated
        self.apply_button.setEnabled(ok)

    def _apply(self):
        self._update_apply_enabled()
        if self.apply_button.isEnabled():
            self._finish(self.APPLY)

    def _finish(self, outcome):
        self.outcome = outcome
        settings.set_value("highlight_preview", self.highlight_box.isChecked())
        self.accept()

    def done(self, result):
        self._clear_highlight()
        super().done(result)

    # -- highlighting ----------------------------------------------------------------------------------------
    def _clear_highlight(self):
        canvas = self.iface.mapCanvas()
        for band in self._bands:
            try:
                canvas.scene().removeItem(band)
            except Exception:
                pass
        self._bands = []

    def _band(self, fill, outline, width):
        band = QgsRubberBand(self.iface.mapCanvas(), self.layer.geometryType())
        band.setFillColor(fill)
        band.setStrokeColor(outline)
        band.setWidth(width)
        self._bands.append(band)
        return band

    def _update_highlight(self):
        self._clear_highlight()
        if not self.highlight_box.isChecked():
            return
        budget = [MAX_HIGHLIGHTS]

        def add(band, geometry):
            if budget[0] > 0 and geometry is not None and not geometry.isNull():
                band.addGeometry(geometry, self.layer)
                budget[0] -= 1

        deleted = self._band(QColor(215, 25, 28, 90), QColor(215, 25, 28), 2)
        trimmed = self._band(QColor(253, 174, 97, 30), QColor(230, 120, 0), 2)
        pasted = self._band(QColor(26, 150, 65, 80), QColor(0, 110, 40), 3)
        for change in self.plan.changes.values():
            if change.delete:
                add(deleted, change.original_geometry)
            elif change.new_geometry is not None:
                add(trimmed, change.new_geometry)
        for feature in self.plan.new_features:
            add(pasted if feature.kind == "incoming" else trimmed, feature.geometry)
        for fid in self.plan.reused:
            change = self.plan.changes.get(fid)
            if change is None:
                feature = self.layer.getFeature(fid)
                if feature.isValid():
                    add(pasted, feature.geometry())

    def _zoom(self):
        rect = QgsRectangle(self.plan.affected_rect)
        for change in self.plan.changes.values():
            rect.combineExtentWith(change.original_geometry.boundingBox())
        if rect.isNull():
            return
        canvas = self.iface.mapCanvas()
        extent = canvas.mapSettings().layerExtentToOutputExtent(self.layer, rect)
        extent.scale(1.25)
        canvas.setExtent(extent)
        canvas.refresh()


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
