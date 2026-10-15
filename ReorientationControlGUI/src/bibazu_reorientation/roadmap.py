"""Normalize and validate BiBaZu pose-roadmap YAML and JSON exports."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from bibazu_reorientation.export_records import (
    read_export,
    validate_transfer,
    with_consumer_sidecar,
)
from bibazu_reorientation.resources import external_roadmap_path, local_geometry_path

ALLOWED_STABILITIES = {"robust", "metastable"}
ALLOWED_TRANSITION_KINDS = {"actuated", "passive_tip", "multi_reorientation", "member_transfer"}


@dataclass(frozen=True, slots=True)
class RoadmapPose:
    pose_id: int
    equivalent_pose_ids: tuple[int, ...]
    stability: str
    quaternion_xyzw: tuple[float, float, float, float]
    floor_contact: str
    wall_contact: str
    rocking_barrier_mm: float | None
    cad_status: str
    thumbnail_png: bytes | None = None

    @property
    def is_robust(self) -> bool:
        return self.stability == "robust"


@dataclass(frozen=True, slots=True)
class RoadmapTransition:
    edge_id: str
    from_pose: int
    to_pose: int
    directed: bool
    transition_kind: str
    actuation: str
    signed_angle_deg: float | None
    geometric_score: float | None
    experimental_status: str
    flip_count: int = 1
    via_pose_ids: tuple[int, ...] = ()
    component_edge_ids: tuple[str, ...] = ()
    commands: tuple[dict, ...] = ()
    evidence_status: str = ""
    member_context: dict | None = None
    preview_segments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class PoseRoadmap:
    path: Path
    sha256: str
    format_name: str
    schema_version: int
    part_name: str
    mesh_path: Path | None
    cad_status: str
    poses: tuple[RoadmapPose, ...]
    transitions: tuple[RoadmapTransition, ...]
    member_states: tuple[dict, ...] = ()
    member_route_data: tuple[dict, ...] = ()
    representative_members: tuple[tuple[int, str], ...] = ()
    exported_choices: tuple[RoadmapTransition, ...] = ()
    execution_routes: tuple[dict, ...] = ()

    @cached_property
    def robust_poses(self) -> tuple[RoadmapPose, ...]:
        return tuple(pose for pose in self.poses if pose.is_robust)

    @cached_property
    def profile_transitions(self) -> tuple[RoadmapTransition, ...]:
        robust_ids = {pose.pose_id for pose in self.robust_poses}
        return tuple(
            edge
            for edge in self.transitions
            if edge.transition_kind == "actuated"
            and edge.from_pose in robust_ids
            and edge.to_pose in robust_ids
        )

    @cached_property
    def multi_reorientation_transitions(self) -> tuple[RoadmapTransition, ...]:
        """Calibration macros supplied by the external exporter."""
        return tuple(
            e
            for e in self.exported_choices
            if e.transition_kind == "multi_reorientation" and not e.edge_id.startswith("composite:")
        )

    @cached_property
    def composite_transitions(self) -> tuple[RoadmapTransition, ...]:
        return tuple(edge for edge in self.transitions if edge.edge_id.startswith("composite:"))

    @cached_property
    def member_transitions(self) -> tuple[RoadmapTransition, ...]:
        return tuple(e for e in self.transitions if e.transition_kind == "member_transfer")

    def route_options(self, start: int, target: int) -> tuple[RoadmapTransition, ...]:
        """Look up finished choices; legacy files expose explicit transitions only."""
        return tuple(
            e for e in self.calibratable_transitions if e.from_pose == start and e.to_pose == target
        )

    @cached_property
    def calibratable_transitions(self) -> tuple[RoadmapTransition, ...]:
        return tuple(
            {
                e.edge_id: e
                for e in (
                    self.profile_transitions
                    + self.composite_transitions
                    + self.member_transitions
                    + self.multi_reorientation_transitions
                    + self.exported_choices
                )
            }.values()
        )

    @cached_property
    def informational_transitions(self) -> tuple[RoadmapTransition, ...]:
        profile_ids = {
            edge.edge_id
            for edge in self.profile_transitions
            + self.composite_transitions
            + self.member_transitions
        }
        return tuple(edge for edge in self.transitions if edge.edge_id not in profile_ids)

    def pose(self, pose_id: int) -> RoadmapPose:
        for pose in self.poses:
            if pose.pose_id == pose_id:
                return pose
        raise KeyError(pose_id)

    def edge(self, edge_id: str) -> RoadmapTransition:
        for edge in self.calibratable_transitions + self.informational_transitions:
            if edge.edge_id == edge_id:
                return edge
        raise KeyError(edge_id)


# Kept for source compatibility with the first target-pose dialog implementation.
StableRoadmapPose = RoadmapPose
StablePoseRoadmap = PoseRoadmap


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _required_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _finite(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _quaternion(value: Any, label: str) -> tuple[float, float, float, float]:
    values = _required_list(value, label)
    if len(values) != 4:
        raise ValueError(f"{label} must contain exactly four values")
    quaternion = tuple(_finite(item, label) for item in values)
    norm = math.sqrt(sum(item * item for item in quaternion))
    if norm < 1e-9 or abs(norm - 1.0) > 1e-3:
        raise ValueError(f"{label} is not a normalized quaternion")
    return quaternion  # type: ignore[return-value]


def _decode_thumbnail(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        return None


def _mesh_path(source: Path, value: Any) -> Path | None:
    if value in (None, ""):
        return None
    path = local_geometry_path(str(value)).expanduser()
    if not path.is_absolute():
        path = source.parent / path
    return path.resolve()


def _normalize_json(source: Path, payload: dict[str, Any]) -> PoseRoadmap:
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unknown roadmap schema version")
    raw_nodes = _required_list(payload.get("nodes"), "nodes")
    poses: list[RoadmapPose] = []
    for raw in raw_nodes:
        node = _required_mapping(raw, "Roadmap node")
        pose_id = int(node["node_id"])
        stability = str(node.get("kind", ""))
        if stability not in ALLOWED_STABILITIES:
            raise ValueError(f"Pose {pose_id} has unknown stability '{stability}'")
        equivalents = tuple(
            int(item)
            for item in _required_list(node.get("pose_ids", [pose_id]), f"Pose {pose_id}.pose_ids")
        )
        poses.append(
            RoadmapPose(
                pose_id=pose_id,
                equivalent_pose_ids=equivalents,
                stability=stability,
                quaternion_xyzw=_quaternion(
                    node.get("representative_quaternion_xyzw"), f"Pose {pose_id}.quaternion"
                ),
                floor_contact=str(node.get("floor_contact_topology", "unknown")),
                wall_contact=str(node.get("wall_contact_topology", "unknown")),
                rocking_barrier_mm=(
                    None
                    if node.get("rocking_barrier_mm") is None
                    else _finite(node["rocking_barrier_mm"], f"Pose {pose_id}.rocking_barrier_mm")
                ),
                cad_status=str(node.get("cad_status", payload.get("geometry_status", "unknown"))),
                thumbnail_png=_decode_thumbnail(node.get("thumbnail_png_base64")),
            )
        )
    raw_edges = _required_list(payload.get("edges"), "edges")
    transitions = tuple(_json_edge(raw) for raw in raw_edges) + _composite_edges(payload, poses)
    mesh = _mesh_path(source, payload.get("source"))
    part_name = mesh.stem if mesh is not None else source.stem.replace("_roadmap", "")
    return _attach_members(
        _build(
            source,
            "internal_json",
            part_name,
            mesh,
            str(payload.get("geometry_status", "unknown")),
            poses,
            transitions,
        ),
        payload,
    )


def _exported_transition(record):
    commands = tuple(record.get("commands", []))
    return RoadmapTransition(
        edge_id=record["edge_id"],
        from_pose=record["source"],
        to_pose=record["target"],
        directed=record.get("directed", True),
        transition_kind=record.get("transition_kind", "member_transfer"),
        actuation=record.get("actuation", ""),
        signed_angle_deg=record.get("signed_angle_deg"),
        geometric_score=record.get("geometric_score"),
        experimental_status=record.get("experimental_status", "untested"),
        flip_count=record.get("actuation_count", len(commands) or 1),
        via_pose_ids=tuple(record.get("via_pose_ids", [])),
        component_edge_ids=tuple(record.get("component_edge_ids", [])),
        commands=commands,
        evidence_status=record.get("evidence_status", "geometry_only"),
        member_context=record if record.get("source_member_ids") else None,
        preview_segments=tuple(record.get("preview_segments", [])),
    )


def _attach_members(roadmap, payload):
    states, records, routes = read_export(payload)
    choices = tuple(_exported_transition(r) for r in records)
    by_id = {e.edge_id: e for e in choices}
    transitions = tuple(by_id.get(e.edge_id, e) for e in roadmap.transitions)
    existing = {e.edge_id for e in transitions}
    transitions += tuple(
        e for e in choices if e.transition_kind == "member_transfer" and e.edge_id not in existing
    )
    return replace(
        roadmap,
        transitions=transitions,
        member_states=states,
        exported_choices=choices,
        execution_routes=routes,
    )


def _composite_edges(payload, poses):
    known = {p.pose_id: p.quaternion_xyzw for p in poses}
    result = []
    for raw in _required_list(payload.get("composite_transfers", []), "composite_transfers"):
        commands = validate_transfer(raw, known)
        result.append(
            RoadmapTransition(
                str(raw["edge_id"]),
                int(raw["source"]),
                int(raw["target"]),
                True,
                "multi_reorientation",
                f"multiple_reorientation_{len(commands)}",
                None,
                None,
                "untested",
                len(commands),
                (),
                (),
                commands,
                "geometry_only",
            )
        )
    return tuple(result)


def _json_edge(raw: Any) -> RoadmapTransition:
    edge = _required_mapping(raw, "Roadmap edge")
    return RoadmapTransition(
        edge_id=str(edge.get("edge_id", "")).strip(),
        from_pose=int(edge["source"]),
        to_pose=int(edge["target"]),
        directed=bool(edge.get("directed", True)),
        transition_kind=str(edge.get("transition_kind", "")),
        actuation=str(edge.get("actuation", "")),
        signed_angle_deg=None
        if edge.get("signed_angle_deg") is None
        else _finite(edge["signed_angle_deg"], "signed_angle_deg"),
        geometric_score=None
        if edge.get("geometric_score") is None
        else _finite(edge["geometric_score"], "geometric_score"),
        experimental_status=str(edge.get("experimental_status", "untested")),
    )


def _normalize_handover(source: Path, payload: dict[str, Any]) -> PoseRoadmap:
    if payload.get("format") != "bibazu_pose_roadmap_handover":
        raise ValueError("Unknown roadmap format")
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unknown roadmap schema version")
    part = _required_mapping(payload.get("part"), "part")
    poses: list[RoadmapPose] = []
    for raw in _required_list(payload.get("poses"), "poses"):
        item = _required_mapping(raw, "Roadmap pose")
        pose_id = int(item["id"])
        stability = str(item.get("stability", ""))
        if stability not in ALLOWED_STABILITIES:
            raise ValueError(f"Pose {pose_id} has unknown stability '{stability}'")
        contacts = item.get("contacts", {})
        contacts = contacts if isinstance(contacts, dict) else {}
        poses.append(
            RoadmapPose(
                pose_id=pose_id,
                equivalent_pose_ids=tuple(
                    int(value)
                    for value in _required_list(
                        item.get("equivalent_catalog_pose_ids", [pose_id]),
                        f"Pose {pose_id}.equivalent_catalog_pose_ids",
                    )
                ),
                stability=stability,
                quaternion_xyzw=_quaternion(
                    item.get("orientation_quaternion_xyzw"), f"Pose {pose_id}.quaternion"
                ),
                floor_contact=str(contacts.get("floor", "unknown")),
                wall_contact=str(contacts.get("wall", "unknown")),
                rocking_barrier_mm=None
                if item.get("rocking_barrier_mm") is None
                else _finite(item["rocking_barrier_mm"], f"Pose {pose_id}.rocking_barrier_mm"),
                cad_status=str(item.get("cad_status", part.get("cad_status", "unknown"))),
            )
        )
    transitions = tuple(
        _handover_edge(raw) for raw in _required_list(payload.get("transitions"), "transitions")
    ) + _composite_edges(payload, poses)
    roadmap = _build(
        source,
        str(payload["format"]),
        str(part.get("name", "")).strip(),
        _mesh_path(source, part.get("mesh_source")),
        str(part.get("cad_status", "unknown")),
        poses,
        transitions,
    )
    return _enrich_thumbnails(_attach_members(roadmap, payload))


def _handover_edge(raw: Any) -> RoadmapTransition:
    edge = _required_mapping(raw, "Roadmap edge")
    action = edge.get("action", {})
    action = action if isinstance(action, dict) else {}
    geometry = edge.get("geometry", {})
    geometry = geometry if isinstance(geometry, dict) else {}
    experimental = edge.get("experimental", {})
    experimental = experimental if isinstance(experimental, dict) else {}
    angle = action.get("commanded_angle_deg", edge.get("signed_angle_deg"))
    score = geometry.get("geometric_score", edge.get("geometric_score"))
    return RoadmapTransition(
        edge_id=str(edge.get("id", "")).strip(),
        from_pose=int(edge["from_pose"]),
        to_pose=int(edge["to_pose"]),
        directed=bool(edge.get("directed", False)),
        transition_kind=str(edge.get("type", "")),
        actuation=str(action.get("name", edge.get("actuation", ""))),
        signed_angle_deg=None if angle is None else _finite(angle, "commanded_angle_deg"),
        geometric_score=None if score is None else _finite(score, "geometric_score"),
        experimental_status=str(
            experimental.get("status", edge.get("experimental_status", "untested"))
        ),
    )


def _build(
    source: Path,
    format_name: str,
    part_name: str,
    mesh: Path | None,
    cad_status: str,
    poses: list[RoadmapPose],
    transitions: tuple[RoadmapTransition, ...],
) -> PoseRoadmap:
    if not part_name:
        raise ValueError("The roadmap does not contain a part name")
    pose_ids = [pose.pose_id for pose in poses]
    if len(set(pose_ids)) != len(pose_ids):
        raise ValueError("The roadmap contains duplicate pose IDs")
    if not any(pose.is_robust for pose in poses):
        raise ValueError("The roadmap does not contain any robust poses")
    edge_ids = [edge.edge_id for edge in transitions]
    if any(not edge_id for edge_id in edge_ids) or len(set(edge_ids)) != len(edge_ids):
        raise ValueError("The roadmap contains empty or duplicate edge IDs")
    known = set(pose_ids)
    for edge in transitions:
        if edge.from_pose not in known or edge.to_pose not in known:
            raise ValueError(f"Edge {edge.edge_id} references an unknown pose")
        if not edge.directed:
            raise ValueError(f"Edge {edge.edge_id} is not directed")
        if edge.transition_kind not in ALLOWED_TRANSITION_KINDS:
            raise ValueError(f"Edge {edge.edge_id} has unknown type '{edge.transition_kind}'")
    if mesh is not None and not mesh.is_file():
        raise ValueError(f"CAD file not found: {mesh}")
    return PoseRoadmap(
        path=source,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        format_name=format_name,
        schema_version=1,
        part_name=part_name,
        mesh_path=mesh,
        cad_status=cad_status,
        poses=tuple(sorted(poses, key=lambda pose: pose.pose_id)),
        transitions=transitions,
    )


def _enrich_thumbnails(roadmap: PoseRoadmap) -> PoseRoadmap:
    sibling = roadmap.path.with_suffix(".json")
    if sibling == roadmap.path or not sibling.is_file():
        return roadmap
    try:
        candidate = load_pose_roadmap(sibling, enrich_thumbnails=False)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError):
        return roadmap
    if {pose.pose_id for pose in candidate.poses} != {pose.pose_id for pose in roadmap.poses} or {
        (edge.edge_id, edge.from_pose, edge.to_pose) for edge in candidate.transitions
    } != {(edge.edge_id, edge.from_pose, edge.to_pose) for edge in roadmap.transitions}:
        return roadmap
    images = {pose.pose_id: pose.thumbnail_png for pose in candidate.poses}
    return replace(
        roadmap,
        poses=tuple(
            replace(pose, thumbnail_png=images.get(pose.pose_id)) for pose in roadmap.poses
        ),
    )


def load_pose_roadmap(path: str | Path, *, enrich_thumbnails: bool = True) -> PoseRoadmap:
    source = external_roadmap_path(path)
    if source.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ValueError("Roadmap must be a .yaml, .yml, or .json file")
    if not source.is_file():
        raise ValueError(f"Pose roadmap not found: {source}")
    try:
        payload = (
            json.loads(source.read_text(encoding="utf-8"))
            if source.suffix.lower() == ".json"
            else yaml.load(
                source.read_text(encoding="utf-8"),
                Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
            )
        )
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid roadmap: {exc}") from exc
    payload = with_consumer_sidecar(_required_mapping(payload, "Roadmap"), source)
    if "nodes" in payload or "edges" in payload:
        return _normalize_json(source, payload)
    result = _normalize_handover(source, payload)
    return (
        result
        if enrich_thumbnails
        else replace(
            result, poses=tuple(replace(pose, thumbnail_png=None) for pose in result.poses)
        )
    )


def load_stable_pose_roadmap(path: str | Path) -> PoseRoadmap:
    """Load a roadmap for the legacy v1 target picker.

    Early internal exports only contained robust node thumbnails. Keep those usable
    for old two-pose configs, while schema-v2 always uses the strict full loader.
    """
    try:
        roadmap = load_pose_roadmap(path)
        return replace(roadmap, poses=roadmap.robust_poses)
    except ValueError as strict_error:
        source = external_roadmap_path(path)
        if source.suffix.lower() != ".json" or not source.is_file():
            raise strict_error
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise strict_error from None
        if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != 1:
            raise strict_error
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            raise strict_error
        poses: list[RoadmapPose] = []
        for raw in raw_nodes:
            if not isinstance(raw, dict) or raw.get("kind") != "robust":
                continue
            pose_id = int(raw["node_id"])
            equivalents = raw.get("pose_ids", [pose_id])
            if not isinstance(equivalents, list):
                raise strict_error
            poses.append(
                RoadmapPose(
                    pose_id=pose_id,
                    equivalent_pose_ids=tuple(int(value) for value in equivalents),
                    stability="robust",
                    quaternion_xyzw=(0.0, 0.0, 0.0, 1.0),
                    floor_contact=str(raw.get("floor_contact_topology", "unknown")),
                    wall_contact=str(raw.get("wall_contact_topology", "unknown")),
                    rocking_barrier_mm=None,
                    cad_status=str(
                        raw.get("cad_status", payload.get("geometry_status", "unknown"))
                    ),
                    thumbnail_png=_decode_thumbnail(raw.get("thumbnail_png_base64")),
                )
            )
        if not poses or len({pose.pose_id for pose in poses}) != len(poses):
            raise strict_error
        mesh_value = payload.get("source")
        mesh = _mesh_path(source, mesh_value) if mesh_value else None
        return PoseRoadmap(
            path=source,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            format_name="legacy_stable_json",
            schema_version=1,
            part_name=Path(str(mesh_value or source.stem.replace("_roadmap", ""))).stem,
            mesh_path=mesh,
            cad_status=str(payload.get("geometry_status", "unknown")),
            poses=tuple(sorted(poses, key=lambda pose: pose.pose_id)),
            transitions=(),
        )
