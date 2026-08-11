from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.ui.transition_preview_dialog import TransitionPreviewDialog

ROADMAP = (
    Path(__file__).resolve().parents[3]
    / "bibazu_geometry_to_pose"
    / "Poses_Found_Robust"
    / "Df1a_roadmap_provisional"
    / "Df1a_roadmap.yaml"
)


def test_transition_dialog_renders_endpoints_and_animation(qtbot) -> None:
    roadmap = load_pose_roadmap(ROADMAP)
    transition = roadmap.profile_transitions[0]
    dialog = TransitionPreviewDialog(roadmap, transition)
    qtbot.addWidget(dialog)
    dialog.timer.stop()

    assert dialog.start_pose.pose_id == transition.from_pose
    assert dialog.end_pose.pose_id == transition.to_pose
    assert dialog.start_image.pixmap() is not None
    assert not dialog.start_image.pixmap().isNull()
    assert dialog.end_image.pixmap() is not None
    assert not dialog.end_image.pixmap().isNull()
    assert dialog.animation.pixmap() is not None
    assert dialog.slider.isEnabled()

    dialog.slider.setValue(500)
    assert dialog.progress_text.text() == "Animation progress: 50%"
    assert dialog.animation.pixmap() is not None
    assert not dialog.animation.pixmap().isNull()


def test_transition_dialog_falls_back_to_pose_images_without_cad(qtbot) -> None:
    roadmap = load_pose_roadmap(ROADMAP)
    roadmap_without_mesh = replace(roadmap, mesh_path=None)
    dialog = TransitionPreviewDialog(
        roadmap_without_mesh,
        roadmap_without_mesh.profile_transitions[0],
    )
    qtbot.addWidget(dialog)
    dialog.timer.stop()

    assert not dialog.slider.isEnabled()
    assert not dialog.play_button.isEnabled()
    assert dialog.start_image.pixmap() is not None
    assert dialog.end_image.pixmap() is not None
    assert "start and target images" in dialog.animation.text()
