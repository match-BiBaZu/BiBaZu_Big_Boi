from __future__ import annotations

import base64
import json
from pathlib import Path

from PyQt6.QtWidgets import QDialog, QToolButton

from bibazu_reorientation.roadmap import load_stable_pose_roadmap
from bibazu_reorientation.ui.roadmap_pose_dialog import RoadmapPoseDialog

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def write_roadmap(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "Df1a.STL",
                "geometry_status": "provisional",
                "nodes": [
                    {
                        "node_id": 15,
                        "pose_ids": [15, 63, 154],
                        "kind": "robust",
                        "floor_contact_topology": "face",
                        "wall_contact_topology": "edge",
                        "thumbnail_png_base64": base64.b64encode(ONE_PIXEL_PNG).decode(),
                    },
                    {
                        "node_id": 48,
                        "pose_ids": [48],
                        "kind": "metastable",
                    },
                    {
                        "node_id": 31,
                        "pose_ids": [31, 109, 168],
                        "kind": "robust",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_loader_returns_only_stable_poses(tmp_path: Path) -> None:
    path = tmp_path / "Df1a_roadmap.json"
    write_roadmap(path)

    roadmap = load_stable_pose_roadmap(path)

    assert roadmap.part_name == "Df1a"
    assert [pose.pose_id for pose in roadmap.poses] == [15, 31]
    assert roadmap.pose(15).equivalent_pose_ids == (15, 63, 154)


def test_clicking_pose_card_selects_and_closes(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "Df1a_roadmap.json"
    write_roadmap(path)
    dialog = RoadmapPoseDialog(load_stable_pose_roadmap(path))
    qtbot.addWidget(dialog)

    button = dialog.findChild(QToolButton, "roadmap_pose_31")
    assert button is not None
    button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_pose is not None
    assert dialog.selected_pose.pose_id == 31
