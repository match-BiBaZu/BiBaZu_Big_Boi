from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibazu_reorientation.roadmap import load_pose_roadmap

ROADMAP_DIR = (
    Path(__file__).resolve().parents[3]
    / "bibazu_geometry_to_pose"
    / "Poses_Found_Robust"
    / "Df1a_roadmap_provisional"
)


def test_df1a_yaml_and_json_normalize_identically() -> None:
    yaml_roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.yaml")
    json_roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.json")
    assert len(yaml_roadmap.poses) == len(json_roadmap.poses) == 11
    assert len(yaml_roadmap.transitions) == len(json_roadmap.transitions) == 23
    assert [pose.pose_id for pose in yaml_roadmap.robust_poses] == [9, 24, 35, 60]
    assert [edge.edge_id for edge in yaml_roadmap.profile_transitions] == [
        "a0:9->35:wall_main_neg_x",
        "a1:9->60:free_z",
        "a2:24->35:free_y",
        "a3:24->60:floor_main_pos_x",
        "a4:35->24:free_y",
        "a15:60->9:free_z",
    ]
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
    mutate(payload)
    destination = tmp_path / "roadmap.json"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p.update(schema_version=99), "Schemaversion"),
        (lambda p: p["nodes"].append(dict(p["nodes"][0])), "doppelte Pose"),
        (lambda p: p["edges"][0].update(target=123456), "unbekannte Pose"),
        (lambda p: p["edges"][0].update(directed=False), "nicht gerichtet"),
        (lambda p: p["edges"][0].update(transition_kind="magic"), "unbekannten Typ"),
        (lambda p: p["nodes"][0].update(representative_quaternion_xyzw=[0, 0, 0, 0]), "Quaternion"),
        (lambda p: p.update(source="missing.stl"), "CAD-Datei nicht gefunden"),
    ],
)
def test_rejects_invalid_roadmaps(tmp_path: Path, mutate, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_pose_roadmap(_mutated_json(tmp_path, mutate))


def test_passive_and_parallel_edges_remain_distinct() -> None:
    roadmap = load_pose_roadmap(ROADMAP_DIR / "Df1a_roadmap.yaml")
    passive = [edge for edge in roadmap.transitions if edge.transition_kind == "passive_tip"]
    assert len(passive) == 7
    grouped: dict[tuple[int, int], list[str]] = {}
    for edge in roadmap.transitions:
        grouped.setdefault((edge.from_pose, edge.to_pose), []).append(edge.edge_id)
    assert any(len(edge_ids) > 1 for edge_ids in grouped.values())
    assert all(len(edge_ids) == len(set(edge_ids)) for edge_ids in grouped.values())
