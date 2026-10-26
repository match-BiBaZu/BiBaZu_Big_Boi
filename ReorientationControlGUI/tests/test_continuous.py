from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from test_batch_controller import FakePressure, multi_frame
from test_manual_config import make_manual_part, source_profiles

from bibazu_reorientation.batch_controller import BatchController
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import BatchState, PlcSnapshot, ProductionMode


@pytest.fixture
def continuous(tmp_path, qapp):
    part = make_manual_part(tmp_path)
    pressure = FakePressure()
    controller = BatchController(
        pressure, RunJournal(tmp_path / "runs"), production_mode=ProductionMode.CONTINUOUS
    )
    controller.set_manual_configuration(part, source_profiles(part), conveyor_speed_mm_per_sec=100)
    controller.set_model_ready(True)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            conveyor_velocity_only=True,
            calibration_valid=True,
        )
    )
    controller.accept_inference(multi_frame())
    assert controller.state == BatchState.READY
    yield controller, pressure
    controller.abort_run()
    controller._health_timer.stop()
    controller._heartbeat_timer.stop()


def start(continuous):
    controller, pressure = continuous
    controller.start_run()
    for name in ("batch_safe_stop", "batch_configuration", "batch_reset", "batch_start"):
        pressure.operation_finished.emit(name)
    controller._on_snapshot(
        replace(controller.snapshot, reorientation_state=20, heartbeat_alive=True)
    )
    pressure.operation_finished.emit("batch_start_end")
    assert pressure.writes[-1][0] == "batch_conveyor_start"
    pressure.operation_finished.emit("batch_conveyor_start")
    assert controller.state == BatchState.RUNNING


def observe(controller, class_id, positions=(80, 70, 60, 50, 40, 35)):
    for position in positions:
        controller.accept_inference(
            multi_frame(
                (position, class_id),
                timestamp=max(time.time(), controller._latest_frame.timestamp + 0.001),
            )
        )


def acknowledge(controller, sequence):
    controller._on_snapshot(replace(controller.snapshot, batch_enqueue_ack=sequence))


def test_empty_belt_starts_once_and_keeps_running_between_parts(continuous):
    controller, pressure = continuous
    start(continuous)
    for _ in range(10):
        controller.accept_inference(
            multi_frame(timestamp=controller._latest_frame.timestamp + 0.001)
        )
    assert controller.state == BatchState.RUNNING
    assert [name for name, _, _ in pressure.writes].count("batch_conveyor_start") == 1
    assert not any(
        name == "batch_finish" or name.startswith("batch_enqueue:")
        for name, _, _ in pressure.writes
    )
    metadata = json.loads((controller._session / "run.json").read_text(encoding="utf-8"))
    assert metadata["production_mode"] == "continuous"


def test_moving_conveyor_can_start_but_still_waits_for_preparation(continuous):
    controller, pressure = continuous
    controller._on_snapshot(
        replace(controller.snapshot, conveyor_motion_state=1, stepper_busy=True)
    )
    assert "Conveyor stopped" not in controller.preflight()
    assert controller.state == BatchState.READY
    pressure.auto_prepare = False
    controller.start_run()
    pressure.operation_finished.emit("batch_safe_stop")
    assert pressure.preparations
    assert [name for name, _, _ in pressure.writes] == ["batch_safe_stop"]
    pressure.operation_finished.emit(pressure.preparations[-1])
    assert pressure.writes[-1][0] == "batch_configuration"


def test_random_poses_select_each_profile_once_without_stopping_belt(continuous):
    controller, pressure = continuous
    start(continuous)
    records = []
    controller.part_queued.connect(records.append)
    for sequence, class_id in enumerate((7, 0, 42, 99), 1):
        observe(controller, class_id)
        assert pressure.writes[-1][0] == f"batch_enqueue:{sequence}"
        acknowledge(controller, sequence)
    assert [record.expected_array_mask for record in records] == [4, 1, 0, 0]
    assert records[0].arrays[2].pressure_mbar == 2007
    assert records[1].arrays[0].pressure_mbar == 2000
    assert controller.state == BatchState.RUNNING
    assert not any(name == "batch_finish" for name, _, _ in pressure.writes)
    assert [name for name, _, _ in pressure.writes].count("batch_conveyor_start") == 1


def test_profile_is_locked_after_handoff_and_not_queued_twice(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7)
    frozen = controller._pending_enqueue[0]
    observe(controller, 0, positions=(30, 25, 20, 15, 10))
    assert controller._pending_enqueue[0] == frozen
    assert [name for name, _, _ in pressure.writes].count("batch_enqueue:1") == 1
    assert controller._sequence == 1


def test_completed_part_does_not_finish_continuous_run(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7)
    acknowledge(controller, 1)
    controller._on_snapshot(
        replace(
            controller.snapshot,
            batch_result_available=True,
            batch_result_sequence=1,
            batch_completed_count=1,
            batch_result_triggered_mask=4,
        )
    )
    assert controller.state == BatchState.RUNNING
    assert pressure.writes[-1][0] == "batch_result_ack:1"
    assert not any(name == "batch_finish" for name, _, _ in pressure.writes)


@pytest.mark.parametrize("state", [BatchState.RUNNING, BatchState.FINISHING, BatchState.DRAINING])
def test_stop_disables_outputs_without_waiting_for_parts_or_queue_depth(continuous, state):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7)
    controller.snapshot = replace(controller.snapshot, batch_queue_depth=3)
    controller.state = state
    controller.stop()
    assert controller.state == BatchState.STOPPING
    name, values, verify = pressure.writes[-1]
    assert name == f"batch_stop:{controller._run_id}"
    assert verify
    assert values["MAIN.GuiReorientationAbort"] is True
    assert values["MAIN.GuiConveyorEnabled"] is False
    assert all(values[f"MAIN.GuiArrayEnabled{i}"] is False for i in range(1, 5))
    assert controller._pending_enqueue is None
    assert not controller._handoff_queue
    assert not controller._heartbeat_timer.isActive()
    assert not controller._health_timer.isActive()
    assert not any(name == "batch_finish" for name, _, _ in pressure.writes)
    writes = list(pressure.writes)
    controller.stop()  # Repeated clicks do not send more commands.
    acknowledge(controller, 1)
    controller._on_snapshot(replace(controller.snapshot, reorientation_fault_code=90))
    assert controller.state == BatchState.STOPPING
    assert pressure.writes == writes
    pressure.operation_finished.emit(name)
    assert pressure.writes[-1][0] == f"batch_stop_owner:{controller._run_id}"
    assert pressure.writes[-1][1] == {"MAIN.GuiReorientationControlActive": False}
    pressure.operation_finished.emit(pressure.writes[-1][0])
    assert controller.state == BatchState.COMPLETE
    result = json.loads((controller._session / "result.json").read_text(encoding="utf-8"))
    assert result["state"] == "stopped"
    writes = list(pressure.writes)
    for operation in ("batch_conveyor_start", "batch_start_end", "batch_release_owner"):
        pressure.operation_finished.emit(operation)
    assert pressure.writes == writes
    assert controller.state == BatchState.COMPLETE


def test_stop_with_empty_feed_zone_stops_without_requesting_drain(continuous):
    controller, pressure = continuous
    start(continuous)
    controller.stop()
    assert controller.state == BatchState.STOPPING
    assert pressure.writes[-1][0].startswith("batch_stop:")


@pytest.mark.parametrize("failure", ["write", "disconnect"])
def test_failed_stop_is_reported_instead_of_marking_run_complete(continuous, failure):
    controller, pressure = continuous
    start(continuous)
    controller.stop()
    stop_name = pressure.writes[-1][0]
    if failure == "write":
        pressure.operation_failed.emit(stop_name, "Stop readback failed")
    else:
        pressure.connection_changed.emit(False, "ADS lost while stopping")
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"
    pressure.operation_finished.emit(stop_name)
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"


def test_stopped_run_can_be_prepared_and_started_again(continuous):
    controller, pressure = continuous
    start(continuous)
    controller.stop()
    stop_name = pressure.writes[-1][0]
    pressure.operation_finished.emit(stop_name)
    pressure.operation_finished.emit(pressure.writes[-1][0])
    assert controller.state == BatchState.COMPLETE
    controller.prepare_next_cycle()
    assert controller.state == BatchState.READY
    start(continuous)
    assert not controller._operator_stop_requested
    assert controller.state == BatchState.RUNNING
    writes = list(pressure.writes)
    pressure.operation_finished.emit(stop_name)
    assert pressure.writes == writes


def test_detections_after_stop_cannot_enqueue_or_restart_motion(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 0, positions=(55, 50))
    controller.stop()
    writes = list(pressure.writes)
    observe(controller, 7, positions=(90,))
    assert controller.state == BatchState.STOPPING
    assert pressure.writes == writes


@pytest.mark.parametrize("loss", ["camera", "yolo", "stale_inference", "late_ack"])
def test_live_input_or_acknowledgement_failure_stops_the_belt(continuous, loss):
    controller, pressure = continuous
    start(continuous)
    if loss == "camera":
        controller.set_camera_fresh(False)
    elif loss == "yolo":
        controller.set_model_ready(False)
    elif loss == "stale_inference":
        controller._latest_frame = replace(controller._latest_frame, timestamp=time.time() - 2)
        controller._check_continuous_health()
    else:
        observe(controller, 7)
        controller._enqueue_requested_at = time.monotonic() - 11
        controller._check_continuous_health()
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"
    assert pressure.writes[-1][1]["MAIN.GuiConveyorEnabled"] is False


@pytest.mark.parametrize("problem", ["late_detection", "full_queue"])
def test_ambiguous_part_identity_cannot_be_assigned_to_the_next_part(continuous, problem):
    controller, pressure = continuous
    start(continuous)
    if problem == "late_detection":
        observe(controller, 7, positions=(25,))
    else:
        controller.snapshot = replace(
            controller.snapshot, batch_queue_capacity=1, batch_queue_depth=1
        )
        observe(controller, 7)
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"


@pytest.mark.parametrize("missed_frames", [3, 30])
def test_lost_detection_does_not_stop_run_and_reacquisition_uses_current_pose(
    continuous, missed_frames
):
    controller, pressure = continuous
    start(continuous)
    warnings = []
    controller.warning_raised.connect(lambda code, text: warnings.append((code, text)))
    observe(controller, 7, positions=(80, 70))
    writes = list(pressure.writes)
    for _ in range(missed_frames):
        controller.accept_inference(
            multi_frame(timestamp=controller._latest_frame.timestamp + 0.001)
        )
    assert controller.state == BatchState.RUNNING
    assert controller._last_tracks == ()
    assert pressure.writes == writes
    assert warnings == []
    observe(controller, 0, positions=(35,))
    assert controller.state == BatchState.RUNNING
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].pose_id == 0
    assert controller._pending_enqueue[0].expected_array_mask == 1


@pytest.mark.parametrize("warning", [1, 2, 3, 4, 5])
def test_plc_light_barrier_warnings_keep_continuous_belt_running(continuous, warning):
    controller, pressure = continuous
    start(continuous)
    warnings = []
    controller.warning_raised.connect(lambda code, text: warnings.append((code, text)))
    writes = list(pressure.writes)
    controller._on_snapshot(
        replace(
            controller.snapshot,
            reorientation_warning_counter=1,
            reorientation_warning_code=warning,
            reorientation_warning_sensor=1,
            light_barriers_stable=(False,) + (True,) * 7,
        )
    )
    assert controller.state == BatchState.RUNNING
    assert pressure.writes == writes
    assert len(warnings) == 1
    observe(controller, 7, positions=(45, 35))
    acknowledge(controller, 1)
    assert controller.state == BatchState.RUNNING
    assert controller._pending_parts[1][0].expected_array_mask == 4


def test_delayed_status_acknowledgement_keeps_running_and_sends_next_record(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7, positions=(35,))
    controller._enqueue_requested_at = time.monotonic() - 4
    controller._check_continuous_health()
    assert controller.state == BatchState.RUNNING
    assert controller._pending_enqueue[0].sequence_id == 1
    # The second part can arrive while the first is unacknowledged and in transit.
    for rows in (((25, 7), (55, 0)), ((15, 7), (35, 0))):
        controller.accept_inference(
            multi_frame(*rows, timestamp=controller._latest_frame.timestamp + 0.001)
        )
    assert controller._sequence == 2
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    acknowledge(controller, 1)
    assert pressure.writes[-1][0] == "batch_enqueue:2"
    acknowledge(controller, 2)
    assert controller.state == BatchState.RUNNING
    assert [controller._pending_parts[i][0].expected_array_mask for i in (1, 2)] == [4, 1]
    assert not any(name in {"batch_abort", "batch_finish"} for name, _, _ in pressure.writes)


def test_queue_ack_deadline_starts_after_ads_commit_write_completes(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7, positions=(35,))
    controller._enqueue_requested_at = time.monotonic() - 9
    pressure.operation_finished.emit("batch_enqueue:1")
    controller._check_continuous_health()
    assert controller.state == BatchState.RUNNING
    assert controller._pending_enqueue is not None  # Write completion is not PLC acceptance.
    written_at = controller._enqueue_written_at
    pressure.operation_finished.emit("batch_enqueue:1")
    assert controller._enqueue_written_at == written_at
    controller._enqueue_written_at = time.monotonic() - 11
    controller._check_continuous_health()
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"


@pytest.mark.parametrize("position", [80, 35])
def test_continuous_start_accepts_parts_already_visible_and_queues_at_line(continuous, position):
    controller, pressure = continuous
    controller.accept_inference(multi_frame((position, 7)))
    assert controller.state == BatchState.READY
    assert "Fresh YOLO frame; feed zone empty before start" not in controller.preflight()
    assert controller.preflight()["Fresh YOLO frame"]
    start(continuous)
    assert controller._last_tracks
    if position == 80:
        assert controller._pending_enqueue is None
        observe(controller, 7, positions=(70, 60, 50, 40, 35))
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].pose_id == 7
    assert controller._pending_enqueue[0].expected_array_mask == 4


def test_stop_immediately_after_start_does_not_wait_for_visible_part(continuous):
    controller, pressure = continuous
    controller.accept_inference(multi_frame((80, 7)))
    start(continuous)
    controller.stop()
    assert controller.state == BatchState.STOPPING
    writes = list(pressure.writes)
    observe(controller, 7, positions=(70, 60, 50, 40, 35))
    acknowledge(controller, 1)
    assert controller.state == BatchState.STOPPING
    assert pressure.writes == writes


def test_startup_detection_waits_for_preparation_and_uses_latest_pose(continuous):
    controller, pressure = continuous
    pressure.auto_prepare = False
    controller.start_run()
    observe(controller, 0, positions=(45,))
    pressure.operation_finished.emit("batch_safe_stop")
    assert controller.state == BatchState.STARTING
    assert [name for name, _, _ in pressure.writes] == ["batch_safe_stop"]
    pressure.operation_finished.emit(pressure.preparations[-1])
    assert pressure.writes[-1][0] == "batch_configuration"
    for name in ("batch_configuration", "batch_reset", "batch_start"):
        pressure.operation_finished.emit(name)
    controller._on_snapshot(
        replace(controller.snapshot, reorientation_state=20, heartbeat_alive=True)
    )
    pressure.operation_finished.emit("batch_start_end")
    observe(controller, 7, positions=(35,))
    assert pressure.writes[-1][0] == "batch_conveyor_start"
    pressure.operation_finished.emit("batch_conveyor_start")
    assert controller.state == BatchState.RUNNING
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].expected_array_mask == 4


def test_vision_loss_after_stop_does_not_interrupt_safe_stop_writes(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7)
    acknowledge(controller, 1)
    controller.stop()
    assert controller.state == BatchState.STOPPING
    controller.set_camera_fresh(False)
    controller.set_model_ready(False)
    assert controller.state == BatchState.STOPPING
    assert not any(name == "batch_abort" for name, _, _ in pressure.writes)


def test_wrong_enqueue_acknowledgement_cannot_advance_the_next_part(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7)
    acknowledge(controller, 2)
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"


def test_controls_are_locked_during_live_feed(continuous):
    controller, _pressure = continuous
    start(continuous)
    with pytest.raises(RuntimeError):
        controller.set_handoff_line_ratio(0.4)
    with pytest.raises(RuntimeError):
        controller.set_production_mode(ProductionMode.SNAPSHOT)


def test_repeated_frame_does_not_queue_before_line_and_crossing_needs_no_consensus(continuous):
    controller, _pressure = continuous
    start(continuous)
    first = multi_frame((60, 7), timestamp=controller._latest_frame.timestamp + 0.001)
    for _ in range(5):
        controller.accept_inference(first)
    assert controller._pending_enqueue is None
    observe(controller, 7, positions=(50, 40, 35))
    assert controller._pending_enqueue[0].expected_array_mask == 4
    assert len(controller._pending_enqueue[1].observations) == 1


@pytest.mark.parametrize("handoff_class, expected_mask", [(0, 1), (42, 0), (99, 0)])
def test_handoff_frame_overrides_earlier_stable_pose(continuous, handoff_class, expected_mask):
    controller, _pressure = continuous
    start(continuous)
    observe(controller, 7, positions=(80, 70, 60, 50, 40))
    assert controller._pending_enqueue is None
    observe(controller, handoff_class, positions=(35,))
    record, decision, _image, _path = controller._pending_enqueue
    assert record.expected_array_mask == expected_mask
    assert len(decision.observations) == (0 if handoff_class == 99 else 1)
    assert decision.timestamp == controller._latest_frame.timestamp


def test_first_detection_on_the_handoff_line_selects_profile_immediately(continuous):
    controller, pressure = continuous
    start(continuous)
    observe(controller, 7, positions=(35,))
    assert controller.state == BatchState.RUNNING
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].expected_array_mask == 4
    observe(controller, 0, positions=(30, 25, 20))
    assert [name for name, _, _ in pressure.writes].count("batch_enqueue:1") == 1


def test_real_plc_fault_still_stops_continuous_feed(continuous):
    controller, pressure = continuous
    start(continuous)
    controller._on_snapshot(replace(controller.snapshot, reorientation_fault_code=95))
    assert controller.state == BatchState.FAULT
    assert pressure.writes[-1][0] == "batch_abort"


def test_movable_yellow_line_changes_the_profile_commit_position(continuous):
    controller, pressure = continuous
    controller.set_handoff_line_ratio(0.5)
    assert controller.pose_cutoff_ratio == 0.5
    start(continuous)
    observe(controller, 7, positions=(90, 85, 80, 75, 70))
    assert controller._pending_enqueue is None
    observe(controller, 7, positions=(65, 60))
    assert controller._pending_enqueue is None
    observe(controller, 7, positions=(55,))
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].expected_array_mask == 4


def test_input_region_detections_are_ignored_until_fully_inside_then_handoff_works(continuous):
    controller, pressure = continuous
    controller.set_input_line_ratio(0.8)
    controller.accept_inference(multi_frame())
    start(continuous)
    warnings = []
    controller.warning_raised.connect(lambda code, text: warnings.append((code, text)))
    for class_id, position in ((99, 95), (7, 90), (0, 85)):
        observe(controller, class_id, positions=(position,))
        assert controller._last_tracks == ()
        assert controller._latest_frame.detections == ()
        assert controller._pending_enqueue is None
    assert warnings == []
    observe(controller, 7, positions=(74, 64, 54, 44, 35))
    assert controller.state == BatchState.RUNNING
    assert pressure.writes[-1][0] == "batch_enqueue:1"
    assert controller._pending_enqueue[0].pose_id == 7
    assert controller._pending_enqueue[0].expected_array_mask == 4
    with pytest.raises(RuntimeError):
        controller.set_input_line_ratio(0.9)
    assert controller.input_line_ratio == 0.8
