from __future__ import annotations

import json
import time
from dataclasses import replace
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
    PartDefinition,
    PlcSnapshot,
    PoseDefinition,
    TransitionSpec,
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


def roadmap_inference_frame(class_id: int, timestamp: float) -> InferenceFrame:
    detection = Detection(
        class_id,
        f"Class {class_id}",
        0.95,
        ((10, 10), (40, 10), (40, 40), (10, 40)),
        "detect",
    )
    return InferenceFrame(np.zeros((50, 50, 3), np.uint8), (detection,), 2.0, timestamp)


def roadmap_profile(tmp_path: Path, name: str, array_index: int):
    source = tmp_path / name
    source.write_text(
        json.dumps(
            {
                "version": 9,
                "ur_ry_angle_deg": 18.0,
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
    return load_pressure_profile(source)


def advance_staging_to_enables(
    controller: ReorientationController, pressure: FakePressure
) -> None:
    pressure.operation_finished.emit("safe_stop")
    pressure.operation_finished.emit("configuration")
    assert pressure.writes[-1][0] == "heartbeat_prepare"
    pressure.operation_finished.emit("heartbeat_prepare")
    assert pressure.writes[-1][0] == "ownership"
    pressure.operation_finished.emit("ownership")
    assert controller._handshake_phase == "ownership_ack"
    acknowledged = PlcSnapshot(
        connected=True,
        calibration_valid=True,
        light_barriers_stable=(True,) * 8,
        reorientation_state=10,
        heartbeat_alive=True,
        heartbeat_ack=controller._heartbeat,
    )
    controller._on_snapshot(acknowledged)
    assert pressure.writes[-1][0] == "reset_end"
    pressure.operation_finished.emit("reset_end")
    assert controller._handshake_phase == "reset_release_ack"
    controller._on_snapshot(acknowledged)
    assert pressure.writes[-1][0] == "enables"


def test_roadmap_cycle_combines_unique_two_step_path_before_staging(tmp_path: Path) -> None:
    first = roadmap_profile(tmp_path, "2-3.json", 1)
    second = roadmap_profile(tmp_path, "3-4.json", 3)
    part = PartDefinition(
        schema_version=2,
        part_name="Roadmap part",
        model_path=tmp_path / "best.pt",
        poses=(
            PoseDefinition(2, "Pose 2", 0),
            PoseDefinition(3, "Pose 3", 1),
            PoseDefinition(4, "Pose 4", 2),
        ),
        target_pose=4,
        transitions=(
            TransitionSpec(2, 3, first.source_path, "edge-2-3"),
            TransitionSpec(3, 4, second.source_path, "edge-3-4"),
        ),
    )
    pressure = FakePressure()
    controller = ReorientationController(pressure, RunJournal(tmp_path / "runs"))
    controller.set_roadmap_configuration(
        part,
        {"edge-2-3": first, "edge-3-4": second},
        conveyor_speed_mm_per_sec=125.0,
        ur_ry_angle_deg=18.0,
    )
    controller.set_model_ready(True)
    controller.set_camera_fresh(True)
    controller.set_lights_ready(True, True)
    controller.set_ur_applied(18.0)
    controller._on_snapshot(
        PlcSnapshot(connected=True, calibration_valid=True, light_barriers_stable=(True,) * 8)
    )
    controller._barriers_clear_since = time.monotonic() - 1.0
    controller._refresh_preflight()

    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(roadmap_inference_frame(0, started + index * 0.01))

    assert [write[0] for write in pressure.writes] == ["safe_stop"]
    assert controller._plan.expected_array_mask == 0b0101
    assert controller.profile.conveyor_speed_mm_per_sec == 125.0
    assert [edge.edge_id for edge in controller._selected_transitions] == [
        "edge-2-3",
        "edge-3-4",
    ]


def test_roadmap_target_pose_forces_zero_mask_and_disables_every_array(
    tmp_path: Path,
) -> None:
    transition_profile = roadmap_profile(tmp_path, "5-10.json", 1)
    part = PartDefinition(
        schema_version=2,
        part_name="Kk1a",
        model_path=tmp_path / "best.pt",
        poses=(PoseDefinition(5, "Pose 5", 2), PoseDefinition(10, "Pose 10", 1)),
        target_pose=10,
        transitions=(
            TransitionSpec(5, 10, transition_profile.source_path, "edge-5-10"),
        ),
    )
    pressure = FakePressure()
    controller = ReorientationController(pressure, RunJournal(tmp_path / "runs"))
    controller.set_roadmap_configuration(
        part,
        {"edge-5-10": transition_profile},
        conveyor_speed_mm_per_sec=100.0,
        ur_ry_angle_deg=18.0,
    )
    controller.set_model_ready(True)
    controller.set_camera_fresh(True)
    controller.set_lights_ready(True, True)
    controller.set_ur_applied(18.0)
    controller._on_snapshot(
        PlcSnapshot(connected=True, calibration_valid=True, light_barriers_stable=(True,) * 8)
    )
    controller._barriers_clear_since = time.monotonic() - 1.0
    controller._refresh_preflight()

    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(roadmap_inference_frame(1, started + index * 0.01))

    assert controller._detected_pose == 10
    assert controller._selected_transitions == ()
    assert controller._plan.expected_array_mask == 0
    assert not any(
        controller._plan.enables[f"MAIN.GuiArrayEnabled{index}"]
        for index in range(1, 5)
    )
    assert pressure.writes[0][0] == "safe_stop"


def test_preflight_is_only_emitted_when_a_check_changes(tmp_path: Path) -> None:
    controller, _pressure = configured_controller(tmp_path)
    emissions: list[dict[str, bool]] = []
    controller.preflight_changed.connect(emissions.append)

    controller.set_camera_fresh(True)
    controller._refresh_preflight()
    assert emissions == []

    controller.set_camera_fresh(False)
    assert len(emissions) == 1
    assert emissions[0]["Fresh camera frame"] is False

    controller.set_camera_fresh(False)
    controller._refresh_preflight()
    assert len(emissions) == 1


def test_light_connection_and_confirmation_do_not_block_start(tmp_path: Path) -> None:
    controller, _pressure = configured_controller(tmp_path)

    controller.set_lights_ready(False, False)

    assert "Both lights confirmed" not in controller.preflight()
    assert controller.state == CycleState.READY


def test_latched_previous_abort_is_reset_during_staging(tmp_path: Path) -> None:
    controller, pressure = configured_controller(tmp_path)
    controller.start_cycle()

    stale_abort = PlcSnapshot(
        connected=True,
        calibration_valid=True,
        light_barriers_stable=(True,) * 8,
        reorientation_state=90,
        reorientation_fault_code=90,
    )
    controller._on_snapshot(stale_abort)
    assert controller.state == CycleState.DETECTING

    started = time.time()
    for index in range(3):
        controller.accept_inference(inference_frame(2, started + index * 0.01))
    assert controller.state == CycleState.STAGING
    controller._on_snapshot(stale_abort)
    assert controller.state == CycleState.STAGING

    pressure.operation_finished.emit("safe_stop")
    pressure.operation_finished.emit("configuration")
    pressure.operation_finished.emit("heartbeat_prepare")
    pressure.operation_finished.emit("ownership")
    assert controller._handshake_phase == "ownership_ack"

    cleared = replace(
        stale_abort,
        reorientation_state=10,
        reorientation_fault_code=0,
        heartbeat_alive=True,
        heartbeat_ack=controller._heartbeat,
    )
    controller._on_snapshot(cleared)
    assert pressure.writes[-1][0] == "reset_end"
    assert controller.state == CycleState.STAGING


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
    assert pressure.writes[-1][0] == "heartbeat_prepare"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationHeartbeat"] > 0
    pressure.operation_finished.emit("heartbeat_prepare")
    assert pressure.writes[-1][0] == "ownership"
    pressure.operation_finished.emit("ownership")
    assert pressure.writes[-1][0] == "ownership"
    assert controller._handshake_phase == "ownership_ack"
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            light_barriers_stable=(True,) * 8,
            reorientation_state=10,
            heartbeat_alive=True,
            heartbeat_ack=controller._heartbeat_ack_before_ownership,
        )
    )
    assert pressure.writes[-1][0] == "ownership"
    acknowledged = PlcSnapshot(
        connected=True,
        calibration_valid=True,
        light_barriers_stable=(True,) * 8,
        reorientation_state=10,
        heartbeat_alive=True,
        heartbeat_ack=controller._heartbeat,
    )
    controller._on_snapshot(acknowledged)
    assert pressure.writes[-1][0] == "reset_end"
    pressure.operation_finished.emit("reset_end")
    assert pressure.writes[-1][0] == "reset_end"
    assert controller._handshake_phase == "reset_release_ack"
    controller._on_snapshot(acknowledged)
    assert [row[0] for row in pressure.writes][-1] == "enables"
    pressure.operation_finished.emit("enables")
    assert pressure.writes[-1][0] == "start"
    pressure.operation_finished.emit("start")
    assert pressure.writes[-1][0] == "start"
    assert controller._handshake_phase == "start_ack"
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            light_barriers_stable=(True,) * 8,
            reorientation_state=20,
            heartbeat_alive=True,
            busy=True,
        )
    )
    assert pressure.writes[-1][0] == "start_pulse_end"
    pressure.operation_finished.emit("start_pulse_end")
    assert controller.state == CycleState.RUNNING


def test_pose_one_uses_zero_array_mask(tmp_path: Path) -> None:
    controller, pressure = configured_controller(tmp_path)
    controller.start_cycle()
    started = time.time()
    for index in range(3):
        controller.accept_inference(inference_frame(1, started + index * 0.01))
    advance_staging_to_enables(controller, pressure)
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
