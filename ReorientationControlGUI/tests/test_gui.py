from __future__ import annotations

from pathlib import Path

from bibazu_reorientation.config import save_roadmap_part_definition
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow


def test_main_window_offscreen_smoke(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    assert window.windowTitle() == "BiBaZu Reorientation Control"
    assert window.start_button.text() == "Start cycle"
    assert window.stop_button.text() == "STOP"
    roadmap_path = (
        Path(__file__).resolve().parents[3]
        / "bibazu_geometry_to_pose"
        / "Poses_Found_Robust"
        / "Df1a_roadmap_provisional"
        / "Df1a_roadmap.yaml"
    )
    roadmap = load_pose_roadmap(roadmap_path)
    model = tmp_path / "best.pt"
    model.write_bytes(b"model")
    definition = save_roadmap_part_definition(
        tmp_path / "part.yaml",
        roadmap_path=roadmap_path,
        part_name="Df1a",
        mesh_path=roadmap.mesh_path,
        model_path=model,
        pose_class_mapping={9: 0, 24: 1, 35: 2, 60: 3},
        target_pose=35,
        transition_profiles={edge.edge_id: None for edge in roadmap.profile_transitions},
    )
    called = []
    monkeypatch.setattr(window.controller, "set_configuration", lambda *_: called.append(True))
    window._load(definition)
    assert called == []
    assert not window.start_button.isEnabled()
    assert "Multi-pose execution" in window.start_button.text()
    assert window.inference is None
    window.camera.shutdown()
    window.pressure.shutdown()
