"""Roadmap JSON loading and transition selection for PressureControlGUI."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class RoadmapPose:
    pose_id: int
    equivalent_pose_ids: tuple[int, ...]
    stability: str
    floor_contact: str
    wall_contact: str
    thumbnail_png: bytes | None


@dataclass(frozen=True, slots=True)
class RoadmapTransition:
    edge_id: str
    source_pose_id: int
    target_pose_id: int
    transition_kind: str
    actuation: str
    signed_angle_deg: float
    capture_width_deg: float
    geometric_score: float

    @property
    def display_name(self) -> str:
        return f"Übergang {self.source_pose_id}-{self.target_pose_id}"

    @property
    def calibratable(self) -> bool:
        return self.transition_kind == "actuated"


@dataclass(frozen=True, slots=True)
class RoadmapDocument:
    path: Path
    part_name: str
    cad_status: str
    poses: tuple[RoadmapPose, ...]
    transitions: tuple[RoadmapTransition, ...]

    def pose(self, pose_id: int) -> RoadmapPose:
        for pose in self.poses:
            if pose.pose_id == pose_id:
                return pose
        raise KeyError(pose_id)


@dataclass(frozen=True, slots=True)
class SelectedRoadmapTransition:
    roadmap_path: Path
    part_name: str
    transition: RoadmapTransition
    source_pose: RoadmapPose
    target_pose: RoadmapPose

    @property
    def profile_name_stem(self) -> str:
        raw = (
            f"{self.part_name}_Uebergang_"
            f"{self.transition.source_pose_id}-{self.transition.target_pose_id}_"
            f"{self.transition.actuation}"
        )
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")


def _decode_thumbnail(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None


def load_roadmap_document(path: str | Path) -> RoadmapDocument:
    source = Path(path).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unsupported roadmap schema version")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise TypeError("Roadmap JSON must contain node and edge lists")

    poses: list[RoadmapPose] = []
    for node in raw_nodes:
        if not isinstance(node, dict):
            raise TypeError("Every roadmap node must be an object")
        stability = str(node.get("kind", ""))
        if stability not in {"robust", "metastable"}:
            raise ValueError("Roadmap node kind must be robust or metastable")
        poses.append(
            RoadmapPose(
                pose_id=int(node["node_id"]),
                equivalent_pose_ids=tuple(int(value) for value in node["pose_ids"]),
                stability=stability,
                floor_contact=str(node.get("floor_contact_topology", "unknown")),
                wall_contact=str(node.get("wall_contact_topology", "unknown")),
                thumbnail_png=_decode_thumbnail(node.get("thumbnail_png_base64")),
            )
        )

    pose_ids = {pose.pose_id for pose in poses}
    transitions: list[RoadmapTransition] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise TypeError("Every roadmap edge must be an object")
        source_id = int(edge["source"])
        target_id = int(edge["target"])
        if source_id not in pose_ids or target_id not in pose_ids:
            raise ValueError("Roadmap edge references an unknown pose")
        transitions.append(
            RoadmapTransition(
                edge_id=str(edge["edge_id"]),
                source_pose_id=source_id,
                target_pose_id=target_id,
                transition_kind=str(edge["transition_kind"]),
                actuation=str(edge["actuation"]),
                signed_angle_deg=float(edge["signed_angle_deg"]),
                capture_width_deg=float(edge.get("capture_width_deg", 0.0)),
                geometric_score=float(edge.get("geometric_score", 0.0)),
            )
        )

    mesh_source = str(payload.get("source", source.stem))
    part_name = Path(mesh_source).stem or source.stem.replace("_roadmap", "")
    return RoadmapDocument(
        path=source,
        part_name=part_name,
        cad_status=str(payload.get("geometry_status", "unknown")),
        poses=tuple(poses),
        transitions=tuple(transitions),
    )


def pose_pixmap(pose: RoadmapPose, width: int, height: int) -> QPixmap:
    pixmap = QPixmap()
    if pose.thumbnail_png and pixmap.loadFromData(pose.thumbnail_png, "PNG"):
        return pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


_ACTION_LABELS = {
    "floor_main_neg_x": "−X · Hauptfläche Boden",
    "floor_main_pos_x": "+X · Hauptfläche Boden",
    "wall_main_neg_x": "−X · Hauptfläche Wand",
    "wall_main_pos_x": "+X · Hauptfläche Wand",
    "free_y": "freie Y-Rotation",
    "free_z": "freie Z-Rotation",
    "passive": "passives Kippen",
}


def action_display_label(actuation: str) -> str:
    return _ACTION_LABELS.get(actuation, actuation)


class RoadmapTransitionDialog(QDialog):
    def __init__(
        self, document: RoadmapDocument, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.document = document
        self.selected_transition: SelectedRoadmapTransition | None = None
        self._transitions_by_id = {
            transition.edge_id: transition for transition in document.transitions
        }
        self.setWindowTitle(f"Posenroadmap auswählen · {document.part_name}")
        self.setModal(True)
        self.resize(1120, 760)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(
            f"<b>{self.document.part_name}</b> · CAD-Status: "
            f"{self.document.cad_status}"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 18px; padding: 8px;")
        layout.addWidget(title)

        instruction = QLabel(
            "Stabile Posen sind kräftig umrandet. Wähle eine Tabellenzeile und "
            "klicke anschließend auf „Übergang übernehmen“; ein Doppelklick "
            "übernimmt direkt. Passive Kanten werden nur zur Übersicht angezeigt."
        )
        instruction.setWordWrap(True)
        instruction.setStyleSheet(
            "background: #eef6ff; border: 1px solid #7aa7d9; "
            "border-radius: 5px; padding: 8px;"
        )
        layout.addWidget(instruction)

        pose_box = QGroupBox("Ermittelte Posen")
        pose_box_layout = QVBoxLayout(pose_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(245)
        cards = QWidget()
        card_grid = QGridLayout(cards)
        card_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        ordered_poses = sorted(
            self.document.poses,
            key=lambda pose: (pose.stability != "robust", pose.pose_id),
        )
        columns = 6
        for index, pose in enumerate(ordered_poses):
            card_grid.addWidget(self._pose_card(pose), index // columns, index % columns)
        scroll.setWidget(cards)
        pose_box_layout.addWidget(scroll)
        layout.addWidget(pose_box)

        transition_box = QGroupBox("Mögliche direkte Übergänge")
        transition_layout = QVBoxLayout(transition_box)
        self.transition_table = QTableWidget(0, 7)
        self.transition_table.setHorizontalHeaderLabels(
            ["ID", "Von", "Nach", "Aktion", "Sollwinkel", "w", "s"]
        )
        self.transition_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.transition_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.transition_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.transition_table.setAlternatingRowColors(True)
        self.transition_table.verticalHeader().setVisible(False)
        self.transition_table.horizontalHeader().setStretchLastSection(False)
        self.transition_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        for transition in self.document.transitions:
            self._add_transition_row(transition)
        self.transition_table.itemSelectionChanged.connect(
            self._on_transition_selection_changed
        )
        self.transition_table.cellDoubleClicked.connect(
            lambda _row, _column: self._accept_selected_transition()
        )
        transition_layout.addWidget(self.transition_table)
        layout.addWidget(transition_box, 1)

        self.selection_label = QLabel("Noch kein kalibrierbarer Übergang ausgewählt")
        self.selection_label.setStyleSheet("font-weight: 600; color: #374151;")
        footer = QHBoxLayout()
        footer.addWidget(self.selection_label)
        footer.addStretch(1)
        self.use_button = QPushButton("Übergang übernehmen")
        self.use_button.setEnabled(False)
        self.use_button.setStyleSheet(
            "QPushButton:enabled { background: #1677c8; color: white; "
            "font-weight: 600; padding: 7px 14px; }"
        )
        self.use_button.clicked.connect(self._accept_selected_transition)
        close_button = QPushButton("Schließen")
        close_button.clicked.connect(self.reject)
        footer.addWidget(self.use_button)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _pose_card(self, pose: RoadmapPose) -> QFrame:
        card = QFrame()
        robust = pose.stability == "robust"
        card.setFixedSize(165, 190)
        card.setStyleSheet(
            "QFrame { background: white; border-radius: 6px; "
            f"border: {'3px solid #1677c8' if robust else '2px dashed #8a94a3'}; }}"
        )
        layout = QVBoxLayout(card)
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setFixedHeight(125)
        pixmap = pose_pixmap(pose, 150, 120)
        if pixmap.isNull():
            image.setText("Kein Vorschaubild\nin dieser JSON")
            image.setStyleSheet("color: #6b7280; border: none;")
        else:
            image.setPixmap(pixmap)
            image.setStyleSheet("border: none;")
        layout.addWidget(image)
        pose_ids = "/".join(str(value) for value in pose.equivalent_pose_ids)
        label = QLabel(
            f"<b>Pose {pose.pose_id}</b><br><small>{pose_ids}</small><br>"
            f"{'stabil' if robust else 'metastabil'}"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("border: none;")
        layout.addWidget(label)
        card.setToolTip(
            f"Boden: {pose.floor_contact}; Wand: {pose.wall_contact}"
        )
        return card

    def _add_transition_row(self, transition: RoadmapTransition) -> None:
        row = self.transition_table.rowCount()
        self.transition_table.insertRow(row)
        values = (
            transition.display_name,
            str(transition.source_pose_id),
            str(transition.target_pose_id),
            action_display_label(transition.actuation),
            (
                f"{transition.signed_angle_deg:+.1f}°"
                if transition.calibratable
                else "—"
            ),
            (
                f"{transition.capture_width_deg:.1f}°"
                if transition.calibratable
                else "—"
            ),
            f"{transition.geometric_score:.3f}",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, transition.edge_id)
                item.setToolTip(f"Interne Kanten-ID: {transition.edge_id}")
            if not transition.calibratable:
                item.setForeground(QColor("#7f7f7f"))
                item.setToolTip("Passiver Übergang: sichtbar, aber nicht kalibrierbar")
            self.transition_table.setItem(row, column, item)

    def _selected_table_transition(self) -> RoadmapTransition | None:
        rows = self.transition_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.transition_table.item(rows[0].row(), 0)
        return self._transitions_by_id.get(
            str(item.data(Qt.ItemDataRole.UserRole))
        )

    def _on_transition_selection_changed(self) -> None:
        transition = self._selected_table_transition()
        calibratable = transition is not None and transition.calibratable
        self.use_button.setEnabled(calibratable)
        if transition is None:
            self.selection_label.setText("Noch kein Übergang ausgewählt")
        elif calibratable:
            self.selection_label.setText(
                f"Ausgewählt: {transition.display_name} · "
                f"{action_display_label(transition.actuation)}"
            )
        else:
            self.selection_label.setText(
                f"{transition.display_name} ist passiv und nicht kalibrierbar"
            )

    def _accept_selected_transition(self) -> None:
        transition = self._selected_table_transition()
        if transition is None or not transition.calibratable:
            return
        try:
            source_pose = self.document.pose(transition.source_pose_id)
            target_pose = self.document.pose(transition.target_pose_id)
        except KeyError as exc:
            QMessageBox.critical(self, "Roadmap-Fehler", f"Pose fehlt: {exc}")
            return
        self.selected_transition = SelectedRoadmapTransition(
            roadmap_path=self.document.path,
            part_name=self.document.part_name,
            transition=transition,
            source_pose=source_pose,
            target_pose=target_pose,
        )
        self.accept()
