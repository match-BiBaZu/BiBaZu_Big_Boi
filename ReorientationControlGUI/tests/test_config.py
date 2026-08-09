from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibazu_reorientation.config import (
    TransitionResolver,
    load_part_definition,
    save_part_definition,
)


def test_yaml_roundtrip_and_relative_paths(tmp_path: Path) -> None:
    model = tmp_path / "models" / "best.pt"
    profile = tmp_path / "profiles" / "2-to-1.json"
    model.parent.mkdir()
    profile.parent.mkdir()
    model.write_bytes(b"model")
    profile.write_text(json.dumps({"version": 1, "arrays": []}), encoding="utf-8")
    target = tmp_path / "part.yaml"
    saved = save_part_definition(
        target, part_name="Teil A", model_path=model, pressure_profile=profile
    )
    assert saved == load_part_definition(target)
    text = target.read_text(encoding="utf-8")
    assert "models/best.pt" in text
    assert TransitionResolver(saved).plan(1) == ()
    assert TransitionResolver(saved).plan(2)[0].pressure_profile == profile


def test_target_pose_two_and_mesh_path(tmp_path: Path) -> None:
    model = tmp_path / "best.pt"
    profile = tmp_path / "1-to-2.json"
    mesh = tmp_path / "part.STL"
    model.write_bytes(b"model")
    profile.write_text(json.dumps({"version": 1, "arrays": []}), encoding="utf-8")
    mesh.write_bytes(b"solid part\nendsolid part\n")

    saved = save_part_definition(
        tmp_path / "part.yaml",
        part_name="Teil B",
        model_path=model,
        pressure_profile=profile,
        target_pose=2,
        mesh_path=mesh,
    )

    assert saved.target_pose == 2
    assert saved.mesh_path == mesh
    assert (saved.transitions[0].from_pose, saved.transitions[0].to_pose) == (1, 2)
    assert TransitionResolver(saved).plan(2) == ()
    assert TransitionResolver(saved).plan(1)[0].pressure_profile == profile


def test_rejects_wrong_pose_mapping(tmp_path: Path) -> None:
    (tmp_path / "best.pt").write_bytes(b"x")
    (tmp_path / "p.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "part.yaml"
    source.write_text(
        """schema_version: 1
part_name: x
model_path: best.pt
poses:
  - {id: 1, label: Pose 2, model_class_id: 0}
  - {id: 2, label: Pose 1, model_class_id: 1}
target_pose: 1
transitions:
  - {from_pose: 2, to_pose: 1, pressure_profile: p.json}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Klasse 0"):
        load_part_definition(source)
