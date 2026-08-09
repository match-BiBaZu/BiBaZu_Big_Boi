from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.models import PartDefinition


class SetupDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Neue Bauteilkonfiguration")
        self.name = QLineEdit()
        self.model = QLineEdit()
        self.profile = QLineEdit()
        form = QFormLayout()
        form.addRow("Bauteilname", self.name)
        form.addRow("YOLO-Modell (.pt)", self._path_row(self.model, "YOLO-Modell (*.pt)"))
        form.addRow("Profil Pose 2 → Pose 1", self._path_row(self.profile, "JSON (*.json)"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _path_row(self, edit: QLineEdit, file_filter: str):
        row = QHBoxLayout()
        row.addWidget(edit)
        button = QPushButton("…")
        button.clicked.connect(lambda: self._browse(edit, file_filter))
        row.addWidget(button)
        return row

    def _browse(self, edit: QLineEdit, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Datei auswählen", "", file_filter)
        if path:
            edit.setText(path)

    def create(self) -> PartDefinition | None:
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        target, _ = QFileDialog.getSaveFileName(
            self, "Konfiguration speichern", f"{self.name.text().strip()}.yaml", "YAML (*.yaml)"
        )
        if not target:
            return None
        return save_part_definition(
            Path(target),
            part_name=self.name.text(),
            model_path=Path(self.model.text()),
            pressure_profile=Path(self.profile.text()),
        )
