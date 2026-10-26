from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from bibazu_reorientation.conveyor import TANDEM_CONFIGURATION
from bibazu_reorientation.models import (
    ConveyorCalibration,
    PlcSnapshot,
    PressureBaseline,
    ProfileWritePlan,
)
from bibazu_reorientation.settings import AppSettings

LOGGER = logging.getLogger(__name__)

CONVEYOR_STATUS_SYMBOLS = (
    "ConveyorServoVelocityOnly",
    "ConveyorServoCommissioned",
    "ConveyorServoMotorCount",
    "ConveyorServoState",
    "ConveyorServoFaultCode",
    "ConveyorServoAccelerationRpmPerSec",
    "ConveyorServo1TargetVelocity",
    "ConveyorServo2TargetVelocity",
    "StepperPosBusy",
    "StepperPosError",
    "GuiConveyorCalibrationValid",
    "GuiConveyorMmPerFullStep",
)
BATCH_STATUS_SYMBOLS = (
    "ConveyorMotionState",
    "ReorientationState",
    "ReorientationFaultCode",
    "ReorientationHeartbeatAlive",
    "ReorientationHeartbeatAck",
    "ReorientationBusy",
    "ReorientationExitSeen",
    "ReorientationArraysIdle",
    "ReorientationExpectedArrayMask",
    "ReorientationTriggeredArrayMask",
    "ReorientationComplete",
    "ReorientationCycleCounter",
    "ReorientationQueueDepth",
    "ReorientationQueueCapacity",
    "ReorientationQueueEnqueueAck",
    "ReorientationQueueEnteredCount",
    "ReorientationQueueCompletedCount",
    "ReorientationQueueBypassCount",
    *(f"ReorientationSensorSequence{i}" for i in range(1, 9)),
    "ReorientationResultAvailable",
    "ReorientationResultSequence",
    "ReorientationResultTriggeredMask",
    "ReorientationResultFaultCode",
)
OPTIONAL_STATUS_SYMBOLS = (
    "State", "State2", "State3", "State4",
    *(f"Array{i}TriggerPending" for i in range(1, 5)),
    *(f"OpenValve{i}" for i in range(1, 25)),
    "ErrorCode0", "ErrorCode1",
    *(f"LightBarrierStable{i}" for i in range(1, 9)),
    "ReorientationFaultDetail",
    "ReorientationFaultSensor",
    "ReorientationFaultExpectedSequence",
    "ReorientationFaultPreviousSequence",
    "ReorientationFaultQueueAck",
    "ReorientationFaultQueueSlotSequence",
    "ReorientationFaultBarrierStableMask",
    "ReorientationWarningCode",
    "ReorientationWarningSensor",
    "ReorientationWarningSequence",
    "ReorientationWarningPreviousSequence",
    "ReorientationWarningSkippedBarrierMask",
    "ReorientationWarningCounter",
)


@dataclass(slots=True, frozen=True)
class _Command:
    name: str
    values: dict[str, bool | int | float] | None = None
    verify: bool = False
    cancelled: threading.Event | None = None
    epoch: threading.Event | None = None


class _AdsWorker(QObject):
    connection = pyqtSignal(bool, str)
    snapshot = pyqtSignal(object)
    baseline = pyqtSignal(object)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str, str)
    reported = pyqtSignal(str, str, object)

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self.plc: Any = None
        self.poller: QTimer | None = None
        self.preparation_timer: QTimer | None = None
        self.preparation: tuple[str, threading.Event] | None = None
        self.preparation_phase = ""
        self.preparation_deadline = 0.0
        self.preparation_resume_at = 0.0
        self._optional_status_symbols: tuple[str, ...] | None = None

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
            self._optional_status_symbols = None
            ads_state, device_state = self.plc.read_state()
            # Unlike Automated Image Capture, this application requires the batch
            # reorientation PLC contract. Fail explicitly when an older PLC build is
            # active instead of silently replacing missing symbols with safe-looking
            # defaults.
            self.plc.read_by_name("MAIN.ReorientationState", pyads.PLCTYPE_UINT)
            self.plc.read_by_name("MAIN.ReorientationQueueCapacity", pyads.PLCTYPE_UINT)
            if not self._conveyor_status()["ConveyorServoVelocityOnly"]:
                raise RuntimeError("PLC conveyor is not the velocity-only Beckhoff tandem version")
            baseline = self._read_baseline()
            self.connection.emit(
                True,
                f"ADS connected (AMS {ads_state}, device {device_state}; batch contract v2)",
            )
            self.baseline.emit(baseline)
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

        force_response_delays_ms = tuple(
            float(
                self._read(
                    f"MAIN.GuiForceSingleNozzleResponseDelayMs{i}",
                    pyads.PLCTYPE_REAL,
                    8.7,
                )
            )
            for i in range(1, 5)
        )
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
                self.plc.read_by_name("MAIN.GuiConveyorSpeedMmPerSec", pyads.PLCTYPE_REAL)
            ),
            conveyor_max_speed_mm_per_sec=float(
                self.plc.read_by_name("MAIN.GuiConveyorMaxSpeedMmPerSec", pyads.PLCTYPE_REAL)
            ),
            conveyor_enabled=bool(
                self.plc.read_by_name("MAIN.GuiConveyorEnabled", pyads.PLCTYPE_BOOL)
            ),
            conveyor_reverse=bool(
                self.plc.read_by_name("MAIN.GuiConveyorReverse", pyads.PLCTYPE_BOOL)
            ),
            conveyor_calibration=ConveyorCalibration(
                float(
                    self.plc.read_by_name("MAIN.GuiCalibrationMarkerDistanceMm", pyads.PLCTYPE_REAL)
                ),
                float(self.plc.read_by_name("MAIN.GuiConveyorMmPerFullStep", pyads.PLCTYPE_REAL)),
                bool(self.plc.read_by_name("MAIN.GuiConveyorCalibrationValid", pyads.PLCTYPE_BOOL)),
            ),
            force_response_delays_ms=force_response_delays_ms,
            force_single_nozzle_response_delays_ms=force_response_delays_ms,
        )

    @pyqtSlot(object)
    def execute(self, command: _Command) -> None:
        if self.plc is None:
            self._report(command.name, "ADS is not connected", command.epoch)
            return
        try:
            values = command.values or {}
            if command.cancelled is not None and command.cancelled.is_set():
                raise RuntimeError("Conveyor start cancelled")
            self._write_values(values, command.verify)
            if command.cancelled is not None and command.cancelled.is_set():
                raise RuntimeError("Conveyor start cancelled")
            self._report(command.name, "", command.epoch)
        except Exception as exc:
            if command.cancelled is not None:
                try:
                    self._write_values(self._preparation_stop_values())
                except Exception:
                    LOGGER.exception("Conveyor stop after failed motion write failed")
            self._report(command.name, str(exc), command.epoch)

    def _report(self, name: str, error: str, epoch: threading.Event | None) -> None:
        if error:
            self.failed.emit(name, error)
        else:
            self.completed.emit(name)
        self.reported.emit(name, error, epoch)

    def _write_values(self, values: dict, verify: bool = False) -> None:
        errors = self.plc.write_list_by_name(values, cache_symbol_info=True)
        failed = {name: error for name, error in errors.items() if error and error != "no error"}
        if failed:
            raise RuntimeError(f"ADS Sum Write failed: {failed}")
        if verify:
            readback = self.plc.read_list_by_name(list(values), cache_symbol_info=True)
            for name, expected in values.items():
                actual = readback[name]
                equal = (
                    actual == expected
                    if not isinstance(expected, float)
                    else abs(actual - expected) < 1e-6
                )
                if not equal:
                    raise RuntimeError(f"Readback {name}: expected {expected!r}, read {actual!r}")

    @staticmethod
    def _preparation_stop_values() -> dict[str, bool]:
        return {
            "MAIN.GuiConveyorEnabled": False,
            "MAIN.GuiCalibrationStop": True,
            "MAIN.GuiConveyorCalibrationMode": False,
            "MAIN.GuiVelocityCheckMode": False,
            "MAIN.GuiForceDelayMeasurementEnabled": False,
            **{f"MAIN.GuiArrayEnabled{i}": False for i in range(1, 5)},
        }

    def _status_values(self, names: tuple[str, ...], *, required: bool = True) -> dict:
        values = self.plc.read_list_by_name(
            [f"MAIN.{name}" for name in names], cache_symbol_info=True
        )
        result = {}
        for name in names:
            value = values.get(f"MAIN.{name}")
            # pyads returns an ADS error string for a failed Sum Read item.
            # Never coerce that string to a successful Boolean status.
            if type(value) not in {bool, int, float} or not math.isfinite(value):
                if required:
                    raise RuntimeError(f"Required PLC status MAIN.{name} unreadable: {value!r}")
                continue
            result[name] = value
        return result

    def _conveyor_status(self) -> dict:
        return self._status_values(CONVEYOR_STATUS_SYMBOLS)

    def _optional_status(self) -> dict:
        if self._optional_status_symbols is not None:
            if not self._optional_status_symbols:
                return {}
            return self._status_values(self._optional_status_symbols, required=False)
        try:
            values = self._status_values(OPTIONAL_STATUS_SYMBOLS, required=False)
            self._optional_status_symbols = OPTIONAL_STATUS_SYMBOLS
        except Exception:
            # Older batch contracts can lack optional diagnostics. Discover
            # available symbols once, then batch only those on later polls.
            values = {}
            for name in OPTIONAL_STATUS_SYMBOLS:
                value = self._read(f"MAIN.{name}", None, None)
                if type(value) in {bool, int, float} and math.isfinite(value):
                    values[name] = value
            self._optional_status_symbols = tuple(values)
        return values

    @pyqtSlot(object)
    def prepare_conveyor(self, request: tuple[str, threading.Event]) -> None:
        name, cancelled = request
        if self.preparation is not None:
            if self.preparation[1].is_set():
                self._finish_preparation("Conveyor preparation cancelled")
            else:
                self._report(name, "Conveyor preparation is already active", cancelled)
                return
        self.preparation = request
        try:
            if cancelled.is_set():
                raise RuntimeError("Conveyor preparation cancelled")
            if self.plc is None:
                raise RuntimeError("ADS is not connected")
            status = self._conveyor_status()
            if not status["ConveyorServoVelocityOnly"]:
                raise RuntimeError("PLC conveyor is not the velocity-only Beckhoff tandem version")
            acceleration = float(status["ConveyorServoAccelerationRpmPerSec"])
            if not math.isfinite(acceleration) or not 0.1 <= acceleration <= 300.0:
                raise RuntimeError("PLC conveyor acceleration must be between 0.1 and 300 rpm/s")
            stop_values = self._preparation_stop_values()
            stop_pulse = {"MAIN.GuiCalibrationStop": stop_values.pop("MAIN.GuiCalibrationStop")}
            self._write_values(stop_values, True)
            self._write_values(stop_pulse)
            self.preparation_phase = "standstill"
            self.preparation_deadline = time.monotonic() + 8.0
            self.preparation_resume_at = 0.0
            if self.preparation_timer is None:
                self.preparation_timer = QTimer(self)
                self.preparation_timer.setInterval(50)
                self.preparation_timer.timeout.connect(self._prepare_tick)
            self.preparation_timer.start()
        except Exception as exc:
            self._finish_preparation(str(exc))

    def _prepare_tick(self) -> None:
        if self.preparation is None:
            return
        try:
            _name, cancelled = self.preparation
            if cancelled.is_set():
                raise RuntimeError("Conveyor preparation cancelled")
            now = time.monotonic()
            if now >= self.preparation_deadline:
                raise RuntimeError(
                    f"Conveyor preparation timed out during {self.preparation_phase}"
                )
            if now < self.preparation_resume_at:
                return
            status = self._conveyor_status()
            stopped = (
                not status["StepperPosBusy"]
                and status["ConveyorServo1TargetVelocity"] == 0
                and status["ConveyorServo2TargetVelocity"] == 0
            )
            if not status["ConveyorServoVelocityOnly"]:
                raise RuntimeError("PLC velocity-only conveyor marker was lost")
            if self.preparation_phase == "standstill" and stopped:
                # Acceleration remains the live PLC setting; never restore it from profiles.
                self._write_values(dict(TANDEM_CONFIGURATION), True)
                self.preparation_phase = "reset"
                self.preparation_resume_at = now + 0.25
                self.preparation_deadline = now + 8.0
            elif self.preparation_phase == "reset":
                self._write_values({"MAIN.GuiConveyorReset": True})
                self.preparation_phase = "ready"
                self.preparation_resume_at = now + 0.05
            elif self.preparation_phase == "ready" and stopped:
                if status["ConveyorServoFaultCode"] or status["StepperPosError"]:
                    raise RuntimeError(
                        f"Beckhoff conveyor drive fault {status['ConveyorServoFaultCode']}"
                    )
                if status["ConveyorServoState"] != 0 or not status["ConveyorServoCommissioned"]:
                    return
                if status["ConveyorServoMotorCount"] != 2:
                    raise RuntimeError("Beckhoff conveyor must use both motors")
                scale = float(status["GuiConveyorMmPerFullStep"])
                if (
                    not status["GuiConveyorCalibrationValid"]
                    or not math.isfinite(scale)
                    or scale <= 0
                ):
                    raise RuntimeError("PLC conveyor calibration is invalid")
                self._write_values({"MAIN.GuiConveyorReset": False}, True)
                self._finish_preparation()
        except Exception as exc:
            self._finish_preparation(str(exc))

    def _finish_preparation(self, error: str = "") -> None:
        request, self.preparation = self.preparation, None
        if self.preparation_timer is not None:
            self.preparation_timer.stop()
        if request is None:
            return
        name, cancelled = request
        if cancelled.is_set() and not error:
            error = "Conveyor preparation cancelled"
        if error:
            cancelled.set()
            try:
                if self.plc is not None:
                    self._write_values(
                        {**self._preparation_stop_values(), "MAIN.GuiConveyorReset": False}
                    )
            except Exception:
                LOGGER.exception("Conveyor stop after preparation failure failed")
            self._report(name, error, cancelled)
        else:
            self._report(name, "", cancelled)

    def _poll(self) -> None:
        if self.plc is None:
            return
        try:
            import pyads

            conveyor = self._conveyor_status()
            if not conveyor["ConveyorServoVelocityOnly"]:
                raise RuntimeError("PLC velocity-only conveyor marker was lost")
            batch = self._status_values(BATCH_STATUS_SYMBOLS)
            optional = self._optional_status()

            def r(name: str, _plc_type: Any, default: Any) -> Any:
                return optional.get(name, default)

            def required_r(name: str, _plc_type: Any) -> Any:
                return batch[name]

            self.snapshot.emit(
                PlcSnapshot(
                    connected=True,
                    conveyor_velocity_only=bool(conveyor["ConveyorServoVelocityOnly"]),
                    conveyor_commissioned=bool(conveyor["ConveyorServoCommissioned"]),
                    conveyor_motor_count=int(conveyor["ConveyorServoMotorCount"]),
                    conveyor_servo_state=int(conveyor["ConveyorServoState"]),
                    conveyor_fault_code=int(conveyor["ConveyorServoFaultCode"]),
                    conveyor_acceleration_rpm_per_sec=float(
                        conveyor["ConveyorServoAccelerationRpmPerSec"]
                    ),
                    conveyor_target_velocities=(
                        int(conveyor["ConveyorServo1TargetVelocity"]),
                        int(conveyor["ConveyorServo2TargetVelocity"]),
                    ),
                    conveyor_motion_state=int(
                        required_r("ConveyorMotionState", pyads.PLCTYPE_UINT)
                    ),
                    stepper_busy=bool(conveyor["StepperPosBusy"]),
                    stepper_error=bool(conveyor["StepperPosError"]),
                    calibration_valid=bool(conveyor["GuiConveyorCalibrationValid"]),
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
                        int(required_r(f"ReorientationSensorSequence{i}", pyads.PLCTYPE_UDINT))
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
            self._finish_preparation("ADS/PLC contract lost")
            try:
                self._write_values(self._preparation_stop_values())
            except Exception:
                LOGGER.exception("Conveyor stop after ADS/PLC contract loss failed")
            if self.poller:
                self.poller.stop()
            self.connection.emit(
                False,
                "ADS/PLC contract is no longer readable: " + (str(exc) or type(exc).__name__),
            )

    @pyqtSlot()
    def close(self) -> None:
        self._finish_preparation("ADS disconnected")
        if self.plc is not None:
            try:
                stopped = self._preparation_stop_values()
                calibration_stop = {
                    "MAIN.GuiCalibrationStop": stopped.pop("MAIN.GuiCalibrationStop")
                }
                self._write_values(stopped, True)
                self._write_values(calibration_stop)  # PLC consumes this pulse immediately.
                self._release_to_legacy()
            except Exception:
                LOGGER.exception("Stopped handover to Pressure Control before disconnect failed")
        if self.poller:
            self.poller.stop()
        if self.plc:
            try:
                self.plc.close()
            except Exception:
                LOGGER.exception("ADS close failed")
        self.plc = None
        self.connection.emit(False, "Disconnected by operator")

    def _release_to_legacy(self) -> None:
        """Clear our stopped batch latch without enabling transport or valves."""
        ownership = ("GuiReorientationControlActive", "ReorientationSafeLatch")
        if not any(self._status_values(ownership).values()):
            return
        # The PLC accepts takeover only after its safe latch is set. Holding
        # both commands covers an active owner as well as a released owner
        # whose operator-stop latch still blocks legacy conveyor commands.
        self._write_values({
            "MAIN.GuiReorientationAbort": True,
            "MAIN.GuiReorientationStart": False,
            "MAIN.GuiReorientationFinish": False,
            "MAIN.GuiReorientationLegacyTakeover": True,
        })
        stopped = (
            *ownership,
            "GuiReorientationLegacyTakeover", "GuiReorientationAbort", "GuiConveyorEnabled",
            *(f"GuiArrayEnabled{i}" for i in range(1, 5)),
            *(f"OpenValve{i}" for i in range(1, 25)),
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not any(self._status_values(stopped).values()):
                return
            time.sleep(0.02)
        raise RuntimeError("PLC did not acknowledge stopped handover to Pressure Control")

    @pyqtSlot()
    def shutdown(self) -> None:
        # Quit only after the queued stop/close has run in the ADS thread.
        self.close()
        QThread.currentThread().quit()


class PressureAdapter(QObject):
    connection_changed = pyqtSignal(bool, str)
    snapshot_changed = pyqtSignal(object)
    baseline_ready = pyqtSignal(object)
    operation_finished = pyqtSignal(str)
    operation_failed = pyqtSignal(str, str)
    _connect = pyqtSignal()
    _execute = pyqtSignal(object)
    _prepare = pyqtSignal(object)
    _close = pyqtSignal()
    _shutdown = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.thread = QThread(self)
        self.worker = _AdsWorker(settings)
        self._motion_cancelled = threading.Event()
        self.worker.moveToThread(self.thread)
        self._connect.connect(self.worker.connect_plc)
        self._execute.connect(self.worker.execute)
        self._prepare.connect(self.worker.prepare_conveyor)
        self._close.connect(self.worker.close)
        self._shutdown.connect(self.worker.shutdown)
        self.worker.connection.connect(self.connection_changed)
        self.worker.snapshot.connect(self.snapshot_changed)
        self.worker.baseline.connect(self.baseline_ready)
        self.worker.reported.connect(self._operation_result)
        self.thread.start()

    @pyqtSlot(str, str, object)
    def _operation_result(self, name: str, error: str, epoch: threading.Event) -> None:
        # Ignore completions and errors from an earlier cancelled start, even if
        # a new run has already begun by the time Qt delivers the signal.
        if epoch is not self._motion_cancelled:
            return
        if error:
            self.operation_failed.emit(name, error)
        else:
            self.operation_finished.emit(name)

    def connect_device(self) -> None:
        self._connect.emit()

    def disconnect_device(self) -> None:
        self.cancel_preparation()
        self._close.emit()

    def write(self, name: str, values: dict[str, bool | int | float], verify: bool = False) -> None:
        guarded = any(
            value is True
            and (
                key == "MAIN.GuiConveyorEnabled"
                or key == "MAIN.GuiReorientationStart"
                or (
                    key == "MAIN.GuiReorientationControlActive"
                    and not values.get("MAIN.GuiReorientationReset")
                )
                or key.startswith("MAIN.GuiArrayEnabled")
            )
            for key, value in values.items()
        )
        self._execute.emit(
            _Command(
                name,
                values,
                verify,
                self._motion_cancelled if guarded else None,
                self._motion_cancelled,
            )
        )

    def prepare_conveyor(self, name: str) -> None:
        self.cancel_preparation()
        self._motion_cancelled = threading.Event()
        self._prepare.emit((name, self._motion_cancelled))

    def cancel_preparation(self) -> None:
        # Synchronous cancellation also invalidates motion writes already queued to ADS.
        self._motion_cancelled.set()

    def stage(self, plan: ProfileWritePlan) -> None:
        self.write("safe_stop", plan.safe_stop, True)

    def shutdown(self) -> None:
        self.cancel_preparation()
        self._shutdown.emit()
        if not self.thread.wait(2000):
            LOGGER.error("ADS shutdown is still waiting for the current operation")
