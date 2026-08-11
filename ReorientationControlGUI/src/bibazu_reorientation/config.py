from __future__ import annotations

import os
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from bibazu_reorientation.models import (
    PartDefinition,
    PoseDefinition,
    RoadmapReadiness,
    TransitionSpec,
)
from bibazu_reorientation.profiles import load_pressure_profile
from bibazu_reorientation.roadmap import (
    PoseRoadmap,
    load_pose_roadmap,
    load_stable_pose_roadmap,
)

LEGACY_SCHEMA_VERSION = 1
ROADMAP_SCHEMA_VERSION = 2


class RoadmapHashMismatchError(ValueError):
    def __init__(self, path: Path, expected: str, actual: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            "The roadmap has changed since the configuration was saved. "
            "Please use 'Re-import roadmap'."
        )


def _resolve(base: Path, value: Any, field: str, suffixes: set[str]) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.suffix.lower() not in suffixes:
        expected = "/".join(sorted(suffixes))
        raise ValueError(f"{field} must point to a {expected} file")
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    return path


def _resolve_optional(base: Path, value: Any, field: str, suffixes: set[str]) -> Path | None:
    if value in (None, ""):
        return None
    return _resolve(base, value, field, suffixes)


def _portable_path(target: Path, config_path: Path) -> str:
    target = target.expanduser().resolve()
    try:
        return Path(os.path.relpath(target, config_path.parent.resolve())).as_posix()
    except (OSError, ValueError):
        return str(target)


def _read_config(source: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("The part file must contain a YAML object")
    return payload


def load_part_definition(path: Path, *, accept_roadmap_change: bool = False) -> PartDefinition:
    source = Path(path).expanduser().resolve()
    payload = _read_config(source)
    version = int(payload.get("schema_version", 0))
    if version == LEGACY_SCHEMA_VERSION:
        return _load_v1(source, payload)
    if version == ROADMAP_SCHEMA_VERSION:
        return _load_v2(source, payload, accept_roadmap_change=accept_roadmap_change)
    raise ValueError(f"Unknown part schema version: {version}")


def _load_v1(source: Path, payload: dict[str, Any]) -> PartDefinition:
    part_name = str(payload.get("part_name", "")).strip()
    if not part_name:
        raise ValueError("part_name must not be empty")
    poses_raw = payload.get("poses")
    if not isinstance(poses_raw, list) or len(poses_raw) != 2:
        raise ValueError("V1 requires exactly two poses")
    poses = tuple(
        PoseDefinition(
            id=int(item["id"]), label=str(item["label"]), model_class_id=int(item["model_class_id"])
        )
        for item in poses_raw
    )
    if {pose.id for pose in poses} != {1, 2}:
        raise ValueError("V1 requires pose IDs 1 and 2")
    if {pose.model_class_id for pose in poses} != {0, 1}:
        raise ValueError("V1 requires YOLO classes 0 and 1")
    expected_labels = {0: "pose1", 1: "pose2"}
    for pose in poses:
        normalized = "".join(
            character for character in pose.label.casefold() if character.isalnum()
        )
        if normalized != expected_labels[pose.model_class_id]:
            raise ValueError("Class 0 must be named 'Pose 1' and class 1 must be named 'Pose 2'")
    target_pose = int(payload.get("target_pose", 0))
    if target_pose not in {1, 2}:
        raise ValueError("V1 supports Pose 1 or Pose 2 as the target")
    transitions_raw = payload.get("transitions")
    if not isinstance(transitions_raw, list) or len(transitions_raw) != 1:
        raise ValueError("V1 requires exactly one transition")
    raw = transitions_raw[0]
    transition = TransitionSpec(
        from_pose=int(raw["from_pose"]),
        to_pose=int(raw["to_pose"]),
        pressure_profile=_resolve(
            source.parent, raw.get("pressure_profile"), "pressure_profile", {".json"}
        ),
    )
    expected_transition = (3 - target_pose, target_pose)
    if (transition.from_pose, transition.to_pose) != expected_transition:
        raise ValueError(
            f"For target pose {target_pose}, V1 requires exactly the transition from Pose "
            f"{expected_transition[0]} to Pose {target_pose}"
        )
    roadmap = _resolve_optional(
        source.parent, payload.get("roadmap_path"), "roadmap_path", {".yaml", ".yml", ".json"}
    )
    roadmap_pose = payload.get("target_roadmap_pose_id")
    if (roadmap is None) != (roadmap_pose is None):
        raise ValueError("roadmap_path and target_roadmap_pose_id must be provided together")
    if roadmap is not None:
        try:
            pose = load_stable_pose_roadmap(roadmap).pose(int(roadmap_pose))
        except KeyError as exc:
            raise ValueError(f"Roadmap pose {roadmap_pose} is unknown") from exc
        if not pose.is_robust:
            raise ValueError(f"Roadmap pose {roadmap_pose} is not robust")
    return PartDefinition(
        schema_version=1,
        part_name=part_name,
        model_path=_resolve(source.parent, payload.get("model_path"), "model_path", {".pt"}),
        poses=poses,
        target_pose=target_pose,
        transitions=(transition,),
        mesh_path=_resolve_optional(
            source.parent, payload.get("mesh_path"), "mesh_path", {".stl", ".obj"}
        ),
        source_path=source,
        roadmap_path=roadmap,
        target_roadmap_pose_id=None if roadmap_pose is None else int(roadmap_pose),
    )


def _load_v2(
    source: Path, payload: dict[str, Any], *, accept_roadmap_change: bool
) -> PartDefinition:
    roadmap_path = _resolve(
        source.parent, payload.get("roadmap_path"), "roadmap_path", {".yaml", ".yml", ".json"}
    )
    roadmap = load_pose_roadmap(roadmap_path)
    expected_hash = str(payload.get("roadmap_sha256", "")).lower()
    if len(expected_hash) != 64:
        raise ValueError("roadmap_sha256 is missing or invalid")
    changed = expected_hash != roadmap.sha256
    if changed and not accept_roadmap_change:
        raise RoadmapHashMismatchError(roadmap_path, expected_hash, roadmap.sha256)
    part_name = str(payload.get("part_name", "")).strip()
    if not part_name:
        raise ValueError("part_name must not be empty")
    model_path = _resolve(source.parent, payload.get("model_path"), "model_path", {".pt"})
    mesh_path = _resolve(source.parent, payload.get("mesh_path"), "mesh_path", {".stl", ".obj"})
    raw_poses = payload.get("poses")
    if not isinstance(raw_poses, list):
        raise ValueError("poses must be a list")
    mappings: dict[int, PoseDefinition] = {}
    class_ids: set[int] = set()
    for raw in raw_poses:
        if not isinstance(raw, dict):
            raise ValueError("Each pose mapping must be an object")
        pose_id = int(raw["id"])
        if pose_id in mappings:
            raise ValueError(f"Duplicate pose mapping: {pose_id}")
        class_id = int(raw["model_class_id"])
        if class_id < 0 or class_id in class_ids:
            raise ValueError("YOLO model classes must be unique and non-negative")
        mappings[pose_id] = PoseDefinition(
            pose_id, str(raw.get("label", f"Pose {pose_id}")), class_id
        )
        class_ids.add(class_id)
    robust_ids = {pose.pose_id for pose in roadmap.robust_poses}
    # On explicit re-import removed IDs disappear. Newly introduced IDs remain absent so
    # the setup dialog can list them as incomplete before it writes a new valid snapshot.
    previous_pose_ids = set(mappings)
    unknown = previous_pose_ids - robust_ids
    if unknown and not changed:
        raise ValueError(f"Mapping contains unknown robust poses: {sorted(unknown)}")
    mappings = {pose_id: mapping for pose_id, mapping in mappings.items() if pose_id in robust_ids}
    if not changed and set(mappings) != robust_ids:
        raise ValueError("Every robust roadmap pose requires exactly one YOLO model class")
    target_pose = int(payload.get("target_pose", 0))
    if target_pose not in robust_ids and not changed:
        raise ValueError("target_pose must be a robust roadmap pose")
    raw_profiles = payload.get("transition_profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError("transition_profiles must map edge_id to a path or null")
    valid_edges = {edge.edge_id: edge for edge in roadmap.profile_transitions}
    previous_edge_ids = set(raw_profiles)
    if not changed and set(raw_profiles) != set(valid_edges):
        raise ValueError("transition_profiles must contain every actuated robust-to-robust edge")
    transitions: list[TransitionSpec] = []
    for edge_id, edge in valid_edges.items():
        raw_profile = raw_profiles.get(edge_id)
        profile_path = _resolve_optional(
            source.parent, raw_profile, f"transition_profiles[{edge_id}]", {".json"}
        )
        if profile_path is not None:
            load_pressure_profile(profile_path, require_transition=False)
        transitions.append(
            TransitionSpec(
                from_pose=edge.from_pose,
                to_pose=edge.to_pose,
                pressure_profile=profile_path,
                edge_id=edge.edge_id,
                transition_kind=edge.transition_kind,
                actuation=edge.actuation,
                signed_angle_deg=edge.signed_angle_deg,
                geometric_score=edge.geometric_score,
                experimental_status=edge.experimental_status,
            )
        )
    return PartDefinition(
        schema_version=2,
        part_name=part_name,
        model_path=model_path,
        poses=tuple(mappings[pose_id] for pose_id in sorted(mappings)),
        target_pose=target_pose,
        transitions=tuple(transitions),
        mesh_path=mesh_path,
        source_path=source,
        roadmap_path=roadmap_path,
        target_roadmap_pose_id=target_pose,
        roadmap_sha256=roadmap.sha256 if changed else expected_hash,
        roadmap_changed=changed,
        roadmap_added_pose_ids=tuple(sorted(robust_ids - previous_pose_ids)) if changed else (),
        roadmap_removed_pose_ids=tuple(sorted(previous_pose_ids - robust_ids)) if changed else (),
        roadmap_added_edge_ids=tuple(sorted(set(valid_edges) - previous_edge_ids))
        if changed
        else (),
        roadmap_removed_edge_ids=tuple(sorted(previous_edge_ids - set(valid_edges)))
        if changed
        else (),
    )


def save_part_definition(
    path: Path,
    *,
    part_name: str,
    model_path: Path,
    pressure_profile: Path,
    target_pose: int = 1,
    mesh_path: Path | None = None,
    roadmap_path: Path | None = None,
    target_roadmap_pose_id: int | None = None,
) -> PartDefinition:
    """Save the unchanged executable two-pose schema v1."""
    destination = Path(path).expanduser().resolve()
    model = _resolve(destination.parent, str(model_path), "model_path", {".pt"})
    profile = _resolve(destination.parent, str(pressure_profile), "pressure_profile", {".json"})
    if not part_name.strip():
        raise ValueError("The part name must not be empty")
    if target_pose not in {1, 2}:
        raise ValueError("The target pose must be Pose 1 or Pose 2")
    mesh = (
        None
        if mesh_path is None or not str(mesh_path).strip()
        else _resolve(destination.parent, str(mesh_path), "mesh_path", {".stl", ".obj"})
    )
    roadmap = (
        None
        if roadmap_path is None
        else _resolve(
            destination.parent, str(roadmap_path), "roadmap_path", {".yaml", ".yml", ".json"}
        )
    )
    if (roadmap is None) != (target_roadmap_pose_id is None):
        raise ValueError("The roadmap and physical target pose must be selected together")
    if roadmap is not None:
        try:
            selected_roadmap_pose = load_stable_pose_roadmap(roadmap).pose(
                int(target_roadmap_pose_id)
            )
        except KeyError as exc:
            raise ValueError(
                f"Roadmap pose {target_roadmap_pose_id} is not catalogued as stable"
            ) from exc
        if not selected_roadmap_pose.is_robust:
            raise ValueError(f"Roadmap pose {target_roadmap_pose_id} is not robust")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "part_name": part_name.strip(),
        "model_path": _portable_path(model, destination),
        "poses": [
            {"id": 1, "label": "Pose 1", "model_class_id": 0},
            {"id": 2, "label": "Pose 2", "model_class_id": 1},
        ],
        "target_pose": target_pose,
        "transitions": [
            {
                "from_pose": 3 - target_pose,
                "to_pose": target_pose,
                "pressure_profile": _portable_path(profile, destination),
            }
        ],
    }
    if mesh is not None:
        payload["mesh_path"] = _portable_path(mesh, destination)
    if roadmap is not None:
        payload["roadmap_path"] = _portable_path(roadmap, destination)
        payload["target_roadmap_pose_id"] = int(target_roadmap_pose_id)
    _atomic_yaml(destination, payload)
    return load_part_definition(destination)


def save_roadmap_part_definition(
    path: Path,
    *,
    roadmap_path: Path,
    part_name: str,
    mesh_path: Path,
    model_path: Path,
    pose_class_mapping: Mapping[int, int],
    target_pose: int,
    transition_profiles: Mapping[str, Path | None],
) -> PartDefinition:
    destination = Path(path).expanduser().resolve()
    roadmap = load_pose_roadmap(roadmap_path)
    mesh = _resolve(destination.parent, str(mesh_path), "mesh_path", {".stl", ".obj"})
    model = _resolve(destination.parent, str(model_path), "model_path", {".pt"})
    if not part_name.strip():
        raise ValueError("The part name must not be empty")
    robust_ids = {pose.pose_id for pose in roadmap.robust_poses}
    if set(pose_class_mapping) != robust_ids:
        raise ValueError("Every robust roadmap pose requires an explicit model class")
    class_ids = [int(value) for value in pose_class_mapping.values()]
    if any(value < 0 for value in class_ids) or len(set(class_ids)) != len(class_ids):
        raise ValueError("Model classes must be unique and non-negative")
    if target_pose not in robust_ids:
        raise ValueError("The target pose must be robust")
    edge_ids = {edge.edge_id for edge in roadmap.profile_transitions}
    if set(transition_profiles) != edge_ids:
        raise ValueError("A table row is required for every actuated robust-to-robust edge")
    serialized_profiles: dict[str, str | None] = {}
    for edge_id in sorted(edge_ids):
        profile = transition_profiles[edge_id]
        if profile is None or not str(profile).strip():
            serialized_profiles[edge_id] = None
            continue
        resolved = _resolve(destination.parent, str(profile), f"Profile {edge_id}", {".json"})
        load_pressure_profile(resolved, require_transition=False)
        serialized_profiles[edge_id] = _portable_path(resolved, destination)
    payload = {
        "schema_version": 2,
        "roadmap_path": _portable_path(roadmap.path, destination),
        "roadmap_sha256": roadmap.sha256,
        "part_name": part_name.strip(),
        "mesh_path": _portable_path(mesh, destination),
        "model_path": _portable_path(model, destination),
        "poses": [
            {
                "id": pose.pose_id,
                "label": f"Pose {pose.pose_id}",
                "model_class_id": int(pose_class_mapping[pose.pose_id]),
            }
            for pose in roadmap.robust_poses
        ],
        "target_pose": int(target_pose),
        "transition_profiles": serialized_profiles,
    }
    _atomic_yaml(destination, payload)
    return load_part_definition(destination)


def _atomic_yaml(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    temporary.replace(destination)


def roadmap_readiness(
    definition: PartDefinition, roadmap: PoseRoadmap | None = None
) -> RoadmapReadiness:
    if definition.schema_version != 2 or definition.roadmap_path is None:
        raise ValueError("Readiness is only available for roadmap configurations")
    roadmap = roadmap or load_pose_roadmap(definition.roadmap_path)
    mapped = {pose.id for pose in definition.poses}
    robust = {pose.pose_id for pose in roadmap.robust_poses}
    available_edges = [edge for edge in definition.transitions if edge.pressure_profile is not None]
    reverse: dict[int, set[int]] = {}
    for edge in available_edges:
        reverse.setdefault(edge.to_pose, set()).add(edge.from_pose)
    reachable = {definition.target_pose}
    queue = deque([definition.target_pose])
    while queue:
        for predecessor in reverse.get(queue.popleft(), set()):
            if predecessor not in reachable:
                reachable.add(predecessor)
                queue.append(predecessor)
    return RoadmapReadiness(
        missing_profile_edge_ids=tuple(
            edge.edge_id for edge in definition.transitions if edge.pressure_profile is None
        ),
        reachable_pose_ids=tuple(sorted(reachable & robust)),
        unreachable_pose_ids=tuple(sorted(robust - reachable)),
        unmapped_pose_ids=tuple(sorted(robust - mapped)),
        roadmap_hash_matches=definition.roadmap_sha256 == roadmap.sha256
        and not definition.roadmap_changed,
        name_differs=definition.part_name != roadmap.part_name,
        mesh_differs=definition.mesh_path != roadmap.mesh_path,
    )


class TransitionResolver:
    def __init__(self, definition: PartDefinition) -> None:
        self.definition = definition

    def plan(
        self,
        start_pose: int,
        target_pose: int | None = None,
        *,
        max_transitions: int = 2,
    ) -> tuple[TransitionSpec, ...]:
        target = self.definition.target_pose if target_pose is None else target_pose
        if start_pose == target:
            return ()
        if max_transitions not in {1, 2}:
            raise ValueError("Only direct paths or paths with one intermediate pose are supported")
        available = tuple(
            transition
            for transition in self.definition.transitions
            if transition.pressure_profile is not None
        )
        direct = tuple(
            transition
            for transition in available
            if transition.from_pose == start_pose and transition.to_pose == target
        )
        if direct:
            if len(direct) != 1:
                choices = ", ".join(
                    f"{transition.edge_id} ({transition.actuation or 'actuated'})"
                    for transition in direct
                )
                raise ValueError(
                    f"Transition {start_pose} → {target} is ambiguous: {choices}. "
                    "Assign a pressure profile to exactly one of these parallel edges."
                )
            return direct
        if max_transitions == 1:
            raise ValueError(f"No transition {start_pose} → {target} is configured")
        paths = tuple(
            (first, second)
            for first in available
            if first.from_pose == start_pose and first.to_pose not in {start_pose, target}
            for second in available
            if second.from_pose == first.to_pose and second.to_pose == target
        )
        if len(paths) != 1:
            reason = "No" if not paths else "No unique"
            raise ValueError(
                f"{reason} path {start_pose} → … → {target} with at most one "
                "intermediate pose is configured"
            )
        return paths[0]
