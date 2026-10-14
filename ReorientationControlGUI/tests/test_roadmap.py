from __future__ import annotations

import json
from pathlib import Path

import pytest
from local_roadmap_fixture import fixture_directory

from bibazu_reorientation.roadmap import load_pose_roadmap

ROADMAP_DIR = (
    fixture_directory()
)


def test_df1a_yaml_and_json_normalize_identically() -> None:
    yaml_roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.yaml")
    json_roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.json")
    assert len(yaml_roadmap.poses) == len(json_roadmap.poses) == 15
    assert len(yaml_roadmap.transitions) == len(json_roadmap.transitions) == 67
    assert [pose.pose_id for pose in yaml_roadmap.robust_poses] == [0, 1, 2, 3]
    assert [edge.edge_id for edge in yaml_roadmap.profile_transitions] == [
        "a17:1->2:wall_main_neg_x",
        "a18:1->3:free_z",
        "a29:0->2:free_y",
        "a30:0->3:floor_main_pos_x",
        "a40:2->0:free_y",
        "a51:3->1:free_z",
    ]
    multi = {edge.edge_id: edge for edge in yaml_roadmap.multi_reorientation_transitions}
    assert set(multi) == {
        "multi2:1->2->0",
        "multi2:0->3->1",
        "multi2:2->0->3",
        "multi2:3->1->2",
        "multi3:1->2->0->3",
        "multi3:0->3->1->2",
        "multi3:2->0->3->1",
        "multi3:3->1->2->0",
    }
    requested = multi["multi2:3->1->2"]
    assert requested.transition_kind == "multi_reorientation"
    assert requested.flip_count == 2
    assert requested.via_pose_ids == (1,)
    assert requested.component_edge_ids == (
        "a51:3->1:free_z",
        "a17:1->2:wall_main_neg_x",
    )
    assert all(pose.thumbnail_png for pose in yaml_roadmap.poses)
    assert {
        (edge.edge_id, edge.from_pose, edge.to_pose, edge.transition_kind)
        for edge in yaml_roadmap.transitions
    } == {
        (edge.edge_id, edge.from_pose, edge.to_pose, edge.transition_kind)
        for edge in json_roadmap.transitions
    }


def _mutated_json(tmp_path: Path, mutate) -> Path:
    source = ROADMAP_DIR / "Df1a_roadmap.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["source"] = str(load_pose_roadmap(source).mesh_path)
    mutate(payload)
    destination = tmp_path / "roadmap.json"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p.update(schema_version=99), "schema version"),
        (lambda p: p["nodes"].append(dict(p["nodes"][0])), "duplicate pose"),
        (lambda p: p["edges"][0].update(target=123456), "unknown pose"),
        (lambda p: p["edges"][0].update(directed=False), "not directed"),
        (lambda p: p["edges"][0].update(transition_kind="magic"), "unknown type"),
        (lambda p: p["nodes"][0].update(representative_quaternion_xyzw=[0, 0, 0, 0]), "quaternion"),
        (lambda p: p.update(source="missing.stl"), "CAD file not found"),
    ],
)
def test_rejects_invalid_roadmaps(tmp_path: Path, mutate, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_pose_roadmap(_mutated_json(tmp_path, mutate))


def test_passive_and_parallel_edges_remain_distinct() -> None:
    roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.yaml")
    passive = [edge for edge in roadmap.transitions if edge.transition_kind == "passive_tip"]
    assert len(passive) == 14
    grouped: dict[tuple[int, int], list[str]] = {}
    for edge in roadmap.transitions:
        grouped.setdefault((edge.from_pose, edge.to_pose), []).append(edge.edge_id)
    assert any(len(edge_ids) > 1 for edge_ids in grouped.values())
    assert all(len(edge_ids) == len(set(edge_ids)) for edge_ids in grouped.values())
