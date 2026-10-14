from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from local_roadmap_fixture import fixture_directory

from bibazu_reorientation.config import (
    RoadmapHashMismatchError,
    load_part_definition,
    roadmap_readiness,
    save_roadmap_part_definition,
)
from bibazu_reorientation.roadmap import load_pose_roadmap

ROADMAP_DIR = (
    fixture_directory()
)


def _save(tmp_path: Path, roadmap_path: Path | None = None):
    roadmap_path = roadmap_path or ROADMAP_DIR / "Df1a_roadmap.yaml"
    roadmap = load_pose_roadmap(roadmap_path)
    model = tmp_path / "best.pt"
    model.write_bytes(b"model")
    return save_roadmap_part_definition(
        tmp_path / "part.yaml",
        roadmap_path=roadmap_path,
        part_name="Df1a edited",
        mesh_path=roadmap.mesh_path,
        model_path=model,
        pose_class_mapping={1: 3, 0: 7, 2: 8, 3: 12},
        target_pose=2,
        transition_profiles={edge.edge_id: None for edge in roadmap.profile_transitions},
    )


def test_v2_roundtrip_uses_portable_paths_and_optional_profiles(tmp_path: Path) -> None:
    loaded = _save(tmp_path)
    payload = yaml.safe_load((tmp_path / "part.yaml").read_text(encoding="utf-8"))
    assert loaded.schema_version == 2
    assert loaded.part_name == "Df1a edited"
    assert loaded.target_pose == 2
    assert {pose.id: pose.model_class_id for pose in loaded.poses} == {1: 3, 0: 7, 2: 8, 3: 12}
    assert len(loaded.transitions) == 14
    assert (
        len([edge for edge in loaded.transitions if edge.transition_kind == "multi_reorientation"])
        == 8
    )
    assert all(edge.pressure_profile is None for edge in loaded.transitions)
    assert not Path(payload["roadmap_path"]).is_absolute()
    assert not Path(payload["mesh_path"]).is_absolute()
    assert set(payload["transition_profiles"]) == {edge.edge_id for edge in loaded.transitions}


def test_hash_change_blocks_until_explicit_reimport(tmp_path: Path) -> None:
    source = ROADMAP_DIR / "Df1a_roadmap.yaml"
    copied = tmp_path / "Df1a_roadmap.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload["part"]["mesh_source"] = str(load_pose_roadmap(source).mesh_path)
    copied.write_text(yaml.safe_dump(payload), encoding="utf-8")
    loaded = _save(tmp_path, copied)
    copied.write_text(copied.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RoadmapHashMismatchError):
        load_part_definition(loaded.source_path)
    draft = load_part_definition(loaded.source_path, accept_roadmap_change=True)
    assert draft.roadmap_changed
    assert {pose.id for pose in draft.poses} == {1, 0, 2, 3}
    assert {edge.edge_id for edge in draft.transitions} == {
        edge.edge_id for edge in loaded.transitions
    }


def test_reimport_reports_added_and_removed_ids(tmp_path: Path) -> None:
    source = ROADMAP_DIR / "Df1a_roadmap.json"
    copied = tmp_path / "Df1a_roadmap.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["source"] = str(load_pose_roadmap(source).mesh_path)
    copied.write_text(json.dumps(payload), encoding="utf-8")
    loaded = _save(tmp_path, copied)
    payload = json.loads(copied.read_text(encoding="utf-8"))
    payload["nodes"] = [node for node in payload["nodes"] if node["node_id"] != 3]
    payload["edges"] = [
        edge for edge in payload["edges"] if edge["source"] != 3 and edge["target"] != 3
    ]
    new_node = dict(payload["nodes"][0])
    new_node.update(node_id=777, pose_ids=[777], thumbnail_png_base64=None)
    payload["nodes"].append(new_node)
    new_edge = dict(payload["edges"][0])
    new_edge.update(
        edge_id="new:777->2:free_z",
        source=777,
        target=2,
        transition_kind="actuated",
        actuation="free_z",
    )
    payload["edges"].append(new_edge)
    copied.write_text(json.dumps(payload), encoding="utf-8")

    draft = load_part_definition(loaded.source_path, accept_roadmap_change=True)
    assert draft.roadmap_added_pose_ids == (777,)
    assert draft.roadmap_removed_pose_ids == (3,)
    assert draft.roadmap_added_edge_ids == ("new:777->2:free_z",)
    assert set(draft.roadmap_removed_edge_ids) == {
        "a18:1->3:free_z",
        "a30:0->3:floor_main_pos_x",
        "a51:3->1:free_z",
    }
    assert {pose.id for pose in draft.poses} == {1, 0, 2}


def test_readiness_uses_only_assigned_directed_edges(tmp_path: Path) -> None:
    definition = _save(tmp_path)
    chosen = {"a17:1->2:wall_main_neg_x", "a29:0->2:free_y", "a51:3->1:free_z"}
    transitions = tuple(
        replace(edge, pressure_profile=Path("assigned.json") if edge.edge_id in chosen else None)
        for edge in definition.transitions
    )
    ready = roadmap_readiness(replace(definition, transitions=transitions))
    assert ready.reachable_pose_ids == (0, 1, 2, 3)
    assert not ready.unreachable_pose_ids
    assert len(ready.missing_profile_edge_ids) == 3
