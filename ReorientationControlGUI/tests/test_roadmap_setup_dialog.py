from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog

ROADMAP = (
    Path(__file__).resolve().parents[3]
    / "bibazu_geometry_to_pose"
    / "Poses_Found_Robust"
    / "Df1a_roadmap_provisional"
    / "Df1a_roadmap.yaml"
)


def test_df1a_builds_direct_and_multi_reorientation_profile_rows(qtbot) -> None:
    dialog = RoadmapSetupDialog()
    qtbot.addWidget(dialog)
    assert "first" in dialog.summary.text()
    dialog._load_roadmap(ROADMAP)
    assert dialog.name.text() == "Df1a"
    assert dialog.mapping_table.rowCount() == 4
    assert dialog.profile_table.rowCount() == 14
    assert dialog.profile_table.columnCount() == 9
    assert dialog.info_table.rowCount() == 17
    assert {
        "a0:9->35:wall_main_neg_x",
        "a1:9->60:free_z",
        "a2:24->35:free_y",
        "a3:24->60:floor_main_pos_x",
        "a4:35->24:free_y",
        "a15:60->9:free_z",
    }.issubset(dialog.profile_inputs)
    assert "multi2:60->9->35" in dialog.profile_inputs
    multi_row = next(
        row
        for row in range(dialog.profile_table.rowCount())
        if dialog.profile_table.cellWidget(row, 6).toolTip() == "multi2:60->9->35"
    )
    assert "MULTIPLE REORIENTATION" in dialog.profile_table.item(multi_row, 4).text()
    assert "at most two intermediate poses" in dialog.readiness.text()
    assert all(
        dialog.profile_table.cellWidget(row, 5).text() == "Preview …"
        for row in range(dialog.profile_table.rowCount())
    )


def test_transition_preview_button_opens_selected_edge(qtbot) -> None:
    dialog = RoadmapSetupDialog()
    qtbot.addWidget(dialog)
    dialog._load_roadmap(ROADMAP)

    with patch("bibazu_reorientation.ui.roadmap_setup_dialog.TransitionPreviewDialog") as preview:
        dialog.profile_table.cellWidget(0, 5).click()

    preview.assert_called_once()
    assert preview.call_args.args[0] is dialog.roadmap
    assert preview.call_args.args[1].edge_id == "a0:9->35:wall_main_neg_x"
    assert preview.call_args.kwargs["mesh_path"] == Path(dialog.mesh.text())
    preview.return_value.exec.assert_called_once()
