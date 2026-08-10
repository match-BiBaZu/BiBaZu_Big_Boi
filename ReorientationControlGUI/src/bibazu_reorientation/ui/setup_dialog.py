from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.models import PartDefinition
from bibazu_reorientation.roadmap import load_stable_pose_roadmap
from bibazu_reorientation.ui.roadmap_pose_dialog import RoadmapPoseDialog


class SetupDialog(QDialog):
    def __init__(self, parent=None, definition: PartDefinition | None = None) -> None:
        super().__init__(parent)
        self.definition = definition
        self._roadmap_path = definition.roadmap_path if definition is not None else None
        self._target_roadmap_pose_id = (
            definition.target_roadmap_pose_id if definition is not None else None
        )
        self.setWindowTitle(
            "Edit part configuration" if definition is not None else "New part configuration"
        )
        self.name = QLineEdit()
        self.model = QLineEdit()
        self.mesh = QLineEdit()
        self.profile = QLineEdit()
        self.target_pose = QComboBox()
        self.target_pose.addItem("Pose 1", 1)
        self.target_pose.addItem("Pose 2", 2)
        self.target_pose.currentIndexChanged.connect(self._update_transition_label)
        self.profile_label = QLabel("Actuation profile Pose 2 → Pose 1")
        self.roadmap_pose_label = QLabel()
        self.roadmap_pose_label.setWordWrap(True)
        self.roadmap_pose_button = QPushButton("Select stable pose …")
        self.roadmap_pose_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.roadmap_pose_button.setStyleSheet(
            "QPushButton {background:#1677c8;color:white;font-weight:600;"
            "padding:6px 10px;border-radius:4px;}"
            "QPushButton:hover {background:#075b9b;}"
        )
        self.roadmap_pose_button.clicked.connect(self._open_or_choose_roadmap)
        self.change_roadmap_button = QPushButton("Choose another roadmap …")
        self.change_roadmap_button.clicked.connect(self._choose_roadmap_file)
        form = QFormLayout()
        form.addRow("Part name", self.name)
        form.addRow("YOLO model (.pt)", self._path_row(self.model, "YOLO model (*.pt)"))
        form.addRow(
            "3D model in target orientation",
            self._path_row(
                self.mesh,
                "3D model (*.stl *.STL *.obj *.OBJ)",
                self._workpiece_directory(),
            ),
        )
        target_row = QHBoxLayout()
        target_row.addWidget(self.target_pose)
        target_row.addWidget(self.roadmap_pose_button)
        form.addRow("Target pose", target_row)
        roadmap_row = QHBoxLayout()
        roadmap_row.addWidget(self.roadmap_pose_label, 1)
        roadmap_row.addWidget(self.change_roadmap_button)
        form.addRow("Physical roadmap pose", roadmap_row)
        self.profile_row = self._path_row(self.profile, "Pressure profile (*.json)")
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
        if definition is not None:
            self._populate(definition)
        self._update_roadmap_pose_label()

    def _populate(self, definition: PartDefinition) -> None:
        self.name.setText(definition.part_name)
        self.model.setText(str(definition.model_path))
        self.mesh.setText(str(definition.mesh_path or ""))
        self.profile.setText(str(definition.transitions[0].pressure_profile))
        index = self.target_pose.findData(definition.target_pose)
        if index >= 0:
            self.target_pose.setCurrentIndex(index)
        self._update_transition_label()

    @staticmethod
    def _workpiece_directory() -> str:
        workspace = Path(__file__).resolve().parents[5]
        candidate = workspace / "bibazu_geometry_to_pose" / "Werkstücke_STL_grob"
        return str(candidate) if candidate.is_dir() else ""

    @staticmethod
    def _roadmap_directory() -> str:
        workspace = Path(__file__).resolve().parents[5]
        candidate = workspace / "bibazu_geometry_to_pose" / "Poses_Found_Robust"
        return str(candidate) if candidate.is_dir() else ""

    def _path_row(self, edit: QLineEdit, file_filter: str, start: str = ""):
        row = QHBoxLayout()
        row.addWidget(edit)
        button = QPushButton("…")
        button.clicked.connect(lambda: self._browse(edit, file_filter, start))
        row.addWidget(button)
        return row

    def _browse(self, edit: QLineEdit, file_filter: str, start: str = "") -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file", start, file_filter)
        if path:
            edit.setText(path)

    def _update_transition_label(self) -> None:
        target = int(self.target_pose.currentData())
        self.profile_label.setText(f"Actuation profile Pose {3 - target} → Pose {target}")
        self._update_roadmap_pose_label()

    def _open_or_choose_roadmap(self) -> None:
        if self._roadmap_path is None:
            self._choose_roadmap_file()
            return
        self._show_roadmap_pose_dialog(self._roadmap_path)

    def _choose_roadmap_file(self) -> None:
        start = (
            str(self._roadmap_path.parent)
            if self._roadmap_path is not None
            else self._roadmap_directory()
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select pose roadmap",
            start,
            "Pose roadmap (*_roadmap.json *.json)",
        )
        if path:
            self._show_roadmap_pose_dialog(Path(path))

    def _show_roadmap_pose_dialog(self, path: Path) -> None:
        try:
            roadmap = load_stable_pose_roadmap(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Pose roadmap", str(exc))
            return
        dialog = RoadmapPoseDialog(roadmap, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.selected_pose is None:
            return
        self._roadmap_path = roadmap.path
        self._target_roadmap_pose_id = dialog.selected_pose.pose_id
        self._update_roadmap_pose_label()

    def _update_roadmap_pose_label(self) -> None:
        selected = self._target_roadmap_pose_id
        self.change_roadmap_button.setVisible(self._roadmap_path is not None)
        if selected is None:
            self.roadmap_pose_label.setText("No physical target pose selected yet")
            self.roadmap_pose_label.setStyleSheet("color:#6b7280;")
            return
        target_class = int(self.target_pose.currentData())
        self.roadmap_pose_label.setText(
            f"<b>Roadmap pose {selected}</b> · assigned to YOLO target class Pose {target_class}"
        )
        self.roadmap_pose_label.setStyleSheet(
            "background:#e7f5ff;color:#0b4f7a;border-left:4px solid #1677c8;padding:5px;"
        )

    def create(self) -> PartDefinition | None:
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        suggested_path = (
            str(self.definition.source_path)
            if self.definition is not None and self.definition.source_path is not None
            else f"{self.name.text().strip()}.yaml"
        )
        target, _ = QFileDialog.getSaveFileName(
            self, "Save configuration", suggested_path, "YAML (*.yaml *.yml)"
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
            roadmap_path=self._roadmap_path,
            target_roadmap_pose_id=self._target_roadmap_pose_id,
        )
