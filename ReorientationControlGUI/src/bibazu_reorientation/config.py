from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from bibazu_reorientation.export_records import source_coverage
from bibazu_reorientation.models import (
    PartDefinition,
    PoseDefinition,
    RoadmapReadiness,
    TransitionSpec,
)
from bibazu_reorientation.profiles import load_pressure_profile
from bibazu_reorientation.resources import external_roadmap_path, local_geometry_path
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
    path = local_geometry_path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = external_roadmap_path(path)
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
    required_edge_ids = (
        set()
        if roadmap.consumer_export_version == 2
        else {edge.edge_id for edge in roadmap.profile_transitions}
    )
    valid_edges = {edge.edge_id: edge for edge in roadmap.calibratable_transitions}
    previous_edge_ids = set(raw_profiles)
    previous_raw_edge_ids = {
        edge_id
        for edge_id in previous_edge_ids
        if not edge_id.startswith(("multi", "route:", "composite:", "member:", "member-route:"))
    }
    unknown_edge_ids = previous_edge_ids - set(valid_edges)
    unavailable_ids = {
        key
        for key in unknown_edge_ids
        if key.startswith(("multi", "route:", "composite:", "member:", "member-route:"))
    }
    if not changed and unknown_edge_ids - unavailable_ids:
        raise ValueError(f"transition_profiles contains unknown edges: {sorted(unknown_edge_ids)}")
    if not changed and not required_edge_ids.issubset(previous_edge_ids):
        raise ValueError("transition_profiles must contain every actuated robust-to-robust edge")
    transitions: list[TransitionSpec] = []
    coverage_map = payload.get("transition_source_coverage", {})
    if not isinstance(coverage_map, dict) or set(coverage_map) - set(valid_edges) - unavailable_ids:
        raise ValueError("transition_source_coverage references unknown transfers")
    unavailable_profiles = tuple(
        (key, _resolve_optional(source.parent, raw_profiles[key], f"Profile {key}", {".json"}))
        for key in sorted(unavailable_ids)
    )
    unavailable_coverage = tuple(
        (key, source_coverage(coverage_map[key], coverage_map[key]))
        for key in sorted(unavailable_ids & set(coverage_map))
    )
    group_coverage_map = payload.get("transition_group_coverage", {})
    if not isinstance(group_coverage_map, dict) or set(group_coverage_map) - set(valid_edges):
        raise ValueError("Group calibration references unknown transfers")
    for edge_id, edge in valid_edges.items():
        raw_profile = raw_profiles.get(edge_id)
        profile_path = _resolve_optional(
            source.parent, raw_profile, f"transition_profiles[{edge_id}]", {".json"}
        )
        if profile_path is not None:
            load_pressure_profile(profile_path, require_transition=False)
        allowed = {
            s["member_id"]
            for s in roadmap.member_states
            if s["retained"] and s["group_id"] == edge.from_pose
        }
        declared = coverage_map.get(edge_id)
        if declared is None and profile_path is not None:
            metadata = json.loads(profile_path.read_text(encoding="utf-8")).get(
                "roadmap_transition", {}
            )
            if isinstance(metadata, dict) and metadata.get("edge_id") == edge_id:
                declared = metadata.get("covered_source_member_ids", [])
        covered = (
            source_coverage(declared if declared is not None else [], allowed)
            if roadmap.member_states
            else ()
        )
        group_qualification = group_coverage_map.get(edge_id, "")
        if not group_qualification and profile_path is not None:
            metadata = json.loads(profile_path.read_text(encoding="utf-8")).get(
                "roadmap_transition", {}
            )
            if isinstance(metadata, dict) and metadata.get("edge_id") == edge_id:
                group_qualification = metadata.get("calibrated_source_group_key", "")
        if group_qualification and group_qualification != edge.source_group_key:
            if not changed:
                raise ValueError(
                    "Pressure profile group calibration does not match the exported source group"
                )
            group_qualification = ""
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
                flip_count=edge.flip_count,
                via_pose_ids=edge.via_pose_ids,
                component_edge_ids=edge.component_edge_ids,
                commands=edge.commands,
                evidence_status=edge.evidence_status,
                member_context=edge.member_context,
                covered_source_member_ids=covered,
                source_group_key=edge.source_group_key,
                calibrated_source_group_key=group_qualification,
            )
        )
    return PartDefinition(
        schema_version=2,
        part_name=part_name,
        model_path=model_path,
        poses=tuple(mappings[pose_id] for pose_id in sorted(mappings)),
        target_pose=target_pose,
        transitions=tuple(transitions),
        member_states=roadmap.member_states,
        execution_routes=roadmap.execution_routes,
        planning_semantics="group_union" if roadmap.consumer_export_version == 2 else "",
        unavailable_transition_profiles=unavailable_profiles,
        unavailable_transition_source_coverage=unavailable_coverage,
        mesh_path=mesh_path,
        source_path=source,
        roadmap_path=roadmap_path,
        target_roadmap_pose_id=target_pose,
        roadmap_sha256=roadmap.sha256 if changed else expected_hash,
        roadmap_changed=changed,
        roadmap_added_pose_ids=tuple(sorted(robust_ids - previous_pose_ids)) if changed else (),
        roadmap_removed_pose_ids=tuple(sorted(previous_pose_ids - robust_ids)) if changed else (),
        roadmap_added_edge_ids=tuple(sorted(required_edge_ids - previous_raw_edge_ids))
        if changed
        else (),
        roadmap_removed_edge_ids=tuple(sorted(previous_raw_edge_ids - required_edge_ids))
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
    transition_source_coverage: Mapping[str, tuple[str, ...]] | None = None,
    transition_group_coverage: Mapping[str, str] | None = None,
    unavailable_transition_profiles: Mapping[str, Path | None] | None = None,
    unavailable_transition_source_coverage: Mapping[str, tuple[str, ...]] | None = None,
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
    required_edge_ids = (
        set()
        if roadmap.consumer_export_version == 2
        else {edge.edge_id for edge in roadmap.profile_transitions}
    )
    edge_ids = {edge.edge_id for edge in roadmap.calibratable_transitions}
    if not required_edge_ids.issubset(transition_profiles) or not set(transition_profiles).issubset(
        edge_ids
    ):
        raise ValueError("A table row is required for every actuated robust-to-robust edge")
    serialized_profiles: dict[str, str | None] = {}
    for edge_id in sorted(edge_ids):
        profile = transition_profiles.get(edge_id)
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
        "transition_group_coverage": dict(transition_group_coverage or {}),
        "transition_source_coverage": {
            key: list(value) for key, value in (transition_source_coverage or {}).items()
        },
    }
    if set(transition_source_coverage or {}) - edge_ids:
        raise ValueError("Source coverage references an unknown transfer")
    for key, values in (transition_source_coverage or {}).items():
        edge = roadmap.edge(key)
        allowed = {
            s["member_id"]
            for s in roadmap.member_states
            if s["retained"] and s["group_id"] == edge.from_pose
        }
        source_coverage(values, allowed)
    for key, group_key in (transition_group_coverage or {}).items():
        if key not in edge_ids or not group_key or roadmap.edge(key).source_group_key != group_key:
            raise ValueError("Group calibration does not match the exported transfer/source group")
    # Preserve saved assignments missing from older exports without adding executable
    # transitions, deriving endpoints, or manufacturing a route in the consumer.
    unavailable = unavailable_transition_profiles or {}
    for key, profile in unavailable.items():
        if key in edge_ids or not key.startswith(
            ("multi", "route:", "composite:", "member:", "member-route:")
        ):
            raise ValueError("Invalid unavailable route assignment")
        payload["transition_profiles"][key] = (
            None if profile is None else _portable_path(Path(profile), destination)
        )
    for key, values in (unavailable_transition_source_coverage or {}).items():
        if key not in unavailable:
            raise ValueError("Unavailable coverage has no saved route assignment")
        payload["transition_source_coverage"][key] = list(source_coverage(values, values))
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
    reachable = {definition.target_pose}
    for pose_id in robust:
        try:
            TransitionResolver(definition).plan(pose_id)
            reachable.add(pose_id)
        except ValueError:
            pass
    return RoadmapReadiness(
        missing_profile_edge_ids=tuple(
            edge.edge_id
            for edge in definition.transitions
            if edge.pressure_profile is None and edge.transition_kind != "multi_reorientation"
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
    """Select a finished exported route with assigned pressure profiles and coverage."""

    def __init__(self, definition: PartDefinition) -> None:
        self.definition = definition

    def plan(
        self, start_pose: int, target_pose: int | None = None, *, max_transitions: int | None = None
    ) -> tuple[TransitionSpec, ...]:
        target = self.definition.target_pose if target_pose is None else target_pose
        if start_pose == target:
            return ()
        if max_transitions is not None and (
            isinstance(max_transitions, bool)
            or not isinstance(max_transitions, int)
            or max_transitions < 1
        ):
            raise ValueError("max_transitions must be a positive integer")
        by_id = {edge.edge_id: edge for edge in self.definition.transitions}
        groups = {}
        for state in self.definition.member_states:
            if state["retained"]:
                groups.setdefault(state["group_id"], set()).add(state["member_id"])
        candidates = []
        routes = self.definition.execution_routes
        # Legacy exports/configs can still execute an explicitly assigned direct
        # transition. They never acquire a synthesized multi-transfer route here.
        if not routes and self.definition.planning_semantics != "group_union":
            routes = tuple(
                dict(
                    source=e.from_pose,
                    target=e.to_pose,
                    profile_edge_ids=[e.edge_id],
                    steps=[
                        dict(
                            edge_id=e.edge_id,
                            required_source_member_ids=sorted(groups.get(e.from_pose, ())),
                        )
                    ],
                    priority=[0 if e.transition_kind != "multi_reorientation" else 1, 1],
                )
                for e in self.definition.transitions
            )
        for record in routes:
            if record["source"] != start_pose or record["target"] != target:
                continue
            keys = record["profile_edge_ids"]
            if max_transitions is not None and len(keys) > max_transitions:
                continue
            if any(key not in by_id or by_id[key].pressure_profile is None for key in keys):
                continue
            if record.get("group_semantics"):
                if any(
                    not step.get("required_source_group_key")
                    or by_id[step["edge_id"]].source_group_key != step["required_source_group_key"]
                    or by_id[step["edge_id"]].calibrated_source_group_key
                    != step["required_source_group_key"]
                    for step in record["steps"]
                ):
                    continue
            elif any(
                not set(step.get("required_source_member_ids", ())).issubset(
                    by_id[step["edge_id"]].covered_source_member_ids
                )
                for step in record["steps"]
            ):
                continue
            rank = tuple(record["priority"])
            if record.get("group_semantics"):
                rank += (record["tie_breaker"],)
            candidates.append((rank, tuple(by_id[key] for key in keys)))
        if not candidates:
            raise ValueError(
                f"No path {start_pose} → {target}: no exported route has all required "
                "pressure profiles and source-group/member coverage. Calibrate an exported "
                "choice or upgrade the roadmap offline."
            )
        priority = min(item[0] for item in candidates)
        paths = {
            tuple(e.edge_id for e in path): path for rank, path in candidates if rank == priority
        }
        if len(paths) != 1:
            raise ValueError(
                f"No unique path {start_pose} → {target}: exported choices are ambiguous. "
                "Assign profiles to exactly one preferred option."
            )
        return next(iter(paths.values()))
