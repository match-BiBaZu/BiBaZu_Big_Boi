from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from bibazu_reorientation.config import save_roadmap_part_definition
from bibazu_reorientation.models import CameraStatus, ConnectionState
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow


def write_transition_profile(
    path: Path, *, array_index: int, speed: float, angle: float
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 9,
                "ur_ry_angle_deg": angle,
                "conveyor_enabled": True,
                "conveyor_speed_mm_per_sec": speed,
                "conveyor_max_speed_mm_per_sec": 1000,
                "conveyor_calibration": {
                    "marker_distance_mm": 315,
                    "mm_per_full_step": 0.3296,
                    "valid": True,
                },
                "arrays": [
                    {
                        "index": array_index,
                        "enabled": True,
                        "nozzles_enabled": [True],
                        "pressure_mbar": 3000,
                        "pulse_duration_ms": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


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
    yolo_loads = []
    monkeypatch.setattr(window, "load_yolo_model", lambda: yolo_loads.append(True))
    window._load(definition)
    assert called == []
    assert yolo_loads == [True]
    assert not window.start_button.isEnabled()
    assert window.start_button.text() == "Start classification and reorientation"
    assert "No pressure profile" in window.machine_parameter_status.text()
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


def test_roadmap_profile_prefills_machine_fields_and_enables_executor(
    qtbot, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
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
    profile = tmp_path / "9-to-35.json"
    profile.write_text(
        json.dumps(
            {
                "version": 9,
                "ur_ry_angle_deg": 18.5,
                "conveyor_enabled": True,
                "conveyor_speed_mm_per_sec": 135,
                "conveyor_max_speed_mm_per_sec": 1000,
                "conveyor_calibration": {
                    "marker_distance_mm": 315,
                    "mm_per_full_step": 0.3296,
                    "valid": True,
                },
                "arrays": [
                    {
                        "index": 1,
                        "enabled": True,
                        "nozzles_enabled": [True],
                        "pressure_mbar": 3000,
                        "pulse_duration_ms": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    selected_edge = next(
        edge for edge in roadmap.profile_transitions if (edge.from_pose, edge.to_pose) == (9, 35)
    )
    definition = save_roadmap_part_definition(
        tmp_path / "part.yaml",
        roadmap_path=roadmap_path,
        part_name="Df1a",
        mesh_path=roadmap.mesh_path,
        model_path=model,
        pose_class_mapping={9: 0, 24: 1, 35: 2, 60: 3},
        target_pose=35,
        transition_profiles={
            edge.edge_id: profile if edge.edge_id == selected_edge.edge_id else None
            for edge in roadmap.profile_transitions
        },
    )
    monkeypatch.setattr(window, "load_yolo_model", lambda: None)

    window._load(definition)

    assert window._machine_parameters_confirmed
    assert window.controller.part == definition
    assert window.conveyor_speed_input.value() == 135
    assert window.use_ur_angle.isChecked()
    assert window.ur_angle_input.value() == 18.5
    assert "confirmed" in window.machine_parameter_status.text()
    window.close()
    window.camera.shutdown()
    window.pressure.shutdown()


def test_roadmap_profile_conflicts_require_explicit_machine_values(
    qtbot, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
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
    first = write_transition_profile(
        tmp_path / "60-to-9.json", array_index=1, speed=100, angle=18.0
    )
    second = write_transition_profile(
        tmp_path / "9-to-35.json", array_index=3, speed=150, angle=19.0
    )
    selected = {
        "a15:60->9:free_z": first,
        "a0:9->35:wall_main_neg_x": second,
    }
    definition = save_roadmap_part_definition(
        tmp_path / "part.yaml",
        roadmap_path=roadmap_path,
        part_name="Df1a",
        mesh_path=roadmap.mesh_path,
        model_path=model,
        pose_class_mapping={9: 0, 24: 1, 35: 2, 60: 3},
        target_pose=35,
        transition_profiles={
            edge.edge_id: selected.get(edge.edge_id) for edge in roadmap.profile_transitions
        },
    )
    monkeypatch.setattr(window, "load_yolo_model", lambda: None)

    window._load(definition)
    assert not window._machine_parameters_confirmed
    assert "Profiles disagree" in window.machine_parameter_status.text()
    assert window.controller.part is None

    window.conveyor_speed_input.setValue(125.0)
    window.ur_angle_input.setValue(18.5)
    window.apply_machine_parameters(show_error=False)
    assert window._machine_parameters_confirmed
    assert window.controller.profile.conveyor_speed_mm_per_sec == 125.0
    assert window.controller.profile.ur_ry_angle_deg == 18.5
    window.close()
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


def test_identical_preflight_update_keeps_existing_widgets(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    checks = {"Camera": True, "PLC": False}

    window._preflight(checks)
    widgets = [window.preflight.itemAt(index).widget() for index in range(2)]
    window._preflight(checks.copy())

    assert [window.preflight.itemAt(index).widget() for index in range(2)] == widgets
    window.close()
    window.camera.shutdown()
    window.pressure.shutdown()


def test_light_worker_signals_are_queued_to_the_gui_thread(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    observed_threads: list[QThread] = []
    monkeypatch.setattr(
        window,
        "_lights_changed",
        lambda: observed_threads.append(QThread.currentThread()),
    )

    emitter = threading.Thread(
        target=lambda: (
            window.light1._set_state(ConnectionState.CONNECTING, "worker test"),
            window.light1.status_changed.emit(window.light1.status),
        )
    )
    emitter.start()
    emitter.join(timeout=1.0)

    assert not emitter.is_alive()
    assert window.light_panel1.status.text() == "Disconnected"
    assert observed_threads == []
    qtbot.waitUntil(lambda: "worker test" in window.light_panel1.status.text())
    qtbot.waitUntil(lambda: bool(observed_threads))
    assert observed_threads == [QApplication.instance().thread()]

    window.close()
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
