from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from bibazu_reorientation.batch_controller import BatchController
from bibazu_reorientation.config import save_part_definition
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import BatchState, Detection, InferenceFrame, PlcSnapshot
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
    controller.set_camera_fresh(True)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            arrays_idle=True,
            light_barriers_stable=(True,) * 8,
        )
    )
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


def advance_to_running(controller: BatchController, pressure: FakePressure) -> None:
    pressure.operation_finished.emit("batch_safe_stop")
    pressure.operation_finished.emit("batch_configuration")
    pressure.operation_finished.emit("batch_reset")
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            reorientation_state=10,
            heartbeat_alive=True,
        )
    )
    pressure.operation_finished.emit("batch_reset_end")
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            calibration_valid=True,
            reorientation_state=10,
            heartbeat_alive=True,
        )
    )
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
    assert controller.state == BatchState.RUNNING


def test_batch_start_enqueue_ack_result_and_controlled_drain(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    queued = []
    results = []
    controller.part_queued.connect(queued.append)
    controller.part_result.connect(results.append)

    controller.start_cycle()
    advance_to_running(controller, pressure)
    started = time.time()
    for index, x in enumerate((70, 60, 50, 40, 30)):
        controller.accept_inference(frame(x, started + index * 0.01))

    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueueArrayMask"] == 2
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

    controller.stop()
    for index in range(3):
        controller.accept_inference(
            InferenceFrame(
                np.zeros((100, 100, 3), np.uint8), (), 1.0, started + 1 + index
            )
        )
    controller._empty_since = time.monotonic() - 2.0
    controller._finish_tick()
    assert controller.state == BatchState.DRAINING
    assert pressure.writes[-1][0] == "batch_finish"

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


def test_target_pose_is_queued_with_zero_mask(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller.start_cycle()
    advance_to_running(controller, pressure)
    started = time.time()
    for index, x in enumerate((70, 60, 50, 40, 30)):
        controller.accept_inference(frame(x, started + index * 0.01, class_id=0))

    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert pressure.writes[-1][1]["MAIN.GuiReorientationQueueArrayMask"] == 0
    assert all(
        pressure.writes[-1][1][f"MAIN.GuiReorientationQueueNozzleMask{index}"] == 0
        for index in range(1, 5)
    )


def test_queue_backpressure_fail_stops_before_unacknowledged_handoff(tmp_path: Path) -> None:
    controller, pressure = configured(tmp_path)
    controller.start_run()
    advance_to_running(controller, pressure)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            reorientation_state=20,
            heartbeat_alive=True,
            batch_queue_depth=128,
            batch_queue_capacity=128,
        )
    )
    started = time.time()
    for index, x in enumerate((70, 60, 50, 40, 30)):
        controller.accept_inference(frame(x, started + index * 0.01))

    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"
    assert not any(name.startswith("batch_enqueue:") for name, _values, _verify in pressure.writes)
