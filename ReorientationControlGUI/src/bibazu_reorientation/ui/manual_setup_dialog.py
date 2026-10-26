from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.inference import validate_model_classes
from bibazu_reorientation.manual_config import save_manual_definition
from bibazu_reorientation.models import PartDefinition


class ModelClassReader(QThread):
    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, path: Path, parent=None) -> None:
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        try:
            if self.isInterruptionRequested():
                return
            from ultralytics import YOLO

            model = YOLO(str(self.path))
            names = validate_model_classes(model.names, ())
            if not self.isInterruptionRequested():
                self.loaded.emit(names)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class ManualSetupDialog(QDialog):
    """Assign complete pressure profiles directly to model classes, without a roadmap."""

    def __init__(self, parent=None, definition: PartDefinition | None = None) -> None:
        super().__init__(parent)
        self.definition = definition
        self._reader: ModelClassReader | None = None
        self._reject_pending = False
        self._updating_rows = False
        self.rows: list[tuple[QSpinBox, QLineEdit, QLineEdit]] = []
        self.setWindowTitle(
            "Edit manual configuration" if definition else "New manual configuration"
        )
        self.resize(920, 580)
        self.name = QLineEdit()
        self.model = QLineEdit()
        self.mesh = QLineEdit()
        self.target_class = QComboBox()
        self.target_class.currentIndexChanged.connect(self._update_target_rows)
        self.read_classes_button = QPushButton("Read YOLO classes")
        self.read_classes_button.clicked.connect(self.read_model_classes)
        self.status = QLabel("Choose a YOLO model and read its classes, or add classes manually.")
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Part name", self.name)
        form.addRow("YOLO model (.pt)", self._file_row(self.model, "YOLO model (*.pt)"))
        form.addRow(self.read_classes_button)
        form.addRow("3D model (optional)", self._file_row(self.mesh, "3D model (*.stl *.obj)"))
        form.addRow("Target class (pass through)", self.target_class)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ("YOLO class ID", "Start pose / class name", "Pressure profile")
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        add = QPushButton("Add start pose")
        add.clicked.connect(self.add_start_pose)
        remove = QPushButton("Remove selected pose")
        remove.clicked.connect(self.remove_start_pose)
        row_buttons = QHBoxLayout()
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        note = QLabel(
            "Assign one complete profile per start pose to reach the selected target. "
            "Empty profiles and unknown classes pass through without firing. "
            "Assignments are saved and reused for each production snapshot."
        )
        note.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._validate_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(self.table)
        layout.addLayout(row_buttons)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        if definition is not None:
            self.name.setText(definition.part_name)
            self.model.setText(str(definition.model_path))
            self.mesh.setText(str(definition.mesh_path or ""))
            by_pose = {edge.from_pose: edge.pressure_profile for edge in definition.transitions}
            for pose in definition.poses:
                self._add_row(pose.model_class_id, pose.label, by_pose.get(pose.id))
            target = next(
                p.model_class_id for p in definition.poses if p.id == definition.target_pose
            )
            self.target_class.setCurrentIndex(self.target_class.findData(target))

    def _file_row(self, edit: QLineEdit, file_filter: str) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        choose = QPushButton("Browse...")
        choose.clicked.connect(partial(self._browse, edit, file_filter))
        row.addWidget(choose)
        return container

    def _browse(self, edit: QLineEdit, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file", "", file_filter)
        if path:
            edit.setText(path)

    def _add_row(self, class_id: int, label: str, profile: Path | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        class_input = QSpinBox()
        class_input.setRange(0, 65534)
        class_input.setValue(class_id)
        label_input = QLineEdit(label)
        profile_input = QLineEdit(str(profile or ""))
        self.rows.append((class_input, label_input, profile_input))
        self.table.setCellWidget(row, 0, class_input)
        self.table.setCellWidget(row, 1, label_input)
        self.table.setCellWidget(row, 2, self._file_row(profile_input, "Pressure profile (*.json)"))
        class_input.valueChanged.connect(self._update_targets)
        label_input.textChanged.connect(self._update_targets)
        self._update_targets()

    def add_start_pose(self) -> None:
        class_id = max((row[0].value() for row in self.rows), default=-1) + 1
        self._add_row(min(class_id, 65534), f"Pose {min(class_id, 65534)}")

    def remove_start_pose(self) -> None:
        index = self.table.currentRow()
        if 0 <= index < len(self.rows):
            self.rows.pop(index)
            self.table.removeRow(index)
            self._update_targets()

    def _update_targets(self, _value=None) -> None:
        if self._updating_rows:
            return
        selected = self.target_class.currentData()
        self.target_class.blockSignals(True)
        self.target_class.clear()
        for class_input, label_input, _profile in self.rows:
            self.target_class.addItem(
                f"{label_input.text()} (class {class_input.value()})", class_input.value()
            )
        index = self.target_class.findData(selected)
        self.target_class.setCurrentIndex(index if index >= 0 else 0)
        self.target_class.blockSignals(False)
        self._update_target_rows()

    def _update_target_rows(self) -> None:
        target = self.target_class.currentData()
        for row, (class_input, _label, _profile) in enumerate(self.rows):
            self.table.cellWidget(row, 2).setEnabled(class_input.value() != target)

    def read_model_classes(self) -> None:
        if self._reader is not None:
            return
        path = Path(self.model.text()).expanduser()
        if not path.is_file() or path.suffix.lower() != ".pt":
            self.status.setText("Select an existing YOLO .pt model first.")
            return
        self.status.setText("Reading YOLO class names...")
        self.read_classes_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self._reader = ModelClassReader(path.resolve(), self)
        self._reader.loaded.connect(self._classes_loaded)
        self._reader.failed.connect(self._classes_failed)
        self._reader.finished.connect(self._reader_finished)
        self._reader.start()

    @pyqtSlot(object)
    def _classes_loaded(self, names: dict[int, str]) -> None:
        if self._reject_pending:
            return
        if (
            self._reader is not None
            and self._reader.path != Path(self.model.text()).expanduser().resolve()
        ):
            self.status.setText(
                "The model path changed. Read its classes again before assigning profiles."
            )
            return
        profiles = {row[0].value(): row[2].text() for row in self.rows}
        selected = self.target_class.currentData()
        self._updating_rows = True
        self.rows = []
        self.table.setRowCount(0)
        for class_id, name in sorted(names.items()):
            self._add_row(
                class_id, name, Path(profiles[class_id]) if profiles.get(class_id) else None
            )
        self._updating_rows = False
        self._update_targets()
        index = self.target_class.findData(selected)
        if index >= 0:
            self.target_class.setCurrentIndex(index)
        self.status.setText(
            f"Loaded {len(names)} YOLO classes. Assign profiles to the start poses."
        )

    @pyqtSlot(str)
    def _classes_failed(self, error: str) -> None:
        self.status.setText(f"Could not read YOLO classes: {error}")

    @pyqtSlot()
    def _reader_finished(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.deleteLater()
        self.read_classes_button.setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
        if self._reject_pending:
            super().reject()

    def reject(self) -> None:
        if self._reader is not None:
            self._reject_pending = True
            self._reader.requestInterruption()
            self.status.setText("Cancelling after the model reader finishes...")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._reader is not None:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)

    def _configuration_values(self) -> dict:
        labels, profiles = {}, {}
        for class_input, label_input, profile_input in self.rows:
            class_id = class_input.value()
            if class_id in labels:
                raise ValueError("YOLO class IDs must be unique")
            labels[class_id] = label_input.text().strip()
            profiles[class_id] = (
                Path(profile_input.text()) if profile_input.text().strip() else None
            )
        return dict(
            part_name=self.name.text(),
            model_path=Path(self.model.text()),
            class_labels=labels,
            target_class_id=self.target_class.currentData(),
            profiles_by_class=profiles,
            mesh_path=Path(self.mesh.text()) if self.mesh.text().strip() else None,
        )

    def _validate_and_accept(self) -> None:
        from bibazu_reorientation.manual_config import load_manual_definition

        try:
            values = self._configuration_values()
            if not values["class_labels"]:
                raise ValueError("Add YOLO start-pose classes first")
            # Validation before the save dialog writes no configuration or hardware state.
            target = values["target_class_id"]
            payload = dict(
                part_name=values["part_name"],
                model_path=str(values["model_path"].resolve()),
                mesh_path=str(values["mesh_path"].resolve()) if values["mesh_path"] else None,
                poses=[
                    dict(id=key, model_class_id=key, label=label)
                    for key, label in values["class_labels"].items()
                ],
                target_pose=target,
                transitions=[
                    dict(
                        from_pose=key,
                        to_pose=target,
                        pressure_profile=str(path.resolve()) if path else None,
                    )
                    for key, path in values["profiles_by_class"].items()
                    if key != target
                ],
            )
            load_manual_definition(Path.cwd() / "manual.yaml", payload)
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Manual configuration", str(exc))
            return
        self.accept()

    def create(self) -> PartDefinition | None:
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        suggested = str(self.definition.source_path) if self.definition else "manual.yaml"
        target, _ = QFileDialog.getSaveFileName(
            self, "Save manual configuration", suggested, "YAML (*.yaml *.yml)"
        )
        return (
            save_manual_definition(Path(target), **self._configuration_values()) if target else None
        )
