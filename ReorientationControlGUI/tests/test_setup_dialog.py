from __future__ import annotations

from pathlib import Path

from bibazu_reorientation.config import save_part_definition
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
    assert dialog.windowTitle() == "Bauteilkonfiguration bearbeiten"
    assert dialog.name.text() == "Teil A"
    assert dialog.model.text() == str(model)
    assert dialog.mesh.text() == str(mesh)
    assert dialog.profile.text() == str(profile)
    assert dialog.target_pose.currentData() == 2
    assert "Pose 1 → Pose 2" in dialog.profile_label.text()

    dialog.target_pose.setCurrentIndex(dialog.target_pose.findData(1))
    assert "Pose 2 → Pose 1" in dialog.profile_label.text()
