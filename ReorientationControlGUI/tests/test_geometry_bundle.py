from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bibazu_reorientation.config import (
    RoadmapHashMismatchError,
    load_part_definition,
    save_roadmap_part_definition,
)
from bibazu_reorientation.mesh_preview import load_mesh_triangles
from bibazu_reorientation.resources import (
    cad_directory,
    geometry_directory,
    local_geometry_path,
    roadmap_directory,
)
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog
from bibazu_reorientation.ui.setup_dialog import SetupDialog


def test_cad_loads_after_bundle_relocation(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "independent" / "geometry"
    shutil.copytree(geometry_directory(), bundle)
    monkeypatch.chdir(tmp_path)
    triangles = load_mesh_triangles(bundle / "cad" / "Df1a.STL")
    assert triangles.shape[1:] == (3, 3)
    assert len(triangles)


def test_dialogs_read_shared_local_roadmaps_and_allow_an_override(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BIBAZU_ROADMAPS_DIR", raising=False)
    assert roadmap_directory().name == "BiBaZu_StableComponentenRoadmaps_grouped"
    roadmap = load_pose_roadmap(roadmap_directory() / "Qk1a" / "Qk1a_roadmap.json")
    assert roadmap.mesh_path.is_relative_to(roadmap_directory())
    assert roadmap.mesh_path.is_file()
    monkeypatch.setenv("BIBAZU_ROADMAPS_DIR", str(tmp_path))
    assert SetupDialog._roadmap_directory() == str(tmp_path)
    assert RoadmapSetupDialog._roadmap_directory() == str(tmp_path)
    assert not (geometry_directory() / "roadmaps").exists()


def test_legacy_geometry_path_only_resolves_an_exact_cad_copy() -> None:
    old = Path("Z:/archive/bibazu_geometry_to_pose/Werkstücke_STL_grob/Df1a.STL")
    assert local_geometry_path(old) == cad_directory() / "Df1a.STL"
    missing = Path("Z:/archive/bibazu_geometry_to_pose/Poses_Found_Robust/missing.yaml")
    assert local_geometry_path(missing) == missing


def test_external_roadmap_edit_still_requires_explicit_reimport(tmp_path: Path) -> None:
    source = roadmap_directory() / "Qk1a" / "Qk1a_roadmap.json"
    roadmap = load_pose_roadmap(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["source"] = str(roadmap.mesh_path)
    roadmap_path = tmp_path / "roadmap.json"
    roadmap_path.write_text(json.dumps(payload), encoding="utf-8")
    roadmap = load_pose_roadmap(roadmap_path)
    model = tmp_path / "model.pt"
    model.write_bytes(b"test model; never loaded by this test")
    config_path = tmp_path / "part.yaml"
    save_roadmap_part_definition(
        config_path,
        roadmap_path=roadmap_path,
        part_name=roadmap.part_name,
        mesh_path=roadmap.mesh_path,
        model_path=model,
        pose_class_mapping={pose.pose_id: index for index, pose in enumerate(roadmap.robust_poses)},
        target_pose=roadmap.robust_poses[0].pose_id,
        transition_profiles={edge.edge_id: None for edge in roadmap.profile_transitions},
    )
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["roadmap_sha256"] == roadmap.sha256
    roadmap_path.write_text(roadmap_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RoadmapHashMismatchError):
        load_part_definition(config_path)
