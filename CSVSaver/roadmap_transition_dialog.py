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
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
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
        return f"Transition {self.source_pose_id}-{self.target_pose_id}"

    @property
    def calibratable(self) -> bool:
        return self.transition_kind in {"actuated", "multi_reorientation"}

    @property
    def is_multi_reorientation(self) -> bool:
        return self.transition_kind == "multi_reorientation"

    @property
    def category_label(self) -> str:
        if self.is_multi_reorientation:
            return f"Multiple reorientation ({self.flip_count}×) · experimental"
        if self.transition_kind == "actuated":
            return "Direct · preferred"
        return "Passive · information only"


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
    component_flips: tuple[RoadmapTransition, ...] = ()

    @property
    def profile_name_stem(self) -> str:
        raw = (
            f"{self.part_name}_Transition_"
            f"{self.transition.source_pose_id}-{self.transition.target_pose_id}_"
            f"{self.transition.actuation}"
        )
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")


def roadmap_transition_metadata(selection: SelectedRoadmapTransition) -> dict[str, Any]:
    """Return the portable transition identity stored in a pressure profile."""
    transition = selection.transition
    return {
        "title": transition.display_name,
        "roadmap_path": str(selection.roadmap_path),
        "part_name": selection.part_name,
        "edge_id": transition.edge_id,
        "source_pose_id": transition.source_pose_id,
        "target_pose_id": transition.target_pose_id,
        "transition_kind": transition.transition_kind,
        "actuation": transition.actuation,
        "signed_angle_deg": transition.signed_angle_deg,
        "flip_count": transition.flip_count,
        "via_pose_ids": list(transition.via_pose_ids),
        "component_edge_ids": list(transition.component_edge_ids),
        "component_flips": [
            {
                "edge_id": flip.edge_id,
                "source_pose_id": flip.source_pose_id,
                "target_pose_id": flip.target_pose_id,
                "actuation": flip.actuation,
                "signed_angle_deg": flip.signed_angle_deg,
            }
            for flip in selection.component_flips
        ],
    }


def selection_from_roadmap_transition_metadata(
    value: Any,
) -> SelectedRoadmapTransition | None:
    """Restore a selected transition without making old profiles invalid.

    Keep the saved transition identity even if regeneration removed its edge.
    Use the referenced roadmap for pose images whenever it remains available.
    """
    if not isinstance(value, dict):
        return None
    try:
        roadmap_path = Path(str(value["roadmap_path"])).expanduser().resolve()
        edge_id = str(value["edge_id"])
        source_id = int(value["source_pose_id"])
        target_id = int(value["target_pose_id"])
        transition = RoadmapTransition(
            edge_id=edge_id,
            source_pose_id=source_id,
            target_pose_id=target_id,
            transition_kind=str(value.get("transition_kind", "actuated")),
            actuation=str(value["actuation"]),
            signed_angle_deg=(
                None
                if value.get("signed_angle_deg") is None
                else float(value["signed_angle_deg"])
            ),
            capture_width_deg=0.0,
            geometric_score=None,
            flip_count=int(value.get("flip_count", 1)),
            via_pose_ids=tuple(int(item) for item in value.get("via_pose_ids", [])),
            component_edge_ids=tuple(
                str(item) for item in value.get("component_edge_ids", [])
            ),
        )
        source_pose = RoadmapPose(
            source_id, (source_id,), "robust", "unknown", "unknown", None
        )
        target_pose = RoadmapPose(
            target_id, (target_id,), "robust", "unknown", "unknown", None
        )
        document = None
        try:
            document = load_roadmap_document(roadmap_path)
            source_pose = document.pose(source_id)
            target_pose = document.pose(target_id)
        except (KeyError, OSError, TypeError, ValueError):
            pass
        return SelectedRoadmapTransition(
            roadmap_path=roadmap_path,
            part_name=str(value.get("part_name", roadmap_path.stem)),
            transition=transition,
            source_pose=source_pose,
            target_pose=target_pose,
            component_flips=_restore_component_flips(value, transition, document),
        )
    except (KeyError, OSError, TypeError, ValueError, StopIteration):
        return None


def _restore_component_flips(
    metadata: dict[str, Any],
    transition: RoadmapTransition,
    document: RoadmapDocument | None,
) -> tuple[RoadmapTransition, ...]:
    """Prefer saved flip angles; recover older profiles from their exact edges."""
    by_id = {edge.edge_id: edge for edge in document.transitions} if document else {}
    saved_flips = metadata.get("component_flips", [])
    if isinstance(saved_flips, list):
        for value in saved_flips:
            try:
                flip = RoadmapTransition(
                    edge_id=str(value["edge_id"]),
                    source_pose_id=int(value["source_pose_id"]),
                    target_pose_id=int(value["target_pose_id"]),
                    transition_kind="actuated",
                    actuation=str(value["actuation"]),
                    signed_angle_deg=(
                        None if value.get("signed_angle_deg") is None
                        else float(value["signed_angle_deg"])
                    ),
                    capture_width_deg=0.0,
                    geometric_score=None,
                )
                by_id[flip.edge_id] = flip
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    pose_ids = (transition.source_pose_id, *transition.via_pose_ids, transition.target_pose_id)
    flips = []
    for index, edge_id in enumerate(transition.component_edge_ids):
        flip = by_id.get(edge_id)
        if (
            flip is not None
            and index + 1 < len(pose_ids)
            and flip.source_pose_id == pose_ids[index]
            and flip.target_pose_id == pose_ids[index + 1]
        ):
            flips.append(flip)
    return tuple(flips)


def selection_from_pressure_profile(
    profile: dict[str, Any], profile_path: Path
) -> SelectedRoadmapTransition | None:
    """Restore metadata, or recover the pose pair from a legacy profile name."""
    metadata = profile.get("roadmap_transition")
    if isinstance(metadata, dict):
        return selection_from_roadmap_transition_metadata(metadata)
    match = re.fullmatch(
        r"(.+)_(?:Uebergang|Transition)_(\d+)-(\d+)_"
        r"(floor_main_(?:pos|neg)_x|wall_main_(?:pos|neg)_x|free_[yz]|"
        r"multiple_reorientation_[23])(?:_.*)?",
        profile_path.stem,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    part_name, source_id, target_id, actuation = match.groups()
    actuation = actuation.lower()
    workspace = Path(__file__).resolve().parents[2]
    roots = (
        profile_path.parent,
        workspace / "bibazu_geometry_to_pose" / "Poses_Found_Robust",
        workspace / "BiBaZu_Big_Boi" / "ReorientationControlGUI" / "all_1_roadmaps",
    )
    roadmap_path = profile_path.parent / f"{part_name}_roadmap.json"
    for root in roots:
        candidates = sorted(root.rglob(f"{part_name}_roadmap.json")) if root.is_dir() else []
        if candidates:
            roadmap_path = candidates[0]
            break
    multi = actuation.startswith("multiple_reorientation_")
    return selection_from_roadmap_transition_metadata({
        "roadmap_path": str(roadmap_path),
        "part_name": part_name,
        "edge_id": f"legacy:{profile_path.stem}",
        "source_pose_id": int(source_id),
        "target_pose_id": int(target_id),
        "transition_kind": "multi_reorientation" if multi else "actuated",
        "actuation": actuation,
        "flip_count": int(actuation.rsplit("_", 1)[1]) if multi else 1,
    })


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
    "floor_main_neg_x": "−X · main face on floor",
    "floor_main_pos_x": "+X · main face on floor",
    "wall_main_neg_x": "−X · main face on wall",
    "wall_main_pos_x": "+X · main face on wall",
    "free_y": "free Y rotation",
    "free_z": "free Z rotation",
    "passive": "passive tipping",
    "multiple_reorientation_2": "Multiple reorientation · 2 flips",
    "multiple_reorientation_3": "Multiple reorientation · 3 flips",
}


def action_display_label(actuation: str) -> str:
    return _ACTION_LABELS.get(actuation, actuation)


class RoadmapMap(QWidget):
    """Clickable robust-pose layer using the exported geometry roadmap layout."""

    def __init__(self, document: RoadmapDocument, on_pose_clicked: Any) -> None:
        super().__init__()
        self.document = document
        self.on_pose_clicked = on_pose_clicked
        self.poses = tuple(
            pose for pose in document.poses if pose.stability == "robust"
        )
        self.start_pose_id: int | None = None
        self.end_pose_id: int | None = None
        self._pixmaps = {pose.pose_id: self._thumbnail(pose) for pose in self.poses}
        self._grid_slots, self._columns, self._rows = self._optimised_grid_slots()
        self.setMinimumHeight(560)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click a pose image to choose start, then end pose")

    def _thumbnail(self, pose: RoadmapPose) -> QPixmap:
        """Use the exact thumbnail embedded by geometry_to_pose when available."""
        pixmap = QPixmap()
        if pose.thumbnail_png and pixmap.loadFromData(pose.thumbnail_png, "PNG"):
            return pixmap
        return pose_pixmap(pose, 160, 120)

    @property
    def layout_summary(self) -> str:
        return (
            f"Layout: {self._rows} row(s) × {self._columns} column(s) "
            "· path distance optimized"
        )

    def _optimised_grid_slots(self) -> tuple[dict[int, tuple[int, int]], int, int]:
        """Place robust poses on a compact grid and shorten all visible paths.

        This follows the geometry-to-pose plot strategy: choose a grid near its
        3:2 canvas ratio, then make deterministic best-improvement swaps.  Unlike
        the exported plot, metastable nodes are excluded before laying out cards,
        so robust thumbnails never inherit crowded or empty metastable slots.
        """
        count = len(self.poses)
        if not count:
            return {}, 1, 1
        candidates: list[tuple[float, int, int]] = []
        for columns in range(1, count + 1):
            rows = math.ceil(count / columns)
            aspect_error = abs(math.log((columns / rows) / 1.5))
            unused_fraction = (columns * rows - count) / count
            candidates.append((aspect_error + 0.12 * unused_fraction, columns, rows))
        _, columns, rows = min(candidates)
        pose_ids = [pose.pose_id for pose in self.poses]
        slots = {
            pose_id: (index % columns, index // columns)
            for index, pose_id in enumerate(pose_ids)
        }
        robust_ids = set(pose_ids)
        weights: dict[tuple[int, int], int] = {}
        for edge in self.document.transitions:
            if (
                edge.is_multi_reorientation
                or not edge.calibratable
                or edge.source_pose_id not in robust_ids
                or edge.target_pose_id not in robust_ids
            ):
                continue
            key = tuple(sorted((edge.source_pose_id, edge.target_pose_id)))
            weights[key] = weights.get(key, 0) + 1

        def distance(left: int, right: int) -> float:
            left_slot, right_slot = slots[left], slots[right]
            return math.hypot(
                left_slot[0] - right_slot[0], left_slot[1] - right_slot[1]
            )

        # The grid is deliberately fixed; only which pose occupies a cell moves.
        # Greedy best-improvement is stable and practical for roadmap sizes here.
        for _ in range(max(1, 2 * count)):
            best_gain = 1e-9
            best_swap: tuple[int, int] | None = None
            for left_index, left in enumerate(pose_ids):
                for right in pose_ids[left_index + 1 :]:
                    affected = [
                        (source, target, weight)
                        for (source, target), weight in weights.items()
                        if left in {source, target} or right in {source, target}
                    ]
                    before = sum(weight * distance(source, target) for source, target, weight in affected)
                    slots[left], slots[right] = slots[right], slots[left]
                    after = sum(weight * distance(source, target) for source, target, weight in affected)
                    slots[left], slots[right] = slots[right], slots[left]
                    if before - after > best_gain:
                        best_gain = before - after
                        best_swap = (left, right)
            if best_swap is None:
                break
            left, right = best_swap
            slots[left], slots[right] = slots[right], slots[left]
        return slots, columns, rows

    def _card_size(self) -> tuple[int, int]:
        rect = self.contentsRect().adjusted(26, 22, -26, -36)
        cell_width = max(1, rect.width() / self._columns)
        cell_height = max(1, rect.height() / self._rows)
        width = max(64, min(160, round(cell_width - 24)))
        height = max(48, min(round(width * 0.75), round(cell_height - 30)))
        return width, height

    def _positions(self) -> dict[int, tuple[float, float]]:
        rect = self.contentsRect().adjusted(26, 22, -26, -22)
        return {
            pose.pose_id: (
                rect.left() + (self._grid_slots[pose.pose_id][0] + 0.5) * rect.width() / self._columns,
                rect.top() + (self._grid_slots[pose.pose_id][1] + 0.5) * rect.height() / self._rows,
            )
            for pose in self.poses
        }

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfdff"))
        positions = self._positions()
        card_width, card_height = self._card_size()
        for edge in self.document.transitions:
            if (
                edge.is_multi_reorientation
                or not edge.calibratable
                or edge.source_pose_id not in positions
                or edge.target_pose_id not in positions
            ):
                continue
            start, end = positions[edge.source_pose_id], positions[edge.target_pose_id]
            color = QColor(
                "#d62728"
                if edge.actuation.endswith("_x")
                else "#2ca02c" if edge.actuation == "free_y" else "#1f77b4"
            )
            painter.setPen(QPen(color, 2.4, Qt.PenStyle.SolidLine))
            painter.drawLine(round(start[0]), round(start[1]), round(end[0]), round(end[1]))
        for pose_id, (x, y) in positions.items():
            if pose_id == self.start_pose_id:
                border = QColor("#e3a008")
            elif pose_id == self.end_pose_id:
                border = QColor("#18a558")
            else:
                border = QColor("#084081")
            pixmap = self._pixmaps[pose_id]
            width, height = (card_width, card_height) if not pixmap.isNull() else (82, 58)
            left, top = round(x - width / 2), round(y - height / 2)
            painter.setPen(QPen(border, 4))
            painter.setBrush(QBrush(QColor("white")))
            painter.drawRect(left - 3, top - 3, width + 6, height + 6)
            if pixmap.isNull():
                painter.setPen(QPen(QColor("#374151")))
                painter.drawText(left, top, width, height, Qt.AlignmentFlag.AlignCenter, f"Pose {pose_id}")
            else:
                painter.drawPixmap(left, top, width, height, pixmap)
            painter.setPen(QPen(QColor("#111827")))
            painter.drawText(left, top + height + 4, width, 18, Qt.AlignmentFlag.AlignCenter, f"Pose {pose_id}")

    def mousePressEvent(self, event) -> None:
        positions = self._positions()
        card_width, card_height = self._card_size()
        for pose in self.poses:
            x, y = positions[pose.pose_id]
            if (
                abs(event.position().x() - x) <= card_width / 2 + 4
                and abs(event.position().y() - y) <= card_height / 2 + 4
            ):
                self.on_pose_clicked(pose.pose_id)
                return


class RoadmapTransitionDialog(QDialog):
    def __init__(
        self,
        document: RoadmapDocument,
        parent: QWidget | None = None,
        profile_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.document = document
        self.profile_directory = profile_directory
        self.selected_transition: SelectedRoadmapTransition | None = None
        self.start_pose_id: int | None = None
        self.end_pose_id: int | None = None
        self._transitions_by_id = {item.edge_id: item for item in document.transitions}
        self._saved_profiles_by_edge = self._find_saved_profiles()
        self.setWindowTitle(f"Select roadmap path · {document.part_name}")
        self.setModal(True)
        self.resize(1320, 980)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(f"<b>{self.document.part_name}</b> · CAD status: {self.document.cad_status}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:18px; padding:6px;")
        layout.addWidget(title)
        hint = QLabel(
            "Click the blue roadmap map: first choose the <b>start</b> pose, then the "
            "<b>end</b> pose. Only paths joining those two poses are shown below. "
            "They are ordered from one flip to multi-flip alternatives."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("background:#eef6ff;border:1px solid #7aa7d9;border-radius:5px;padding:8px;")
        layout.addWidget(hint)
        map_box = QGroupBox("Pose roadmap")
        map_layout = QVBoxLayout(map_box)
        self.map_widget = RoadmapMap(self.document, self._pose_clicked)
        map_layout.addWidget(self.map_widget)
        map_footer = QHBoxLayout()
        self.pose_selection_label = QLabel("Start: —    End: —")
        self.pose_selection_label.setStyleSheet("font-weight:600; color:#1f3b53;")
        map_footer.addWidget(self.pose_selection_label)
        layout_label = QLabel(self.map_widget.layout_summary)
        layout_label.setStyleSheet("color:#4b5563;")
        map_footer.addWidget(layout_label)
        map_footer.addStretch(1)
        clear_button = QPushButton("Choose start again")
        clear_button.clicked.connect(self._clear_pose_selection)
        map_footer.addWidget(clear_button)
        map_layout.addLayout(map_footer)
        layout.addWidget(map_box)

        transition_box = QGroupBox("Paths for selected poses")
        transition_layout = QVBoxLayout(transition_box)
        self.path_hint = QLabel("Select a start and end pose on the map.")
        self.path_hint.setWordWrap(True)
        transition_layout.addWidget(self.path_hint)
        self.transition_table = QTableWidget(0, 4)
        self.transition_table.setHorizontalHeaderLabels(["Path", "Flips", "Flip axis / angle", "Saved profile"])
        self.transition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.transition_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.transition_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transition_table.setAlternatingRowColors(True)
        self.transition_table.verticalHeader().setVisible(False)
        header = self.transition_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.transition_table.itemSelectionChanged.connect(self._on_transition_selection_changed)
        self.transition_table.cellDoubleClicked.connect(lambda _row, _column: self._accept_selected_transition())
        transition_layout.addWidget(self.transition_table)
        layout.addWidget(transition_box, 1)

        self.selection_label = QLabel("No path selected")
        footer = QHBoxLayout()
        footer.addWidget(self.selection_label)
        footer.addStretch(1)
        self.use_button = QPushButton("Use selected path")
        self.use_button.setEnabled(False)
        self.use_button.setStyleSheet("QPushButton:enabled {background:#1677c8;color:white;font-weight:600;padding:7px 14px;}")
        self.use_button.clicked.connect(self._accept_selected_transition)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        footer.addWidget(self.use_button)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _find_saved_profiles(self) -> dict[str, tuple[Path, ...]]:
        found: dict[str, list[Path]] = {}
        directory = self.profile_directory
        if directory is None or not directory.is_dir():
            return {}
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            metadata = payload.get("roadmap_transition") if isinstance(payload, dict) else None
            for transition in self.document.transitions:
                matches_metadata = False
                if isinstance(metadata, dict):
                    try:
                        matches_metadata = (
                            str(metadata.get("edge_id", "")) == transition.edge_id
                            or (
                                int(metadata.get("source_pose_id", -1))
                                == transition.source_pose_id
                                and int(metadata.get("target_pose_id", -1))
                                == transition.target_pose_id
                                and str(metadata.get("actuation", ""))
                                == transition.actuation
                                and int(metadata.get("flip_count", 1))
                                == transition.flip_count
                            )
                        )
                    except (TypeError, ValueError):
                        pass
                name_stem = SelectedRoadmapTransition(
                    self.document.path, self.document.part_name, transition,
                    self.document.pose(transition.source_pose_id), self.document.pose(transition.target_pose_id),
                ).profile_name_stem
                if matches_metadata or path.stem in {
                    name_stem, name_stem.replace("_Transition_", "_Uebergang_", 1)
                }:
                    found.setdefault(transition.edge_id, []).append(path)
        return {edge_id: tuple(paths) for edge_id, paths in found.items()}

    def _pose_clicked(self, pose_id: int) -> None:
        if self.start_pose_id is None or self.end_pose_id is not None:
            self.start_pose_id, self.end_pose_id = pose_id, None
        elif pose_id == self.start_pose_id:
            self.pose_selection_label.setText(f"Start: Pose {pose_id}    End: choose a different pose")
            return
        else:
            self.end_pose_id = pose_id
        self._refresh_paths()

    def _clear_pose_selection(self) -> None:
        self.start_pose_id = None
        self.end_pose_id = None
        self._refresh_paths()

    def _set_pose_selection(self, start_pose_id: int, end_pose_id: int) -> None:
        """Small test/programmatic hook using the same filter as map clicks."""
        self.start_pose_id = start_pose_id
        self.end_pose_id = end_pose_id
        self._refresh_paths()

    def _refresh_paths(self) -> None:
        self.map_widget.start_pose_id = self.start_pose_id
        self.map_widget.end_pose_id = self.end_pose_id
        self.map_widget.update()
        self.transition_table.setRowCount(0)
        self.use_button.setEnabled(False)
        self.selection_label.setText("No path selected")
        if self.start_pose_id is None:
            self.pose_selection_label.setText("Start: —    End: —")
            self.path_hint.setText("Select a start and end pose on the map.")
            return
        if self.end_pose_id is None:
            self.pose_selection_label.setText(f"Start: Pose {self.start_pose_id}    End: —")
            self.path_hint.setText("Now click the desired end pose.")
            return
        self.pose_selection_label.setText(f"Start: Pose {self.start_pose_id}    End: Pose {self.end_pose_id}")
        options = sorted(
            (
                item for item in self.document.transitions
                if item.calibratable
                and item.source_pose_id == self.start_pose_id
                and item.target_pose_id == self.end_pose_id
            ),
            key=lambda item: (item.flip_count, item.edge_id),
        )
        if not options:
            self.path_hint.setText("No calibratable path was found for these two poses.")
            return
        self.path_hint.setText(f"{len(options)} path option(s), ordered by required reorientations.")
        for transition in options:
            self._add_transition_row(transition)

    def _step_summary(self, transition: RoadmapTransition) -> str:
        if not transition.is_multi_reorientation:
            angle = "angle not specified" if transition.signed_angle_deg is None else f"{transition.signed_angle_deg:+.1f}°"
            return f"{action_display_label(transition.actuation)} ({angle})"
        steps: list[str] = []
        for edge_id in transition.component_edge_ids:
            edge = self._transitions_by_id.get(edge_id)
            if edge is None:
                steps.append(edge_id)
                continue
            angle = "?" if edge.signed_angle_deg is None else f"{edge.signed_angle_deg:+.1f}°"
            steps.append(f"{action_display_label(edge.actuation)} ({angle})")
        return "  →  ".join(steps) or "component flips not available"

    def _add_transition_row(self, transition: RoadmapTransition) -> None:
        row = self.transition_table.rowCount()
        self.transition_table.insertRow(row)
        pose_ids = (transition.source_pose_id, *transition.via_pose_ids, transition.target_pose_id)
        saved = self._saved_profiles_by_edge.get(transition.edge_id, ())
        values = (
            " → ".join(f"Pose {item}" for item in pose_ids),
            str(transition.flip_count),
            self._step_summary(transition),
            "Saved: " + ", ".join(path.name for path in saved) if saved else "No saved profile",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, transition.edge_id)
            if transition.flip_count == 1:
                item.setBackground(QColor("#dcefff"))
                item.setForeground(QColor("#123a58"))
            else:
                item.setBackground(QColor("#fff3cd"))
                item.setForeground(QColor("#664d03"))
                item.setToolTip("Experimental direct profile for this complete multi-flip path.")
            self.transition_table.setItem(row, column, item)
        self.transition_table.setRowHeight(row, 34)

    def _selected_table_transition(self) -> RoadmapTransition | None:
        rows = self.transition_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.transition_table.item(rows[0].row(), 0)
        return self._transitions_by_id.get(str(item.data(Qt.ItemDataRole.UserRole)))

    def _on_transition_selection_changed(self) -> None:
        transition = self._selected_table_transition()
        self.use_button.setEnabled(transition is not None)
        if transition is None:
            self.selection_label.setText("No path selected")
        else:
            self.selection_label.setText(
                f"Selected: {transition.flip_count} flip(s) · {self._step_summary(transition)}"
            )

    def _accept_selected_transition(self) -> None:
        transition = self._selected_table_transition()
        if transition is None:
            return
        self.selected_transition = SelectedRoadmapTransition(
            roadmap_path=self.document.path,
            part_name=self.document.part_name,
            transition=transition,
            source_pose=self.document.pose(transition.source_pose_id),
            target_pose=self.document.pose(transition.target_pose_id),
            component_flips=tuple(
                self._transitions_by_id[edge_id]
                for edge_id in transition.component_edge_ids
            ),
        )
        self.accept()
