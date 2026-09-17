# -*- coding: utf-8 -*-
"""Small settings dialog; everything is optional - an ordinary paste needs none of it."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from . import settings


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Topological Paste - settings")
        layout = QVBoxLayout(self)

        self.skip_box = QCheckBox(
            "Skip the preview when the paste only adds new features and has no warnings", self
        )
        self.skip_box.setChecked(settings.get_bool("skip_preview_when_only_adding"))
        layout.addWidget(self.skip_box)

        self.highlight_box = QCheckBox("Highlight affected geometries on the map while previewing", self)
        self.highlight_box.setChecked(settings.get_bool("highlight_preview"))
        layout.addWidget(self.highlight_box)

        self.grid_box = QCheckBox("Advanced: use a precision grid (CHANGES COORDINATES)", self)
        self.grid_box.setChecked(settings.get_bool("precision_grid_enabled"))
        layout.addWidget(self.grid_box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Grid size, in the units of the target layer's CRS:", self))
        self.grid_spin = QDoubleSpinBox(self)
        self.grid_spin.setDecimals(9)
        self.grid_spin.setRange(0.000000001, 1000000.0)
        self.grid_spin.setValue(settings.get_float("precision_grid_size"))
        row.addWidget(self.grid_spin)
        layout.addLayout(row)
        note = QLabel(
            "With the grid off (default) no coordinate is ever snapped, rounded or buffered. With the grid on, "
            "pasted features, trimmed features and their touching neighbours are snapped to the same grid; "
            "the preview lists this and asks for your acceptance.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        settings.set_value("skip_preview_when_only_adding", self.skip_box.isChecked())
        settings.set_value("highlight_preview", self.highlight_box.isChecked())
        settings.set_value("precision_grid_enabled", self.grid_box.isChecked())
        settings.set_value("precision_grid_size", self.grid_spin.value())
        self.accept()
