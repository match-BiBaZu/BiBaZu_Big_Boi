from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bibazu_reorientation.config import save_roadmap_part_definition
from bibazu_reorientation.models import CameraStatus, ConnectionState
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
    assert not window.exposure_slider.isEnabled()
    assert window.exposure_value.text() == "– µs"
    assert window.camera_fps.text() == "FPS: –"
    assert not window.light1._auto_reconnect
    assert not window.light2._auto_reconnect
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


def test_camera_status_enables_log_exposure_slider_and_shows_fps(
    qtbot, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.camera._set_state(ConnectionState.CONNECTED, "700006383255")
    window._camera_status_changed(
        CameraStatus(
            serial_number="700006383255",
            camera_fps=47.5,
            stream_fps=46.8,
            preview_fps=14.9,
            exposure_time_us=10_000.0,
            exposure_min_us=10.0,
            exposure_max_us=1_000_000.0,
            exposure_writable=True,
            exposure_auto="Off",
        )
    )

    assert window.exposure_slider.isEnabled()
    assert window.exposure_value.text() == "10 000 µs"
    assert window.camera_fps.text() == "FPS: 47.5 cam · 46.8 raw · 14.9 view"
    exposure = window._slider_to_exposure(window.exposure_slider.value(), 10.0, 1_000_000.0)
    assert exposure == pytest.approx(10_000.0, rel=0.02)

    requested: list[float] = []
    monkeypatch.setattr(
        window.camera,
        "set_exposure_time",
        lambda value: requested.append(value) or True,
    )
    window.exposure_slider.setValue(window._exposure_to_slider(25_000.0, 10.0, 1_000_000.0))
    window.exposure_apply_timer.stop()
    window._apply_camera_exposure()
    assert requested[-1] == pytest.approx(25_000.0, rel=0.02)

    window.camera.shutdown()
    window.pressure.shutdown()


@pytest.mark.asyncio
async def test_light_connections_are_serialized() -> None:
    order: list[str] = []

    class FakeLight:
        def __init__(self, name: str) -> None:
            self.name = name
            self.status = SimpleNamespace(connected=False)

        async def connect_async(self) -> None:
            order.append(f"{self.name}-start")
            await asyncio.sleep(0)
            order.append(f"{self.name}-end")
            self.status.connected = True

    class FakeButton:
        def setEnabled(self, _enabled: bool) -> None:  # noqa: N802 - Qt-compatible fake
            pass

        def setText(self, _text: str) -> None:  # noqa: N802 - Qt-compatible fake
            pass

    holder = SimpleNamespace(
        light1=FakeLight("one"),
        light2=FakeLight("two"),
        connect_button=FakeButton(),
    )
    await MainWindow._connect_lights_sequentially(holder)

    assert order == ["one-start", "one-end", "two-start", "two-end"]


def test_cancelled_light_connection_task_is_safely_consumed() -> None:
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(asyncio.sleep(1.0))
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        MainWindow._light_connection_finished(task)
    finally:
        loop.close()
