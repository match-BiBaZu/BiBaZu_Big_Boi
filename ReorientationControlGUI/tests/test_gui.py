from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert window.connect_button.text() == "Connect all components"
    assert window.disconnect_button.text() == "Disconnect all components"
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
    disconnected: list[str] = []
    monkeypatch.setattr(window.light1, "disconnect_device", lambda: disconnected.append("light1"))
    monkeypatch.setattr(window.light2, "disconnect_device", lambda: disconnected.append("light2"))
    monkeypatch.setattr(window.camera, "disconnect_device", lambda: disconnected.append("camera"))
    monkeypatch.setattr(
        window.pressure, "disconnect_device", lambda: disconnected.append("pressure")
    )
    window.disconnect_all()
    assert disconnected == ["light1", "light2", "camera", "pressure"]
    window.camera.shutdown()
    window.pressure.shutdown()


@pytest.mark.asyncio
async def test_light_connections_are_serialized() -> None:
    order: list[str] = []

    class FakeLight:
        def __init__(self, name: str) -> None:
            self.name = name

        async def connect_async(self) -> None:
            order.append(f"{self.name}-start")
            await asyncio.sleep(0)
            order.append(f"{self.name}-end")

    holder = SimpleNamespace(light1=FakeLight("one"), light2=FakeLight("two"))
    await MainWindow._connect_lights_sequentially(holder)

    assert order == ["one-start", "one-end", "two-start", "two-end"]
