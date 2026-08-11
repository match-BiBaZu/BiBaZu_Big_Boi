from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from bibazu_reorientation.batch_controller import BatchController
from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import (
    BatchState,
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


def configured(tmp_path: Path) -> tuple[BatchController, FakePressure]:
    model = tmp_path / "best.pt"
    model.write_bytes(b"model")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": 9,
                "light_barrier_debounce_ms": 17,
                "light_barrier_debounce_enabled": [
                    False,
                    False,
                    True,
                    True,
                    False,
                    False,
                    False,
                    False,
                ],
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
                        "index": 2,
                        "enabled": True,
                        "nozzles_enabled": [True],
                        "pressure_mbar": 3000,
                        "pulse_duration_ms": 50,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    part = save_part_definition(
        tmp_path / "part.yaml",
        part_name="part",
        model_path=model,
        pressure_profile=profile_path,
        target_pose=1,
    )
    pressure = FakePressure()
    controller = BatchController(pressure, RunJournal(tmp_path / "runs"))
    controller.set_configuration(part, load_pressure_profile(profile_path))
    controller.set_model_ready(True)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            arrays_idle=True,
            light_barriers_stable=(True,) * 8,
        )
    )
    controller.accept_inference(frame(70, time.time()))
    assert controller.state == BatchState.READY
    return controller, pressure


def frame(x: float, timestamp: float, class_id: int = 1) -> InferenceFrame:
    detection = Detection(
        class_id,
        f"class {class_id}",
        0.9,
        ((x - 5, 30), (x + 5, 30), (x + 5, 40), (x - 5, 40)),
        "detect",
    )
    return InferenceFrame(np.zeros((100, 100, 3), np.uint8), (detection,), 1.0, timestamp)


def multi_frame(
    *rows: tuple[float, int], timestamp: float | None = None
) -> InferenceFrame:
    detections = tuple(
        Detection(
            class_id,
            f"class {class_id}",
            0.9,
            ((x - 5, 30), (x + 5, 30), (x + 5, 40), (x - 5, 40)),
            "detect",
        )
        for x, class_id in rows
    )
    return InferenceFrame(
        np.zeros((100, 100, 3), np.uint8),
        detections,
        1.0,
        time.time() if timestamp is None else timestamp,
    )


def advance_to_queue_staging(controller: BatchController, pressure: FakePressure) -> None:
    pressure.operation_finished.emit("batch_safe_stop")
    pressure.operation_finished.emit("batch_configuration")
    pressure.operation_finished.emit("batch_reset")
    pressure.operation_finished.emit("batch_start")
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            reorientation_state=20,
            heartbeat_alive=True,
        )
    )
    pressure.operation_finished.emit("batch_start_end")
    assert controller.state == BatchState.STARTING
    assert pressure.writes[-1][0].startswith("batch_enqueue:")


def test_start_uses_profile_debounce_and_single_plc_acknowledgement(
    tmp_path: Path,
) -> None:
    controller, pressure = configured(tmp_path)
    controller.start_cycle()
    pressure.operation_finished.emit("batch_safe_stop")
    configuration = pressure.writes[-1][1]
    assert configuration["MAIN.GuiBarrierCalibrationDebounceMs"] == 17
    assert tuple(
        configuration[f"MAIN.GuiLightBarrierDebounceEnabled{index}"]
        for index in range(1, 9)
    ) == (False, False, True, True, False, False, False, False)
    pressure.operation_finished.emit("batch_configuration")
    assert pressure.writes[-1][0] == "batch_reset"
    assert pressure.writes[-1][2] is False

    pressure.operation_finished.emit("batch_reset")
    assert pressure.writes[-1][0] == "batch_start"
    pressure.operation_finished.emit("batch_start")
    assert controller._handshake_phase == "batch_start_ack"

    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            reorientation_fault_code=0,
            heartbeat_alive=False,
        )
    )
    assert controller._handshake_phase is None
    assert pressure.writes[-1][0] == "batch_start_end"


def test_fault_96_reports_latched_sensor_context(tmp_path: Path) -> None:
    controller, _pressure = configured(tmp_path)
    controller.snapshot = PlcSnapshot(
        reorientation_fault_detail=3,
        reorientation_fault_sensor=4,
        reorientation_fault_expected_sequence=7,
        reorientation_fault_previous_sequence=6,
        reorientation_fault_queue_ack=9,
        reorientation_fault_queue_slot_sequence=7,
        reorientation_fault_barrier_stable_mask=0xF3,
    )

    text = controller._plc_fault_text(96)

    assert "LB4" in text
    assert "previous sensor" in text
    assert "expected part 7" in text
    assert "queue_ack=9" in text
    assert "stable_mask=0xF3" in text


def test_batch_start_enqueue_ack_result_and_controlled_drain(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    queued = []
    results = []
    controller.part_queued.connect(queued.append)
    controller.part_result.connect(results.append)

    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueueArrayMask"] == 2
    assert not any(
        name == "batch_conveyor_start" for name, _values, _verify in pressure.writes
    )
    pressure.operation_finished.emit("batch_enqueue:1")
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_enqueue_ack=1,
            batch_queue_depth=1,
        )
    )
    assert queued[0].sequence_id == 1
    assert pressure.writes[-1][0] == "batch_conveyor_start"
    pressure.operation_finished.emit("batch_conveyor_start")
    assert controller.state == BatchState.DRAINING
    assert pressure.writes[-1][0] == "batch_finish"

    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_enqueue_ack=1,
            batch_result_available=True,
            batch_result_sequence=1,
            batch_result_triggered_mask=2,
        )
    )
    assert results[0].sequence_id == 1
    assert pressure.writes[-1][0] == "batch_result_ack:1"

    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=40,
            heartbeat_alive=True,
            complete=True,
        )
    )
    assert pressure.writes[-1][0] == "batch_release_safe"
    pressure.operation_finished.emit("batch_release_safe")
    assert pressure.writes[-1] == (
        "batch_release_owner",
        {"MAIN.GuiReorientationControlActive": False},
        True,
    )
    pressure.operation_finished.emit("batch_release_owner")
    assert controller.state == BatchState.COMPLETE


def test_all_snapshot_records_are_acknowledged_before_conveyor_motion(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller.accept_inference(multi_frame((70, 1), (30, 0)))
    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)

    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueuePoseId"] == 1
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_enqueue_ack=1,
            batch_queue_depth=1,
        )
    )
    assert pressure.writes[-1][0] == "batch_enqueue:2"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueuePoseId"] == 2
    assert not any(
        name == "batch_conveyor_start" for name, _values, _verify in pressure.writes
    )

    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_enqueue_ack=2,
            batch_queue_depth=2,
        )
    )
    assert pressure.writes[-1][0] == "batch_conveyor_start"


def test_target_pose_is_queued_with_zero_mask(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller.accept_inference(frame(70, time.time(), class_id=0))
    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)

    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueueArrayMask"] == 0
    assert all(
        pressure.writes[-1][1][f"MAIN.GuiReorientationQueueNozzleMask{index}"] == 0
        for index in range(1, 5)
    )


def test_snapshot_larger_than_plc_capacity_cannot_start(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            arrays_idle=True,
            batch_queue_capacity=1,
        )
    )
    controller.accept_inference(multi_frame((70, 1), (30, 0)))

    assert controller.state == BatchState.OFFLINE
    with pytest.raises(RuntimeError, match="preflight"):
        controller.start_run()
    assert pressure.writes == []


def test_camera_and_yolo_loss_after_snapshot_are_warnings(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    warnings = []
    controller.warning_raised.connect(lambda code, text: warnings.append((code, text)))
    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_enqueue_ack=1,
            batch_queue_depth=1,
        )
    )
    pressure.operation_finished.emit("batch_conveyor_start")

    controller.set_camera_fresh(False)
    controller.set_model_ready(False)

    assert controller.state == BatchState.DRAINING
    assert [code for code, _text in warnings] == ["camera_lost", "yolo_lost"]
    assert not any(name == "batch_abort" for name, _values, _verify in pressure.writes)


def test_uncertain_snapshot_part_warns_and_remains_in_queue(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    warnings = []
    controller.warning_raised.connect(lambda code, text: warnings.append((code, text)))
    controller.accept_inference(frame(3, time.time(), class_id=1))

    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)

    assert warnings[0][0] == "snapshot_uncertain"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueueArrayMask"] == 0
    assert controller.state == BatchState.STARTING


def test_plc_drive_fault_remains_a_hard_stop(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller.start_cycle()
    advance_to_queue_staging(controller, pressure)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=94,
            reorientation_fault_code=94,
            heartbeat_alive=True,
        )
    )

    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"
