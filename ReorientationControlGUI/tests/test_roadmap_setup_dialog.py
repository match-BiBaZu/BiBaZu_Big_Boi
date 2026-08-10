from __future__ import annotations

from pathlib import Path

from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog

ROADMAP = (
    Path(__file__).resolve().parents[3]
    / "bibazu_geometry_to_pose"
    / "Poses_Found_Robust"
    / "Df1a_roadmap_provisional"
    / "Df1a_roadmap.yaml"
)


def test_df1a_is_first_step_and_builds_six_profile_rows(qtbot) -> None:
    dialog = RoadmapSetupDialog()
    qtbot.addWidget(dialog)
    assert "zuerst" in dialog.summary.text()
    dialog._load_roadmap(ROADMAP)
    assert dialog.name.text() == "Df1a"
    assert dialog.mapping_table.rowCount() == 4
    assert dialog.profile_table.rowCount() == 6
    assert dialog.info_table.rowCount() == 17
    assert set(dialog.profile_inputs) == {
        "a0:9->35:wall_main_neg_x",
        "a1:9->60:free_z",
        "a2:24->35:free_y",
        "a3:24->60:floor_main_pos_x",
        "a4:35->24:free_y",
        "a15:60->9:free_z",
    }
    assert "Mehrposen-Ausführung noch nicht freigegeben" in dialog.readiness.text()
