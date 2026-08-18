"""Roadmap JSON loading and transition selection for PressureControlGUI."""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pose_preview import render_mesh_preview
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
    quaternion_xyzw: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    mesh_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RoadmapTransition:
    edge_id: str
    source_pose_id: int
    target_pose_id: int
    transition_kind: str
    actuation: str
    signed_angle_deg: float | None
    capture_width_deg: float
    geometric_score: float | None
    flip_count: int = 1
    via_pose_ids: tuple[int, ...] = ()
    component_edge_ids: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return f"Übergang {self.source_pose_id}-{self.target_pose_id}"

    @property
    def calibratable(self) -> bool:
        return self.transition_kind in {"actuated", "multi_reorientation"}

    @property
    def is_multi_reorientation(self) -> bool:
        return self.transition_kind == "multi_reorientation"

    @property
    def category_label(self) -> str:
        if self.is_multi_reorientation:
            return f"Mehrfach-Reorientierung ({self.flip_count}×) · experimentell"
        if self.transition_kind == "actuated":
            return "Direkt · bevorzugt"
        return "Passiv · nur Information"


@dataclass(frozen=True, slots=True)
class RoadmapDocument:
    path: Path
    part_name: str
    mesh_path: Path | None
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


def _mesh_path(roadmap_path: Path, value: Any) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = roadmap_path.parent / path
    return path.resolve()


def _quaternion(value: Any) -> tuple[float, float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0, 1.0)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Roadmap pose quaternion must contain four values")
    quaternion = tuple(float(component) for component in value)
    norm = math.sqrt(sum(component * component for component in quaternion))
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("Roadmap pose quaternion must be finite and non-zero")
    return tuple(component / norm for component in quaternion)  # type: ignore[return-value]


def load_roadmap_document(path: str | Path) -> RoadmapDocument:
    source = Path(path).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unsupported roadmap schema version")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise TypeError("Roadmap JSON must contain node and edge lists")

    mesh_path = _mesh_path(source, payload.get("source"))

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
                quaternion_xyzw=_quaternion(node.get("representative_quaternion_xyzw")),
                mesh_path=mesh_path,
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

    robust_ids = {pose.pose_id for pose in poses if pose.stability == "robust"}
    direct_robust = tuple(
        edge
        for edge in transitions
        if edge.transition_kind == "actuated"
        and edge.source_pose_id in robust_ids
        and edge.target_pose_id in robust_ids
    )
    transitions.extend(_build_multi_reorientation_transitions(direct_robust))

    mesh_source = str(payload.get("source", source.stem))
    part_name = Path(mesh_source).stem or source.stem.replace("_roadmap", "")
    return RoadmapDocument(
        path=source,
        part_name=part_name,
        mesh_path=mesh_path,
        cad_status=str(payload.get("geometry_status", "unknown")),
        poses=tuple(poses),
        transitions=tuple(transitions),
    )


def _build_multi_reorientation_transitions(
    transitions: tuple[RoadmapTransition, ...],
) -> tuple[RoadmapTransition, ...]:
    adjacency: dict[int, list[RoadmapTransition]] = {}
    for edge in transitions:
        adjacency.setdefault(edge.source_pose_id, []).append(edge)
    for edges in adjacency.values():
        edges.sort(key=lambda edge: (edge.target_pose_id, edge.edge_id))

    candidates: list[tuple[tuple[int, ...], tuple[RoadmapTransition, ...]]] = []

    def walk(pose_ids: tuple[int, ...], edges: tuple[RoadmapTransition, ...]) -> None:
        if len(edges) in {2, 3}:
            candidates.append((pose_ids, edges))
        if len(edges) == 3:
            return
        for edge in adjacency.get(pose_ids[-1], []):
            if edge.target_pose_id in pose_ids:
                continue
            walk(pose_ids + (edge.target_pose_id,), edges + (edge,))

    for start in sorted(adjacency):
        walk((start,), ())
    candidates.sort(
        key=lambda item: (
            len(item[1]),
            item[0],
            tuple(edge.edge_id for edge in item[1]),
        )
    )
    base_counts: dict[str, int] = {}
    result: list[RoadmapTransition] = []
    for pose_ids, edges in candidates:
        flip_count = len(edges)
        base_id = f"multi{flip_count}:" + "->".join(map(str, pose_ids))
        option = base_counts.get(base_id, 0) + 1
        base_counts[base_id] = option
        edge_id = base_id if option == 1 else f"{base_id}:option{option}"
        result.append(
            RoadmapTransition(
                edge_id=edge_id,
                source_pose_id=pose_ids[0],
                target_pose_id=pose_ids[-1],
                transition_kind="multi_reorientation",
                actuation=f"multiple_reorientation_{flip_count}",
                signed_angle_deg=None,
                capture_width_deg=0.0,
                geometric_score=None,
                flip_count=flip_count,
                via_pose_ids=pose_ids[1:-1],
                component_edge_ids=tuple(edge.edge_id for edge in edges),
            )
        )
    return tuple(result)


def pose_pixmap(pose: RoadmapPose, width: int, height: int) -> QPixmap:
    if pose.mesh_path is not None and pose.mesh_path.is_file():
        try:
            return render_mesh_preview(
                pose.mesh_path,
                width,
                height,
                quaternion_xyzw=pose.quaternion_xyzw,
                caption=f"Roadmap pose {pose.pose_id}",
            )
        except (ImportError, OSError, ValueError):
            pass
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
    "multiple_reorientation_2": "Mehrfach-Reorientierung · 2 Flips",
    "multiple_reorientation_3": "Mehrfach-Reorientierung · 3 Flips",
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
        self._poses_by_id = {pose.pose_id: pose for pose in document.poses}
        self.setWindowTitle(f"Posenroadmap auswählen · {document.part_name}")
        self.setModal(True)
        self.resize(1120, 760)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(
            f"<b>{self.document.part_name}</b> · CAD-Status: {self.document.cad_status}"
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
            card_grid.addWidget(
                self._pose_card(pose), index // columns, index % columns
            )
        scroll.setWidget(cards)
        pose_box_layout.addWidget(scroll)
        layout.addWidget(pose_box)

        transition_box = QGroupBox("Mögliche Übergänge")
        transition_layout = QVBoxLayout(transition_box)
        self.transition_table = QTableWidget(0, 8)
        self.transition_table.setHorizontalHeaderLabels(
            ["ID", "Von", "Nach", "Kategorie", "Aktion", "Sollwinkel", "w", "s"]
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
            4, QHeaderView.ResizeMode.Stretch
        )
        transition_hint = QLabel(
            "<b>Blau:</b> direkte Übergänge zwischen stabilen Posen. "
            "<b>Gelb:</b> experimentelle, nicht bevorzugte Mehrfach-Reorientierungen "
            "mit 2 oder 3 Flips; ihr einzelnes Profil überschreibt die Kombination "
            "der jeweiligen Einzelprofile."
        )
        transition_hint.setWordWrap(True)
        transition_hint.setStyleSheet(
            "background: #edf7ff; border-left: 4px solid #1677c8; "
            "padding: 6px 9px; color: #1f3b53;"
        )
        transition_layout.addWidget(transition_hint)
        ordered_transitions = sorted(
            self.document.transitions,
            key=lambda transition: (
                not self._connects_stable_poses(transition),
                not transition.calibratable,
                transition.is_multi_reorientation,
                transition.source_pose_id,
                transition.target_pose_id,
                transition.edge_id,
            ),
        )
        for transition in ordered_transitions:
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
        card.setToolTip(f"Boden: {pose.floor_contact}; Wand: {pose.wall_contact}")
        return card

    def _connects_stable_poses(self, transition: RoadmapTransition) -> bool:
        source = self._poses_by_id[transition.source_pose_id]
        target = self._poses_by_id[transition.target_pose_id]
        return source.stability == "robust" and target.stability == "robust"

    def _add_transition_row(self, transition: RoadmapTransition) -> None:
        row = self.transition_table.rowCount()
        self.transition_table.insertRow(row)
        stable_pair = self._connects_stable_poses(transition)
        self.transition_table.setRowHeight(row, 34 if stable_pair else 27)
        values = (
            transition.display_name,
            str(transition.source_pose_id),
            str(transition.target_pose_id),
            transition.category_label,
            action_display_label(transition.actuation),
            (
                f"{transition.signed_angle_deg:+.1f}°"
                if transition.signed_angle_deg is not None
                else "—"
            ),
            (
                f"{transition.capture_width_deg:.1f}°"
                if transition.calibratable
                else "—"
            ),
            "—"
            if transition.geometric_score is None
            else f"{transition.geometric_score:.3f}",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            font = item.font()
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, transition.edge_id)
                item.setToolTip(f"Interne Kanten-ID: {transition.edge_id}")
            if transition.is_multi_reorientation:
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor("#664d03"))
                item.setBackground(QColor("#fff3cd"))
                via = " → ".join(map(str, transition.via_pose_ids))
                item.setToolTip(
                    f"{transition.flip_count} Flips über {via}; experimentell und nicht "
                    "bevorzugt. Ein Profil gilt für den gesamten Übergang."
                )
            elif stable_pair:
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor("#123a58"))
                item.setBackground(QColor("#dcefff"))
                item.setToolTip(
                    f"Stabil → stabil · interne Kanten-ID: {transition.edge_id}"
                )
            else:
                item.setForeground(QColor("#737b84"))
                item.setBackground(QColor("#f3f4f6"))
            if not transition.calibratable:
                item.setForeground(QColor("#92979d"))
                item.setToolTip("Passiver Übergang: sichtbar, aber nicht kalibrierbar")
            self.transition_table.setItem(row, column, item)

    def _selected_table_transition(self) -> RoadmapTransition | None:
        rows = self.transition_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.transition_table.item(rows[0].row(), 0)
        return self._transitions_by_id.get(str(item.data(Qt.ItemDataRole.UserRole)))

    def _on_transition_selection_changed(self) -> None:
        transition = self._selected_table_transition()
        calibratable = transition is not None and transition.calibratable
        self.use_button.setEnabled(calibratable)
        if transition is None:
            self.selection_label.setText("Noch kein Übergang ausgewählt")
        elif calibratable:
            via = (
                f" · über {' → '.join(map(str, transition.via_pose_ids))}"
                if transition.is_multi_reorientation
                else ""
            )
            self.selection_label.setText(
                f"Ausgewählt: {transition.display_name} · "
                f"{action_display_label(transition.actuation)}{via}"
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
