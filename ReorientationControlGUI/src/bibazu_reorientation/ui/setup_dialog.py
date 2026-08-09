from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
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
        self.mesh = QLineEdit()
        self.profile = QLineEdit()
        self.target_pose = QComboBox()
        self.target_pose.addItem("Pose 1", 1)
        self.target_pose.addItem("Pose 2", 2)
        self.target_pose.currentIndexChanged.connect(self._update_transition_label)
        self.profile_label = QLabel("Aktuierungsprofil Pose 2 → Pose 1")
        form = QFormLayout()
        form.addRow("Bauteilname", self.name)
        form.addRow("YOLO-Modell (.pt)", self._path_row(self.model, "YOLO-Modell (*.pt)"))
        form.addRow(
            "3D-Modell in Zielorientierung",
            self._path_row(
                self.mesh,
                "3D-Modell (*.stl *.STL *.obj *.OBJ)",
                self._workpiece_directory(),
            ),
        )
        form.addRow("Zielpose", self.target_pose)
        self.profile_row = self._path_row(self.profile, "Pressure-Profil (*.json)")
        form.addRow(self.profile_label, self.profile_row)
        self.form = form
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @staticmethod
    def _workpiece_directory() -> str:
        workspace = Path(__file__).resolve().parents[5]
        candidate = workspace / "bibazu_geometry_to_pose" / "Werkstücke_STL_grob"
        return str(candidate) if candidate.is_dir() else ""

    def _path_row(self, edit: QLineEdit, file_filter: str, start: str = ""):
        row = QHBoxLayout()
        row.addWidget(edit)
        button = QPushButton("…")
        button.clicked.connect(lambda: self._browse(edit, file_filter, start))
        row.addWidget(button)
        return row

    def _browse(self, edit: QLineEdit, file_filter: str, start: str = "") -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Datei auswählen", start, file_filter)
        if path:
            edit.setText(path)

    def _update_transition_label(self) -> None:
        target = int(self.target_pose.currentData())
        self.profile_label.setText(f"Aktuierungsprofil Pose {3 - target} → Pose {target}")

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
            target_pose=int(self.target_pose.currentData()),
            mesh_path=Path(self.mesh.text()) if self.mesh.text().strip() else None,
        )
