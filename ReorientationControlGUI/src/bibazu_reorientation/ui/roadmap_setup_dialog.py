from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.config import save_roadmap_part_definition
from bibazu_reorientation.models import PartDefinition
from bibazu_reorientation.profiles import load_pressure_profile
from bibazu_reorientation.roadmap import PoseRoadmap, RoadmapTransition, load_pose_roadmap
from bibazu_reorientation.ui.roadmap_pose_dialog import RoadmapPoseDialog


class RoadmapSetupDialog(QDialog):
    """Create/edit schema-v2 roadmap configurations without enabling execution."""

    def __init__(self, parent=None, definition: PartDefinition | None = None) -> None:
        super().__init__(parent)
        self.definition = definition
        self.roadmap: PoseRoadmap | None = None
        self.target_pose_id: int | None = None
        self.class_inputs: dict[int, QSpinBox] = {}
        self.profile_inputs: dict[str, QLineEdit] = {}
        self.setWindowTitle(
            "Edit roadmap configuration" if definition else "New roadmap configuration"
        )
        self.resize(1280, 850)

        self.roadmap_path = QLineEdit()
        self.roadmap_path.setReadOnly(True)
        choose = QPushButton("Select roadmap …")
        choose.clicked.connect(self._choose_roadmap)
        roadmap_row = QHBoxLayout()
        roadmap_row.addWidget(self.roadmap_path, 1)
        roadmap_row.addWidget(choose)

        self.summary = QLabel("Please select a roadmap (.yaml, .yml, or .json) first.")
        self.summary.setWordWrap(True)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.name = QLineEdit()
        self.mesh = QLineEdit()
        self.model = QLineEdit()
        self.target = QLabel("Not selected yet")
        target_button = QPushButton("Select target pose from image cards …")
        target_button.clicked.connect(self._choose_target)
        target_row = QHBoxLayout()
        target_row.addWidget(self.target, 1)
        target_row.addWidget(target_button)

        form = QFormLayout()
        form.addRow("1. Roadmap", roadmap_row)
        form.addRow("Part name", self.name)
        form.addRow("CAD model", self._path_row(self.mesh, "3D model (*.stl *.STL *.obj *.OBJ)"))
        form.addRow("YOLO model", self._path_row(self.model, "YOLO model (*.pt)"))
        form.addRow("Target pose", target_row)

        self.mapping_table = QTableWidget(0, 4)
        self.mapping_table.setHorizontalHeaderLabels(
            ("Roadmap pose", "Stability", "Contacts", "YOLO class ID")
        )
        self.mapping_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.profile_table = QTableWidget(0, 8)
        self.profile_table.setHorizontalHeaderLabels(
            (
                "Direction",
                "Action",
                "Target angle",
                "Geometric score",
                "Experiment",
                "Pressure profile",
                "Status",
                "Selection",
            )
        )
        self.profile_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.profile_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self.profile_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.info_table = QTableWidget(0, 4)
        self.info_table.setHorizontalHeaderLabels(("Direction", "Type", "Action", "Note"))
        self.info_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.info_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.readiness = QLabel("Draft is not complete yet")
        self.readiness.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(self.warning)
        layout.addWidget(
            self._group("Explicit YOLO class mapping (robust poses only)", self.mapping_table),
            1,
        )
        layout.addWidget(
            self._group(
                "Actuated robust-to-robust transitions (profiles optional)", self.profile_table
            ),
            2,
        )
        layout.addWidget(
            self._group("Passive and metastable transitions (information only)", self.info_table),
            1,
        )
        layout.addWidget(self._group("Readiness", self.readiness))
        layout.addWidget(buttons)

        for edit in (self.name, self.mesh, self.model):
            edit.textChanged.connect(self._update_readiness)
        if definition is not None and definition.roadmap_path is not None:
            self._load_roadmap(definition.roadmap_path, definition)

    @staticmethod
    def _group(title: str, child: QWidget) -> QGroupBox:
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        layout.addWidget(child)
        return group

    def _path_row(self, edit: QLineEdit, file_filter: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("…")
        button.clicked.connect(lambda: self._browse(edit, file_filter))
        row.addWidget(button)
        return row

    def _browse(self, edit: QLineEdit, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file", edit.text(), file_filter)
        if path:
            edit.setText(path)

    @staticmethod
    def _roadmap_directory() -> str:
        workspace = Path(__file__).resolve().parents[5]
        candidate = workspace / "bibazu_geometry_to_pose" / "Poses_Found_Robust"
        return str(candidate) if candidate.is_dir() else ""

    def _choose_roadmap(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select pose roadmap",
            self._roadmap_directory(),
            "Pose-Roadmap (*.yaml *.yml *.json)",
        )
        if not path:
            return
        try:
            self._load_roadmap(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Pose roadmap", str(exc))

    def _load_roadmap(self, path: Path, definition: PartDefinition | None = None) -> None:
        roadmap = load_pose_roadmap(path)
        self.roadmap = roadmap
        self.roadmap_path.setText(str(roadmap.path))
        self.name.setText(definition.part_name if definition else roadmap.part_name)
        self.mesh.setText(
            str(
                definition.mesh_path
                if definition and definition.mesh_path
                else roadmap.mesh_path or ""
            )
        )
        self.model.setText(str(definition.model_path) if definition else "")
        self.target_pose_id = (
            definition.target_pose
            if definition
            and definition.target_pose in {pose.pose_id for pose in roadmap.robust_poses}
            else None
        )
        self._populate_mapping(definition)
        self._populate_profiles(definition)
        self._populate_information()
        self._update_target_text()
        self.summary.setText(
            f"<b>{roadmap.part_name}</b> · {len(roadmap.poses)} poses "
            f"({len(roadmap.robust_poses)} robust) · {len(roadmap.transitions)} "
            f"directed edges · {len(roadmap.profile_transitions)} profile-eligible edges"
        )
        warnings: list[str] = []
        if roadmap.cad_status == "provisional":
            warnings.append("CAD status: provisional")
        if any(edge.experimental_status == "untested" for edge in roadmap.transitions):
            warnings.append("Experimental status: untested")
        warnings.append("Geometric scores are not success probabilities.")
        if definition is not None and definition.roadmap_changed:
            warnings.insert(
                0,
                "Roadmap re-imported: only pose/edge IDs that still exist were preserved.",
            )
            changes = (
                ("new poses", definition.roadmap_added_pose_ids),
                ("removed poses", definition.roadmap_removed_pose_ids),
                ("new edges", definition.roadmap_added_edge_ids),
                ("removed edges", definition.roadmap_removed_edge_ids),
            )
            warnings.extend(
                f"{label}: {', '.join(map(str, values))}" for label, values in changes if values
            )
        self.warning.setText(" ⚠ ".join(warnings))
        self.warning.setStyleSheet(
            "background:#fff3cd;color:#664d03;padding:7px;border:1px solid #ffecb5;"
        )
        self._update_readiness()

    def _populate_mapping(self, definition: PartDefinition | None) -> None:
        assert self.roadmap is not None
        previous = {pose.id: pose.model_class_id for pose in definition.poses} if definition else {}
        used = set(previous.values())
        self.class_inputs.clear()
        self.mapping_table.setRowCount(len(self.roadmap.robust_poses))
        for row, pose in enumerate(self.roadmap.robust_poses):
            self.mapping_table.setItem(row, 0, QTableWidgetItem(str(pose.pose_id)))
            self.mapping_table.setItem(row, 1, QTableWidgetItem(pose.stability))
            self.mapping_table.setItem(
                row, 2, QTableWidgetItem(f"Floor: {pose.floor_contact}; wall: {pose.wall_contact}")
            )
            spin = QSpinBox()
            spin.setRange(-1, 9999)
            spin.setSpecialValueText("unassigned")
            if pose.pose_id in previous:
                spin.setValue(previous[pose.pose_id])
            elif definition is not None:
                spin.setValue(-1)
            else:
                suggestion = 0
                while suggestion in used:
                    suggestion += 1
                spin.setValue(suggestion)
                used.add(suggestion)
            spin.valueChanged.connect(self._update_readiness)
            self.mapping_table.setCellWidget(row, 3, spin)
            self.class_inputs[pose.pose_id] = spin

    def _populate_profiles(self, definition: PartDefinition | None) -> None:
        assert self.roadmap is not None
        previous = (
            {edge.edge_id: edge.pressure_profile for edge in definition.transitions}
            if definition
            else {}
        )
        self.profile_inputs.clear()
        self.profile_table.setRowCount(len(self.roadmap.profile_transitions))
        for row, edge in enumerate(self.roadmap.profile_transitions):
            self.profile_table.setItem(
                row, 0, QTableWidgetItem(f"{edge.from_pose} → {edge.to_pose}")
            )
            self.profile_table.setItem(row, 1, QTableWidgetItem(edge.actuation))
            self.profile_table.setItem(
                row,
                2,
                QTableWidgetItem(
                    "–" if edge.signed_angle_deg is None else f"{edge.signed_angle_deg:.1f}°"
                ),
            )
            self.profile_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    "–" if edge.geometric_score is None else f"{edge.geometric_score:.3f}"
                ),
            )
            self.profile_table.setItem(row, 4, QTableWidgetItem(edge.experimental_status))
            edit = QLineEdit(str(previous.get(edge.edge_id) or ""))
            edit.setToolTip(edge.edge_id)
            edit.textChanged.connect(
                lambda _=None, e=edge, widget=edit, row=row: self._validate_profile(e, widget, row)
            )
            self.profile_table.setCellWidget(row, 5, edit)
            self.profile_table.setItem(row, 6, QTableWidgetItem("optional / missing"))
            choose = QPushButton("JSON …")
            choose.clicked.connect(
                lambda _=False, widget=edit: self._browse(widget, "Pressure profile (*.json)")
            )
            self.profile_table.setCellWidget(row, 7, choose)
            self.profile_inputs[edge.edge_id] = edit
            self._validate_profile(edge, edit, row)

    def _validate_profile(self, edge: RoadmapTransition, edit: QLineEdit, row: int) -> None:
        value = edit.text().strip()
        status = "optional / missing"
        if value:
            try:
                profile = load_pressure_profile(Path(value), require_transition=False)
                status = (
                    "valid (legacy; PLC baseline may be required)"
                    if profile.source_version < 8
                    else "valid"
                )
            except Exception as exc:
                status = f"invalid: {exc}"
        item = self.profile_table.item(row, 6)
        if item is not None:
            item.setText(status)
            item.setToolTip(status)
        self._update_readiness()

    def _populate_information(self) -> None:
        assert self.roadmap is not None
        edges = self.roadmap.informational_transitions
        self.info_table.setRowCount(len(edges))
        for row, edge in enumerate(edges):
            self.info_table.setItem(row, 0, QTableWidgetItem(f"{edge.from_pose} → {edge.to_pose}"))
            self.info_table.setItem(row, 1, QTableWidgetItem(edge.transition_kind))
            self.info_table.setItem(row, 2, QTableWidgetItem(edge.actuation))
            self.info_table.setItem(
                row, 3, QTableWidgetItem("Information only; no pressure profile")
            )

    def _choose_target(self) -> None:
        if self.roadmap is None:
            QMessageBox.information(self, "Target pose", "Please select a roadmap first.")
            return
        dialog = RoadmapPoseDialog(self.roadmap, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_pose is not None:
            self.target_pose_id = dialog.selected_pose.pose_id
            self._update_target_text()
            self._update_readiness()

    def _update_target_text(self) -> None:
        self.target.setText(
            "Not selected yet"
            if self.target_pose_id is None
            else f"Roadmap pose {self.target_pose_id}"
        )

    def _update_readiness(self, *_args) -> None:
        if self.roadmap is None:
            self.readiness.setText("Roadmap missing")
            return
        missing_mapping = [
            pose_id for pose_id, spin in self.class_inputs.items() if spin.value() < 0
        ]
        values = [spin.value() for spin in self.class_inputs.values() if spin.value() >= 0]
        duplicates = len(values) != len(set(values))
        missing_profiles = [
            edge_id for edge_id, edit in self.profile_inputs.items() if not edit.text().strip()
        ]
        available = {
            edge_id for edge_id, edit in self.profile_inputs.items() if edit.text().strip()
        }
        reachable = self._reachable_starts(available)
        parts = [
            f"Missing profiles: {len(missing_profiles)}",
            "Target reachable via assigned edges from: "
            f"{', '.join(map(str, reachable)) or 'target pose only'}",
        ]
        parts.append(
            "Class mapping complete"
            if not missing_mapping and not duplicates
            else f"Class mapping incomplete/ambiguous: {missing_mapping}"
        )
        parts.append("Roadmap hash will be recorded again when saving")
        if self.name.text().strip() != self.roadmap.part_name:
            parts.append("Part name intentionally differs from the roadmap")
        if (
            self.mesh.text().strip()
            and Path(self.mesh.text()).expanduser().resolve() != self.roadmap.mesh_path
        ):
            parts.append("CAD path intentionally differs from the roadmap")
        parts.append("Draft can be saved; multi-pose execution is not enabled yet")
        self.readiness.setText("<br>".join(f"• {part}" for part in parts))

    def _reachable_starts(self, available_edge_ids: set[str]) -> list[int]:
        if self.roadmap is None or self.target_pose_id is None:
            return []
        reachable = {self.target_pose_id}
        changed = True
        while changed:
            changed = False
            for edge in self.roadmap.profile_transitions:
                if (
                    edge.edge_id in available_edge_ids
                    and edge.to_pose in reachable
                    and edge.from_pose not in reachable
                ):
                    reachable.add(edge.from_pose)
                    changed = True
        return sorted(reachable)

    def accept(self) -> None:
        if self.roadmap is None:
            QMessageBox.warning(self, "Configuration", "Please select a roadmap first.")
            return
        values = [spin.value() for spin in self.class_inputs.values()]
        if any(value < 0 for value in values) or len(set(values)) != len(values):
            QMessageBox.warning(
                self, "Configuration", "Every robust pose requires a unique model class."
            )
            return
        if self.target_pose_id is None:
            QMessageBox.warning(self, "Configuration", "Please select a robust target pose.")
            return
        super().accept()

    def create(self) -> PartDefinition | None:
        if self.exec() != QDialog.DialogCode.Accepted or self.roadmap is None:
            return None
        suggested = (
            str(self.definition.source_path)
            if self.definition and self.definition.source_path
            else f"{self.name.text().strip() or self.roadmap.part_name}.yaml"
        )
        target, _ = QFileDialog.getSaveFileName(
            self, "Save roadmap configuration", suggested, "YAML (*.yaml *.yml)"
        )
        if not target:
            return None
        return save_roadmap_part_definition(
            Path(target),
            roadmap_path=self.roadmap.path,
            part_name=self.name.text(),
            mesh_path=Path(self.mesh.text()),
            model_path=Path(self.model.text()),
            pose_class_mapping={
                pose_id: spin.value() for pose_id, spin in self.class_inputs.items()
            },
            target_pose=int(self.target_pose_id),
            transition_profiles={
                edge_id: (Path(edit.text()) if edit.text().strip() else None)
                for edge_id, edit in self.profile_inputs.items()
            },
        )
