from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QDialog

from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.roadmap import load_stable_pose_roadmap
from bibazu_reorientation.ui.setup_dialog import SetupDialog


def test_edit_dialog_is_prefilled_and_updates_transition(qtbot, tmp_path: Path) -> None:
    model = tmp_path / "best.pt"
    profile = tmp_path / "1-to-2.json"
    mesh = tmp_path / "part.STL"
    model.write_bytes(b"model")
    profile.write_text("{}", encoding="utf-8")
    mesh.write_bytes(b"solid part\nendsolid part\n")
    definition = save_part_definition(
        tmp_path / "part.yaml",
        part_name="Teil A",
        model_path=model,
        pressure_profile=profile,
        target_pose=2,
        mesh_path=mesh,
    )

    dialog = SetupDialog(definition=definition)
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Edit part configuration"
    assert dialog.name.text() == "Teil A"
    assert dialog.model.text() == str(model)
    assert dialog.mesh.text() == str(mesh)
    assert dialog.profile.text() == str(profile)
    assert dialog.target_pose.currentData() == 2
    assert "Pose 1 → Pose 2" in dialog.profile_label.text()

    dialog.target_pose.setCurrentIndex(dialog.target_pose.findData(1))
    assert "Pose 2 → Pose 1" in dialog.profile_label.text()


def test_roadmap_pose_picker_updates_physical_target(qtbot, tmp_path: Path) -> None:
    roadmap_path = tmp_path / "Df1a_roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "Df1a.STL",
                "geometry_status": "provisional",
                "nodes": [
                    {"node_id": 15, "pose_ids": [15, 63, 154], "kind": "robust"},
                    {"node_id": 48, "pose_ids": [48], "kind": "metastable"},
                ],
            }
        ),
        encoding="utf-8",
    )
    roadmap = load_stable_pose_roadmap(roadmap_path)
    dialog = SetupDialog()
    qtbot.addWidget(dialog)

    with patch("bibazu_reorientation.ui.setup_dialog.RoadmapPoseDialog") as dialog_type:
        chooser = dialog_type.return_value
        chooser.exec.return_value = QDialog.DialogCode.Accepted
        chooser.selected_pose = roadmap.pose(15)
        dialog._show_roadmap_pose_dialog(roadmap_path)

    assert dialog._roadmap_path == roadmap_path
    assert dialog._target_roadmap_pose_id == 15
    assert "Roadmap pose 15" in dialog.roadmap_pose_label.text()
    assert "YOLO target class Pose 1" in dialog.roadmap_pose_label.text()
