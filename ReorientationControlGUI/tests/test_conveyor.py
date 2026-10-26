from __future__ import annotations

import threading

import pytest

from bibazu_reorientation.conveyor import NOMINAL_MM_PER_FULL_STEP
from bibazu_reorientation.hardware.pressure import (
    BATCH_STATUS_SYMBOLS,
    OPTIONAL_STATUS_SYMBOLS,
    PressureAdapter,
    _AdsWorker,
    _Command,
)
from bibazu_reorientation.settings import AppSettings


class ConveyorPlc:
    """ADS stand-in which consumes pulse commands just as MAIN does."""

    def __init__(self) -> None:
        self.values = {
            "ConveyorServoVelocityOnly": True,
            "ConveyorServoCommissioned": False,
            "ConveyorServoMotorCount": 1,
            "ConveyorServoState": 0,
            "ConveyorServoFaultCode": 0,
            "ConveyorServoAccelerationRpmPerSec": 17.5,
            "ConveyorServo1TargetVelocity": 0,
            "ConveyorServo2TargetVelocity": 0,
            "StepperPosBusy": False,
            "StepperPosError": False,
            "GuiConveyorCalibrationValid": True,
            "GuiConveyorMmPerFullStep": NOMINAL_MM_PER_FULL_STEP,
            "GuiReorientationControlActive": False,
            "ReorientationSafeLatch": False,
            "GuiReorientationAbort": False,
            "GuiReorientationLegacyTakeover": False,
            **{f"OpenValve{i}": False for i in range(1, 25)},
        }
        self.writes: list[dict] = []
        self.bad_readback = False
        self.single_reads: list[str] = []
        self.batch_reads: list[tuple[str, ...]] = []

    def read_by_name(self, name, _kind):
        self.single_reads.append(name)
        return self.values[name.removeprefix("MAIN.")]

    def read_list_by_name(self, names, **_kwargs):
        self.batch_reads.append(tuple(names))
        result = {name: self.values[name.removeprefix("MAIN.")] for name in names}
        if self.bad_readback and "MAIN.ConveyorServoMotorCount" in names:
            result["MAIN.ConveyorServoMotorCount"] = 1
        return result

    def write_list_by_name(self, values, **_kwargs):
        self.writes.append(dict(values))
        for name, value in values.items():
            self.values[name.removeprefix("MAIN.")] = value
        self.values["GuiCalibrationStop"] = False
        self.values["GuiConveyorReset"] = False
        if (
            self.values.get("GuiReorientationAbort")
            and self.values["GuiReorientationControlActive"]
        ):
            self.values["ReorientationSafeLatch"] = True
        if self.values["GuiReorientationLegacyTakeover"] and self.values["ReorientationSafeLatch"]:
            self.values.update(
                GuiReorientationControlActive=False, ReorientationSafeLatch=False,
                GuiReorientationAbort=False, GuiReorientationLegacyTakeover=False,
            )
        for array in range(1, 5):
            if not self.values.get(f"GuiArrayEnabled{array}", False):
                for valve in range((array - 1) * 6 + 1, array * 6 + 1):
                    self.values[f"OpenValve{valve}"] = False
        return dict.fromkeys(values, 0)

    def close(self):
        pass


@pytest.fixture
def conveyor(qapp):
    worker = _AdsWorker(AppSettings())
    worker.plc = ConveyorPlc()
    completed, failed = [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(lambda name, error: failed.append((name, error)))
    yield worker, completed, failed
    worker.close()


def start_preparation(worker):
    cancelled = threading.Event()
    worker.prepare_conveyor(("test_prepare", cancelled))
    if worker.preparation_timer is not None:
        worker.preparation_timer.stop()
    return cancelled


def tick(worker):
    worker.preparation_resume_at = 0.0
    worker._prepare_tick()


def assert_no_motion(plc):
    assert not any(row.get("MAIN.GuiConveyorEnabled") for row in plc.writes)
    assert not any(
        value
        for row in plc.writes
        for key, value in row.items()
        if key.startswith("MAIN.GuiArrayEnabled")
    )


def test_preparation_configures_both_motors_without_motion_or_calibration_writes(conveyor):
    worker, completed, failed = conveyor
    start_preparation(worker)
    assert completed == []
    tick(worker)  # Standstill, then write stopped configuration.
    assert completed == []
    tick(worker)  # Reset pulse, consumed by the PLC.
    assert completed == []
    tick(worker)  # Require fault-free, calibrated, stopped status.
    assert completed == ["test_prepare"]
    assert failed == []
    plc = worker.plc
    configuration = next(row for row in plc.writes if "MAIN.ConveyorServoMotorCount" in row)
    assert configuration["MAIN.ConveyorServoMotorCount"] == 2
    assert configuration["MAIN.ConveyorServoDirection1"] == -1
    assert configuration["MAIN.ConveyorServoDirection2"] == -1
    assert configuration["MAIN.ConveyorServoMotor2Ratio"] == 1.0
    assert configuration["MAIN.ConveyorServoMaxMotorRpm"] == 0.0
    assert plc.values["ConveyorServoAccelerationRpmPerSec"] == 17.5
    assert all("MAIN.ConveyorServoAccelerationRpmPerSec" not in row for row in plc.writes)
    assert all("MAIN.GuiConveyorMmPerFullStep" not in row for row in plc.writes)
    assert_no_motion(plc)


def test_preparation_waits_for_standstill(conveyor):
    worker, completed, failed = conveyor
    worker.plc.values["StepperPosBusy"] = True
    worker.plc.values["ConveyorServo1TargetVelocity"] = 100
    start_preparation(worker)
    tick(worker)
    assert not any("MAIN.ConveyorServoMotorCount" in row for row in worker.plc.writes)
    assert completed == failed == []
    assert_no_motion(worker.plc)


@pytest.mark.parametrize("phase", ["standstill", "reset", "ready"])
def test_cancellation_in_every_preparation_phase_stops_without_completion(conveyor, phase):
    worker, completed, failed = conveyor
    cancelled = start_preparation(worker)
    for _ in range(["standstill", "reset", "ready"].index(phase)):
        tick(worker)
    cancelled.set()
    tick(worker)
    tick(worker)  # A late timer callback cannot revive the request.
    assert completed == []
    assert "cancelled" in failed[0][1]
    assert worker.preparation is None
    assert_no_motion(worker.plc)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ConveyorServoVelocityOnly": False}, "velocity-only"),
        ({"ConveyorServoAccelerationRpmPerSec": float("nan")}, "acceleration"),
        ({"ConveyorServoAccelerationRpmPerSec": 0.0}, "acceleration"),
    ],
)
def test_incompatible_plc_is_rejected(conveyor, change, message):
    worker, completed, failed = conveyor
    worker.plc.values.update(change)
    start_preparation(worker)
    assert completed == []
    assert message in failed[0][1].lower()
    assert_no_motion(worker.plc)


@pytest.mark.parametrize("failure", ["timeout", "fault", "calibration", "readback", "missing"])
def test_failed_preparation_never_completes_or_enables_transport(conveyor, failure):
    worker, completed, failed = conveyor
    start_preparation(worker)
    if failure == "readback":
        worker.plc.bad_readback = True
    elif failure == "missing":
        del worker.plc.values["ConveyorServoFaultCode"]
    elif failure == "timeout":
        worker.preparation_deadline = 0.0
    tick(worker)
    tick(worker)
    if failure == "fault":
        worker.plc.values["ConveyorServoFaultCode"] = 6
    if failure == "calibration":
        worker.plc.values["GuiConveyorCalibrationValid"] = False
    tick(worker)
    assert completed == []
    assert len(failed) == 1
    assert worker.preparation is None
    assert_no_motion(worker.plc)


def test_disconnect_cancels_pending_preparation(conveyor):
    worker, completed, failed = conveyor
    start_preparation(worker)
    plc = worker.plc
    worker.close()
    tick(worker)
    assert completed == []
    assert "disconnected" in failed[0][1]
    assert_no_motion(plc)


def test_queued_motion_write_is_rejected_after_cancellation(conveyor):
    worker, completed, failed = conveyor
    cancelled = threading.Event()
    command = _Command("late_start", {"MAIN.GuiConveyorEnabled": True}, True, cancelled)
    cancelled.set()
    worker.execute(command)
    assert completed == []
    assert "cancelled" in failed[0][1]
    assert_no_motion(worker.plc)


def test_timer_preparation_leaves_qt_event_loop_responsive(conveyor, qtbot):
    worker, completed, failed = conveyor
    worker.prepare_conveyor(("async_prepare", threading.Event()))
    assert completed == []
    qtbot.waitUntil(lambda: bool(completed or failed), timeout=2000)
    assert completed == ["async_prepare"]
    assert failed == []


def test_adapter_drops_results_from_an_earlier_start(qapp):
    adapter = PressureAdapter(AppSettings())
    completed, failed = [], []
    adapter.operation_finished.connect(completed.append)
    adapter.operation_failed.connect(lambda name, error: failed.append((name, error)))
    try:
        old_epoch = adapter._motion_cancelled
        adapter.cancel_preparation()
        adapter._motion_cancelled = threading.Event()
        adapter._operation_result("batch_configuration", "", old_epoch)
        adapter._operation_result("batch_configuration", "old failure", old_epoch)
        assert completed == failed == []
        adapter._operation_result("batch_configuration", "", adapter._motion_cancelled)
        assert completed == ["batch_configuration"]
    finally:
        adapter.shutdown()


def test_missing_live_conveyor_symbol_disconnects_without_a_successful_snapshot(conveyor):
    worker, _completed, _failed = conveyor
    del worker.plc.values["ConveyorServoFaultCode"]
    snapshots, connections = [], []
    worker.snapshot.connect(snapshots.append)
    worker.connection.connect(lambda connected, detail: connections.append((connected, detail)))
    worker._poll()
    assert snapshots == []
    assert connections[0][0] is False
    assert "ConveyorServoFaultCode" in connections[0][1]
    assert worker.plc.writes[-1]["MAIN.GuiConveyorEnabled"] is False


def test_cancellation_during_ads_enable_is_followed_by_stop(conveyor):
    worker, completed, failed = conveyor
    cancelled = threading.Event()
    original_write = worker.plc.write_list_by_name

    def cancel_during_enable(values, **kwargs):
        result = original_write(values, **kwargs)
        if values.get("MAIN.GuiConveyorEnabled"):
            cancelled.set()
        return result

    worker.plc.write_list_by_name = cancel_during_enable
    worker.execute(_Command("inflight_start", {"MAIN.GuiConveyorEnabled": True}, True, cancelled))
    assert completed == []
    assert "cancelled" in failed[0][1]
    assert worker.plc.writes[-1]["MAIN.GuiConveyorEnabled"] is False


def test_shutdown_stops_and_closes_before_thread_exits(qapp):
    adapter = PressureAdapter(AppSettings())
    plc = ConveyorPlc()
    adapter.worker.plc = plc
    adapter.shutdown()
    assert not adapter.thread.isRunning()
    assert adapter.worker.plc is None
    assert plc.values["GuiConveyorEnabled"] is False
    assert not plc.values["GuiReorientationControlActive"]
    assert not plc.values["ReorientationSafeLatch"]


@pytest.mark.parametrize("owner, latched", [(True, False), (True, True), (False, True)])
def test_close_releases_active_or_already_released_batch_latch_with_outputs_off(
    conveyor, owner, latched
):
    worker, _completed, _failed = conveyor
    plc = worker.plc
    plc.values.update(
        GuiReorientationControlActive=owner, ReorientationSafeLatch=latched,
        GuiConveyorEnabled=True, GuiArrayEnabled1=True, OpenValve1=True,
    )
    worker.close()
    assert worker.plc is None
    assert not plc.values["GuiReorientationControlActive"]
    assert not plc.values["ReorientationSafeLatch"]
    assert not plc.values["GuiReorientationAbort"]
    assert not plc.values["GuiConveyorEnabled"]
    assert all(not plc.values[f"GuiArrayEnabled{i}"] for i in range(1, 5))
    assert all(not plc.values[f"OpenValve{i}"] for i in range(1, 25))
    assert plc.writes[0]["MAIN.GuiConveyorEnabled"] is False
    assert any(row.get("MAIN.GuiReorientationLegacyTakeover") for row in plc.writes)
    assert not any(row.get("MAIN.GuiConveyorEnabled") for row in plc.writes)
    assert not any(
        row.get(f"MAIN.GuiArrayEnabled{i}") for row in plc.writes for i in range(1, 5)
    )


def test_enable_readback_failure_requests_stop(conveyor):
    worker, completed, failed = conveyor

    def unreadable_readback(*_args, **_kwargs):
        raise RuntimeError("ADS readback unavailable")

    worker.plc.read_list_by_name = unreadable_readback
    worker.execute(
        _Command("failed_start", {"MAIN.GuiConveyorEnabled": True}, True, threading.Event())
    )
    assert completed == []
    assert "readback unavailable" in failed[0][1]
    assert worker.plc.writes[-1]["MAIN.GuiConveyorEnabled"] is False


def test_lost_velocity_only_marker_stops_and_disconnects(conveyor):
    worker, _completed, _failed = conveyor
    worker.plc.values["ConveyorServoVelocityOnly"] = False
    snapshots, connections = [], []
    worker.snapshot.connect(snapshots.append)
    worker.connection.connect(lambda connected, detail: connections.append((connected, detail)))
    worker._poll()
    assert snapshots == []
    assert connections[0][0] is False
    assert "marker was lost" in connections[0][1]
    assert worker.plc.writes[-1]["MAIN.GuiConveyorEnabled"] is False


@pytest.fixture
def status_poll(conveyor):
    worker, _completed, _failed = conveyor
    worker.plc.values.update(dict.fromkeys(BATCH_STATUS_SYMBOLS, 0))
    worker.plc.values.update(dict.fromkeys(OPTIONAL_STATUS_SYMBOLS, 0))
    worker.plc.values.update(
        ReorientationQueueEnqueueAck=2,
        ReorientationQueueDepth=2,
        ReorientationWarningCode=5,
        ReorientationWarningCounter=7,
        ReorientationWarningSensor=3,
    )
    return worker


def test_status_poll_uses_three_batch_reads_instead_of_individual_roundtrips(status_poll):
    worker = status_poll
    snapshots = []
    worker.snapshot.connect(snapshots.append)
    worker._poll()
    worker._poll()
    assert len(snapshots) == 2
    assert worker.plc.single_reads == []
    assert len(worker.plc.batch_reads) == 6
    assert snapshots[-1].batch_enqueue_ack == snapshots[-1].batch_queue_depth == 2
    assert snapshots[-1].reorientation_warning_code == 5
    assert snapshots[-1].reorientation_warning_counter == 7
    assert snapshots[-1].calibration_valid


@pytest.mark.parametrize("invalid", [None, "symbol not found", float("nan")])
def test_batch_read_error_cannot_become_a_successful_required_status(status_poll, invalid):
    worker = status_poll
    worker.plc.values["ReorientationHeartbeatAlive"] = invalid
    snapshots, connections = [], []
    worker.snapshot.connect(snapshots.append)
    worker.connection.connect(lambda connected, detail: connections.append((connected, detail)))
    worker._poll()
    assert snapshots == []
    assert connections[0][0] is False
    assert "ReorientationHeartbeatAlive" in connections[0][1]
    assert worker.plc.writes[-1]["MAIN.GuiConveyorEnabled"] is False


def test_missing_optional_symbol_is_discovered_once_then_remaining_values_are_batched(status_poll):
    worker = status_poll
    del worker.plc.values["ReorientationWarningPreviousSequence"]
    snapshots = []
    worker.snapshot.connect(snapshots.append)
    worker._poll()
    assert len(snapshots) == 1
    assert worker.plc.single_reads
    worker.plc.single_reads.clear()
    worker.plc.batch_reads.clear()
    worker._poll()
    assert len(snapshots) == 2
    assert worker.plc.single_reads == []
    assert len(worker.plc.batch_reads) == 3
    assert snapshots[-1].reorientation_warning_previous_sequence == 0
    assert snapshots[-1].reorientation_warning_code == 5


def test_optional_sum_read_item_error_is_retried_on_next_poll(status_poll):
    worker = status_poll
    worker.plc.values["ReorientationWarningCode"] = "ADS item error"
    snapshots = []
    worker.snapshot.connect(snapshots.append)
    worker._poll()
    assert snapshots[-1].reorientation_warning_code == 0
    worker.plc.values["ReorientationWarningCode"] = 5
    worker._poll()
    assert snapshots[-1].reorientation_warning_code == 5
