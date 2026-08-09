from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.controller import ReorientationController
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import (
    CycleState,
    Detection,
    InferenceFrame,
    PlcSnapshot,
)
from bibazu_reorientation.profiles import load_pressure_profile


class FakePressure(QObject):
    connection_changed = pyqtSignal(bool, str)
    snapshot_changed = pyqtSignal(object)
    operation_finished = pyqtSignal(str)
    operation_failed = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, dict, bool]] = []

    def write(self, name: str, values: dict, verify: bool = False) -> None:
        self.writes.append((name, values, verify))


def configured_controller(
    tmp_path: Path, target_pose: int = 1
) -> tuple[ReorientationController, FakePressure]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = tmp_path / "best.pt"
    profile_path = tmp_path / "profile.json"
    model.write_bytes(b"model")
    profile_path.write_text(
        json.dumps(
            {
                "version": 8,
                "conveyor_enabled": True,
                "conveyor_speed_mm_per_sec": 100,
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
                        "delay_ms": 0,
                        "pulse_duration_ms": 100,
                        "offset_mm": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    part = save_part_definition(
        tmp_path / "part.yaml",
        part_name="Testteil",
        model_path=model,
        pressure_profile=profile_path,
        target_pose=target_pose,
    )
    pressure = FakePressure()
    controller = ReorientationController(pressure, RunJournal(tmp_path / "runs"))
    controller.set_configuration(part, load_pressure_profile(profile_path))
    controller.set_model_ready(True)
    controller.set_camera_fresh(True)
    controller.set_lights_ready(True, True)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            light_barriers_stable=(True,) * 6,
        )
    )
    controller._barriers_clear_since = time.monotonic() - 1.0
    controller._refresh_preflight()
    return controller, pressure


def inference_frame(pose: int, timestamp: float | None = None) -> InferenceFrame:
    detection = Detection(
        pose - 1,
        f"Pose {pose}",
        0.95,
        ((10, 10), (40, 10), (40, 40), (10, 40)),
        "detect",
    )
    return InferenceFrame(
        np.zeros((50, 50, 3), np.uint8), (detection,), 2.0, timestamp or time.time()
    )


def test_staging_order_and_no_enable_before_readback(tmp_path: Path) -> None:
    controller, pressure = configured_controller(tmp_path)
    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(inference_frame(2, started + index * 0.01))
    assert [row[0] for row in pressure.writes] == ["safe_stop"]
    assert pressure.writes[0][2] is True
    pressure.operation_finished.emit("safe_stop")
    assert [row[0] for row in pressure.writes] == ["safe_stop", "configuration"]
    assert "MAIN.GuiConveyorEnabled" not in pressure.writes[1][1]
    pressure.operation_finished.emit("configuration")
    pressure.operation_finished.emit("ownership")
    pressure.operation_finished.emit("reset_end")
    pressure.operation_finished.emit("heartbeat_initial")
    assert [row[0] for row in pressure.writes][-1] == "enables"


def test_pose_one_uses_zero_array_mask(tmp_path: Path) -> None:
    controller, pressure = configured_controller(tmp_path)
    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(inference_frame(1, started + index * 0.01))
    pressure.operation_finished.emit("safe_stop")
    pressure.operation_finished.emit("configuration")
    pressure.operation_finished.emit("ownership")
    pressure.operation_finished.emit("reset_end")
    pressure.operation_finished.emit("heartbeat_initial")
    enables = pressure.writes[-1][1]
    assert enables["MAIN.GuiReorientationExpectedArrayMask"] == 0
    assert not any(enables[f"MAIN.GuiArrayEnabled{i}"] for i in range(1, 5))


def test_pose_two_target_passes_through_and_pose_one_actuates(tmp_path: Path) -> None:
    controller, pressure = configured_controller(tmp_path, target_pose=2)
    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(inference_frame(2, started + index * 0.01))
    assert pressure.writes[0][0] == "safe_stop"
    assert controller._plan.expected_array_mask == 0

    second, _ = configured_controller(tmp_path / "second", target_pose=2)
    second.start_cycle()
    for index in range(3):
        second.accept_inference(inference_frame(1, started + index * 0.01))
    assert second._plan.expected_array_mask == 1


def test_illegal_double_start(tmp_path: Path) -> None:
    controller, _ = configured_controller(tmp_path)
    controller.start_cycle()
    with pytest.raises(RuntimeError):
        controller.start_cycle()
    assert controller.state == CycleState.DETECTING
