from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from bibazu_reorientation.models import (
    ConveyorCalibration,
    PlcSnapshot,
    PressureBaseline,
    ProfileWritePlan,
)
from bibazu_reorientation.settings import AppSettings

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _Command:
    name: str
    values: dict[str, bool | int | float] | None = None
    verify: bool = False


class _AdsWorker(QObject):
    connection = pyqtSignal(bool, str)
    snapshot = pyqtSignal(object)
    baseline = pyqtSignal(object)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str, str)

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self.plc: Any = None
        self.poller: QTimer | None = None

    @pyqtSlot()
    def connect_plc(self) -> None:
        if self.plc is not None:
            self.connection.emit(True, "ADS is already connected")
            return
        try:
            import pyads

            self.plc = pyads.Connection(
                self.settings.plc_ams_net_id, self.settings.plc_port, self.settings.plc_ip
            )
            self.plc.open()
            ads_state, device_state = self.plc.read_state()
            # Unlike Automated Image Capture, this application requires the batch
            # reorientation PLC contract. Fail explicitly when an older PLC build is
            # active instead of silently replacing missing symbols with safe-looking
            # defaults.
            self.plc.read_by_name("MAIN.ReorientationState", pyads.PLCTYPE_UINT)
            self.plc.read_by_name(
                "MAIN.ReorientationQueueCapacity", pyads.PLCTYPE_UINT
            )
            self.connection.emit(
                True,
                f"ADS connected (AMS {ads_state}, device {device_state}; batch contract v2)",
            )
            self.baseline.emit(self._read_baseline())
            self.poller = QTimer(self)
            self.poller.setInterval(100)
            self.poller.timeout.connect(self._poll)
            self.poller.start()
        except Exception as exc:
            if self.plc is not None:
                try:
                    self.plc.close()
                except Exception:
                    LOGGER.exception("ADS close after failed connect failed")
                self.plc = None
            self.connection.emit(False, str(exc) or type(exc).__name__)

    def _read(self, name: str, plc_type: Any, default: Any) -> Any:
        try:
            return self.plc.read_by_name(name, plc_type)
        except Exception:
            return default

    def _read_baseline(self) -> PressureBaseline:
        import pyads

        return PressureBaseline(
            light_barrier_debounce_ms=int(
                self._read("MAIN.GuiBarrierCalibrationDebounceMs", pyads.PLCTYPE_UDINT, 20)
            ),
            light_barrier_inverted=tuple(
                bool(self._read(f"MAIN.GuiLightBarrierInvert{i}", pyads.PLCTYPE_BOOL, True))
                for i in range(1, 9)
            ),
            light_barrier_debounce_enabled=tuple(
                bool(
                    self._read(f"MAIN.GuiLightBarrierDebounceEnabled{i}", pyads.PLCTYPE_BOOL, False)
                )
                for i in range(1, 9)
            ),
            conveyor_speed_mm_per_sec=float(
                self._read("MAIN.GuiConveyorSpeedMmPerSec", pyads.PLCTYPE_REAL, 0.0)
            ),
            conveyor_max_speed_mm_per_sec=float(
                self._read("MAIN.GuiConveyorMaxSpeedMmPerSec", pyads.PLCTYPE_REAL, 1000.0)
            ),
            conveyor_enabled=bool(self._read("MAIN.GuiConveyorEnabled", pyads.PLCTYPE_BOOL, False)),
            conveyor_reverse=bool(self._read("MAIN.GuiConveyorReverse", pyads.PLCTYPE_BOOL, False)),
            conveyor_calibration=ConveyorCalibration(
                float(self._read("MAIN.GuiCalibrationMarkerDistanceMm", pyads.PLCTYPE_REAL, 315.0)),
                float(self._read("MAIN.GuiConveyorMmPerFullStep", pyads.PLCTYPE_REAL, 0.32960026)),
                bool(self._read("MAIN.GuiConveyorCalibrationValid", pyads.PLCTYPE_BOOL, False)),
            ),
        )

    @pyqtSlot(object)
    def execute(self, command: _Command) -> None:
        if self.plc is None:
            self.failed.emit(command.name, "ADS is not connected")
            return
        try:
            values = command.values or {}
            errors = self.plc.write_list_by_name(values, cache_symbol_info=True)
            failed = {
                name: error for name, error in errors.items() if error and error != "no error"
            }
            if failed:
                raise RuntimeError(f"ADS Sum Write failed: {failed}")
            if command.verify:
                readback = self.plc.read_list_by_name(list(values), cache_symbol_info=True)
                for name, expected in values.items():
                    actual = readback[name]
                    equal = (
                        actual == expected
                        if not isinstance(expected, float)
                        else abs(actual - expected) < 1e-6
                    )
                    if not equal:
                        raise RuntimeError(
                            f"Readback {name}: expected {expected!r}, read {actual!r}"
                        )
            self.completed.emit(command.name)
        except Exception as exc:
            self.failed.emit(command.name, str(exc))

    def _poll(self) -> None:
        if self.plc is None:
            return
        try:
            import pyads

            def r(name: str, plc_type: Any, default: Any) -> Any:
                return self._read(f"MAIN.{name}", plc_type, default)

            def required_r(name: str, plc_type: Any) -> Any:
                return self.plc.read_by_name(f"MAIN.{name}", plc_type)

            self.snapshot.emit(
                PlcSnapshot(
                    connected=True,
                    conveyor_motion_state=int(r("ConveyorMotionState", pyads.PLCTYPE_UINT, 0)),
                    stepper_busy=bool(r("StepperPosBusy", pyads.PLCTYPE_BOOL, False)),
                    stepper_error=bool(r("StepperPosError", pyads.PLCTYPE_BOOL, False)),
                    calibration_valid=bool(
                        r("GuiConveyorCalibrationValid", pyads.PLCTYPE_BOOL, False)
                    ),
                    array_states=tuple(
                        int(r(f"State{i if i > 1 else ''}", pyads.PLCTYPE_INT, 2))
                        for i in range(1, 5)
                    ),
                    pending_mask=sum(
                        1 << (i - 1)
                        for i in range(1, 5)
                        if r(f"Array{i}TriggerPending", pyads.PLCTYPE_BOOL, False)
                    ),
                    open_valve_mask=sum(
                        1 << (i - 1)
                        for i in range(1, 25)
                        if r(f"OpenValve{i}", pyads.PLCTYPE_BOOL, False)
                    ),
                    vtem_error_codes=(
                        int(r("ErrorCode0", pyads.PLCTYPE_INT, 0)),
                        int(r("ErrorCode1", pyads.PLCTYPE_INT, 0)),
                    ),
                    light_barriers_stable=tuple(
                        bool(r(f"LightBarrierStable{i}", pyads.PLCTYPE_BOOL, False))
                        for i in range(1, 9)
                    ),
                    reorientation_state=int(required_r("ReorientationState", pyads.PLCTYPE_UINT)),
                    reorientation_fault_code=int(
                        required_r("ReorientationFaultCode", pyads.PLCTYPE_UINT)
                    ),
                    heartbeat_alive=bool(
                        required_r("ReorientationHeartbeatAlive", pyads.PLCTYPE_BOOL)
                    ),
                    heartbeat_ack=int(required_r("ReorientationHeartbeatAck", pyads.PLCTYPE_UDINT)),
                    busy=bool(required_r("ReorientationBusy", pyads.PLCTYPE_BOOL)),
                    exit_seen=bool(required_r("ReorientationExitSeen", pyads.PLCTYPE_BOOL)),
                    arrays_idle=bool(required_r("ReorientationArraysIdle", pyads.PLCTYPE_BOOL)),
                    expected_array_mask=int(
                        required_r("ReorientationExpectedArrayMask", pyads.PLCTYPE_BYTE)
                    ),
                    triggered_array_mask=int(
                        required_r("ReorientationTriggeredArrayMask", pyads.PLCTYPE_BYTE)
                    ),
                    complete=bool(required_r("ReorientationComplete", pyads.PLCTYPE_BOOL)),
                    cycle_counter=int(required_r("ReorientationCycleCounter", pyads.PLCTYPE_UDINT)),
                    batch_queue_depth=int(
                        required_r("ReorientationQueueDepth", pyads.PLCTYPE_UINT)
                    ),
                    batch_queue_capacity=int(
                        required_r("ReorientationQueueCapacity", pyads.PLCTYPE_UINT)
                    ),
                    batch_enqueue_ack=int(
                        required_r("ReorientationQueueEnqueueAck", pyads.PLCTYPE_UDINT)
                    ),
                    batch_entered_count=int(
                        required_r("ReorientationQueueEnteredCount", pyads.PLCTYPE_UDINT)
                    ),
                    batch_completed_count=int(
                        required_r("ReorientationQueueCompletedCount", pyads.PLCTYPE_UDINT)
                    ),
                    batch_bypass_count=int(
                        required_r("ReorientationQueueBypassCount", pyads.PLCTYPE_UDINT)
                    ),
                    batch_sensor_sequences=tuple(
                        int(
                            required_r(
                                f"ReorientationSensorSequence{i}", pyads.PLCTYPE_UDINT
                            )
                        )
                        for i in range(1, 9)
                    ),
                    batch_result_available=bool(
                        required_r("ReorientationResultAvailable", pyads.PLCTYPE_BOOL)
                    ),
                    batch_result_sequence=int(
                        required_r("ReorientationResultSequence", pyads.PLCTYPE_UDINT)
                    ),
                    batch_result_triggered_mask=int(
                        required_r("ReorientationResultTriggeredMask", pyads.PLCTYPE_BYTE)
                    ),
                    batch_result_fault_code=int(
                        required_r("ReorientationResultFaultCode", pyads.PLCTYPE_UINT)
                    ),
                    reorientation_fault_detail=int(
                        r("ReorientationFaultDetail", pyads.PLCTYPE_UINT, 0)
                    ),
                    reorientation_fault_sensor=int(
                        r("ReorientationFaultSensor", pyads.PLCTYPE_UINT, 0)
                    ),
                    reorientation_fault_expected_sequence=int(
                        r("ReorientationFaultExpectedSequence", pyads.PLCTYPE_UDINT, 0)
                    ),
                    reorientation_fault_previous_sequence=int(
                        r("ReorientationFaultPreviousSequence", pyads.PLCTYPE_UDINT, 0)
                    ),
                    reorientation_fault_queue_ack=int(
                        r("ReorientationFaultQueueAck", pyads.PLCTYPE_UDINT, 0)
                    ),
                    reorientation_fault_queue_slot_sequence=int(
                        r("ReorientationFaultQueueSlotSequence", pyads.PLCTYPE_UDINT, 0)
                    ),
                    reorientation_fault_barrier_stable_mask=int(
                        r("ReorientationFaultBarrierStableMask", pyads.PLCTYPE_BYTE, 0)
                    ),
                    reorientation_warning_code=int(
                        r("ReorientationWarningCode", pyads.PLCTYPE_UINT, 0)
                    ),
                    reorientation_warning_sensor=int(
                        r("ReorientationWarningSensor", pyads.PLCTYPE_UINT, 0)
                    ),
                    reorientation_warning_sequence=int(
                        r("ReorientationWarningSequence", pyads.PLCTYPE_UDINT, 0)
                    ),
                    reorientation_warning_previous_sequence=int(
                        r(
                            "ReorientationWarningPreviousSequence",
                            pyads.PLCTYPE_UDINT,
                            0,
                        )
                    ),
                    reorientation_warning_skipped_barrier_mask=int(
                        r(
                            "ReorientationWarningSkippedBarrierMask",
                            pyads.PLCTYPE_BYTE,
                            0,
                        )
                    ),
                    reorientation_warning_counter=int(
                        r("ReorientationWarningCounter", pyads.PLCTYPE_UDINT, 0)
                    ),
                )
            )
        except Exception as exc:
            if self.poller:
                self.poller.stop()
            self.connection.emit(
                False,
                "ADS/PLC contract is no longer readable: " + (str(exc) or type(exc).__name__),
            )

    @pyqtSlot()
    def close(self) -> None:
        if self.poller:
            self.poller.stop()
        if self.plc:
            try:
                self.plc.close()
            except Exception:
                LOGGER.exception("ADS close failed")
        self.plc = None
        self.connection.emit(False, "Disconnected by operator")


class PressureAdapter(QObject):
    connection_changed = pyqtSignal(bool, str)
    snapshot_changed = pyqtSignal(object)
    baseline_ready = pyqtSignal(object)
    operation_finished = pyqtSignal(str)
    operation_failed = pyqtSignal(str, str)
    _connect = pyqtSignal()
    _execute = pyqtSignal(object)
    _close = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.thread = QThread(self)
        self.worker = _AdsWorker(settings)
        self.worker.moveToThread(self.thread)
        self._connect.connect(self.worker.connect_plc)
        self._execute.connect(self.worker.execute)
        self._close.connect(self.worker.close)
        self.worker.connection.connect(self.connection_changed)
        self.worker.snapshot.connect(self.snapshot_changed)
        self.worker.baseline.connect(self.baseline_ready)
        self.worker.completed.connect(self.operation_finished)
        self.worker.failed.connect(self.operation_failed)
        self.thread.start()

    def connect_device(self) -> None:
        self._connect.emit()

    def disconnect_device(self) -> None:
        self._close.emit()

    def write(self, name: str, values: dict[str, bool | int | float], verify: bool = False) -> None:
        self._execute.emit(_Command(name, values, verify))

    def stage(self, plan: ProfileWritePlan) -> None:
        self.write("safe_stop", plan.safe_stop, True)

    def shutdown(self) -> None:
        self._close.emit()
        self.thread.quit()
        self.thread.wait(2000)
