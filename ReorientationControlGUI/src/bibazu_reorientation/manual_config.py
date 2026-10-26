"""Roadmap-free configuration: one direct pressure profile per recognized pose."""

from pathlib import Path
from typing import Any

from bibazu_reorientation.config import _atomic_yaml, _portable_path, _resolve, _resolve_optional
from bibazu_reorientation.models import PartDefinition, PoseDefinition, TransitionSpec

MANUAL_SCHEMA_VERSION = 3


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def load_manual_definition(source: Path, payload: dict) -> PartDefinition:
    name = str(payload.get("part_name", "")).strip()
    if not name:
        raise ValueError("Part name must not be empty")
    raw_poses = payload.get("poses")
    if not isinstance(raw_poses, list) or not raw_poses:
        raise ValueError("Add at least one YOLO start-pose class")
    poses = []
    saved_pose_classes: dict[int, int] = {}
    for raw in raw_poses:
        if not isinstance(raw, dict):
            raise ValueError("Each start pose must be an object")
        pose_id = _integer(raw.get("id"), "Pose ID", 0, 65535)
        class_id = _integer(raw.get("model_class_id"), "YOLO class ID", 0, 65534)
        label = str(raw.get("label", "")).strip()
        if not label:
            raise ValueError("Each start pose needs a label")
        saved_pose_classes[pose_id] = class_id
        poses.append(PoseDefinition(class_id, label, class_id))
    if (
        len(saved_pose_classes) != len(poses)
        or len({pose.model_class_id for pose in poses}) != len(poses)
    ):
        raise ValueError("Pose IDs and YOLO class IDs must be unique")
    saved_target = _integer(payload.get("target_pose"), "Target pose", 0, 65535)
    if saved_target not in saved_pose_classes:
        raise ValueError("Select a target from the configured YOLO classes")
    # Older manual YAMLs saved class + 1 as the pose ID. Resolve their target
    # and profile references through the saved IDs, then use actual YOLO class
    # IDs everywhere at runtime. No source files or profile assignments change.
    target = saved_pose_classes[saved_target]
    raw_transitions = payload.get("transitions", [])
    if not isinstance(raw_transitions, list):
        raise ValueError("Manual profile assignments must be a list")
    transitions = []
    sources = set()
    for raw in raw_transitions:
        if not isinstance(raw, dict):
            raise ValueError("Each manual profile assignment must be an object")
        saved_start = _integer(raw.get("from_pose"), "Start pose", 0, 65535)
        saved_end = _integer(raw.get("to_pose"), "Destination pose", 0, 65535)
        if (
            saved_start not in saved_pose_classes
            or saved_start == saved_target
            or saved_end != saved_target
            or saved_start in sources
        ):
            raise ValueError("Assign at most one direct profile per non-target start pose")
        sources.add(saved_start)
        start = saved_pose_classes[saved_start]
        transitions.append(
            TransitionSpec(
                start,
                target,
                _resolve_optional(
                    source.parent, raw.get("pressure_profile"), "pressure_profile", {".json"}
                ),
                edge_id=f"manual:{start}->{target}",
            )
        )
    return PartDefinition(
        schema_version=MANUAL_SCHEMA_VERSION,
        part_name=name,
        model_path=_resolve(source.parent, payload.get("model_path"), "model_path", {".pt"}),
        poses=tuple(poses),
        target_pose=target,
        transitions=tuple(transitions),
        mesh_path=_resolve_optional(
            source.parent, payload.get("mesh_path"), "mesh_path", {".stl", ".obj"}
        ),
        source_path=source,
    )


def save_manual_definition(
    path: Path,
    *,
    part_name: str,
    model_path: Path,
    class_labels: dict[int, str],
    target_class_id: int,
    profiles_by_class: dict[int, Path | None],
    mesh_path: Path | None = None,
) -> PartDefinition:
    destination = Path(path).expanduser().resolve()
    for class_id in class_labels:
        _integer(class_id, "YOLO class ID", 0, 65534)
    _integer(target_class_id, "Target class ID", 0, 65534)
    if target_class_id not in class_labels:
        raise ValueError("Select a target from the configured YOLO classes")
    if set(profiles_by_class) - set(class_labels):
        raise ValueError("A profile refers to an unknown YOLO class")
    payload = {
        "schema_version": MANUAL_SCHEMA_VERSION,
        "part_name": part_name.strip(),
        "model_path": _portable_path(model_path, destination),
        "poses": [
            dict(id=key, label=label, model_class_id=key)
            for key, label in sorted(class_labels.items())
        ],
        "target_pose": target_class_id,
        "transitions": [
            dict(
                from_pose=key,
                to_pose=target_class_id,
                pressure_profile=_portable_path(profile, destination) if profile else None,
            )
            for key, profile in sorted(profiles_by_class.items())
            if key != target_class_id
        ],
    }
    if mesh_path is not None:
        payload["mesh_path"] = _portable_path(mesh_path, destination)
    definition = load_manual_definition(destination, payload)
    _atomic_yaml(destination, payload)
    return definition
