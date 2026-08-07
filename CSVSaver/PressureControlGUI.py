import csv
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyads
from PyQt6.QtCore import QObject, Qt, QSignalBlocker, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ur_angle_control import (
    UR_ANGLE_DEFAULT_DEG,
    UR_ANGLE_MAX_DEG,
    UR_ANGLE_MIN_DEG,
    UR_ANGLE_STEP_DEG,
    UR_HOST,
    UrAngleClient,
)


AMS_NET_ID = "10.145.4.14.1.1"
PLC_IP = "192.168.10.23"
PLC_PORT = pyads.PORT_TC3PLC1

PROFILE_DIR = Path("pressure_profiles")
PROFILE_VERSION = 8
CSV_FILE = Path("pressure_log.csv")
LIGHT_BARRIER_EVENT_LOG_FILE = Path(__file__).resolve().parent / "light_barrier_events.csv"
FORCE_DELAY_LOG_FILE = Path(__file__).resolve().parent / "force_peak_delay_log.csv"
CSV_HEADER = [
    "timestamp",
    "AvgPressureN1",
    "AvgPressureN2",
    "EstimatedVelocityArray1",
    "EstimatedVelocityArray2",
    "EstimatedVelocityArray3",
    "EstimatedVelocityArray4",
]
LOG_POLL_INTERVAL_MS = 150
ADS_TIMEOUT_MS = 500
ADS_RECONNECT_INTERVAL_MS = 2000
ADS_WRITE_DEBOUNCE_MS = 100

ARRAY_COUNT = 4
NOZZLES_PER_ARRAY = 6
PRESSURE_MIN_MBAR = 0
PRESSURE_MAX_MBAR = 6000
DELAY_MIN_MS = 0
DELAY_MAX_MS = 1000
PULSE_MIN_MS = 1
PULSE_MAX_MS = 500
SENSOR_SPACING_MIN_MM = 1.0
SENSOR_SPACING_MAX_MM = 5000.0
SENSOR_SPACING_12_DEFAULT_MM = 23.54
SENSOR_SPACING_34_DEFAULT_MM = 39.9
SENSOR_SPACING_56_DEFAULT_MM = 64.69
OFFSET_MIN_MM = 0.0
OFFSET_MAX_MM = 5000.0
LIGHT_BARRIER_DEBOUNCE_MIN_MS = 1
LIGHT_BARRIER_DEBOUNCE_MAX_MS = 200
LIGHT_BARRIER_DEBOUNCE_DEFAULT_MS = 20
LIGHT_BARRIER_INVERT_DEFAULTS = (False, False, True, True, False, False)
LIGHT_BARRIER_DEBOUNCE_ENABLED_DEFAULTS = (
    True,
    True,
    False,
    False,
    True,
    True,
)
CONVEYOR_SPEED_MIN_MM_PER_SEC = 0.0
CONVEYOR_SPEED_MAX_MM_PER_SEC = 5000.0
CONVEYOR_MAX_SPEED_MIN_MM_PER_SEC = 1.0
CONVEYOR_MAX_SPEED_MAX_MM_PER_SEC = 5000.0
ESTIMATE_POLL_INTERVAL_MS = 750
ESTIMATE_DISPLAY_EPSILON = 0.05
CALIBRATION_POLL_INTERVAL_MS = 100
SETUP_POLL_INTERVAL_MS = 50
FORCE_DELAY_POLL_INTERVAL_MS = 100
FORCE_DELAY_WINDOW_DEFAULT_MS = 2000
FORCE_DELAY_WINDOW_MIN_MS = 100
FORCE_DELAY_WINDOW_MAX_MS = 30000
FORCE_DELAY_MIN_RISE_DEFAULT = 0.05
FORCE_RESPONSE_DELAY_DEFAULTS_MS = (15.0,) * ARRAY_COUNT
FORCE_SINGLE_NOZZLE_RESPONSE_DELAY_DEFAULTS_MS = (15.0,) * ARRAY_COUNT
CALIBRATION_MARKER_DISTANCE_DEFAULT_MM = 315.0
CONVEYOR_MM_PER_FULL_STEP_DEFAULT = 0.32960026
CALIBRATION_JOG_STEPS_DEFAULT = 100
CALIBRATION_JOG_SPEED_DEFAULT = 10.0
CONVEYOR_JOG_DISTANCE_DEFAULT_MM = 1.0
CONVEYOR_JOG_DISTANCE_MAX_MM = 5000.0

pyads.set_timeout(ADS_TIMEOUT_MS)


def calculate_conveyor_jog(
    distance_mm: float, speed_mm_per_sec: float, mm_per_full_step: float
) -> tuple[int, float, float]:
    if mm_per_full_step <= 0.0:
        raise ValueError("Conveyor calibration is invalid")
    full_steps = max(1, min(100000, int((distance_mm / mm_per_full_step) + 0.5)))
    actual_distance_mm = full_steps * mm_per_full_step
    full_steps_per_sec = max(1.0, min(500.0, speed_mm_per_sec / mm_per_full_step))
    return full_steps, actual_distance_mm, full_steps_per_sec


def calculate_force_delay_statistics(delays_ms: list[float]) -> dict[str, float]:
    if not delays_ms:
        return {
            "mean": 0.0,
            "standard_deviation": 0.0,
            "minimum": 0.0,
            "maximum": 0.0,
            "coefficient_of_variation": 0.0,
        }
    mean = statistics.fmean(delays_ms)
    standard_deviation = statistics.pstdev(delays_ms)
    return {
        "mean": mean,
        "standard_deviation": standard_deviation,
        "minimum": min(delays_ms),
        "maximum": max(delays_ms),
        "coefficient_of_variation": (
            standard_deviation / mean * 100.0 if mean != 0.0 else 0.0
        ),
    }


def calculate_force_response_delay(
    single_nozzle_ms: float, four_nozzle_ms: float, active_nozzles: int
) -> float:
    if active_nozzles <= 1:
        return single_nozzle_ms
    if active_nozzles >= 4:
        return four_nozzle_ms
    return single_nozzle_ms + (four_nozzle_ms - single_nozzle_ms) * (
        active_nozzles - 1
    ) / 3.0


@dataclass(frozen=True)
class ArraySymbols:
    array_enabled: str
    nozzle_enabled: tuple[str, ...]
    pressure: str
    delay: str
    pulse_duration: str
    offset: str
    estimated_velocity: str
    estimated_delay: str


SYMBOLS = {
    index: ArraySymbols(
        array_enabled=f"MAIN.GuiArrayEnabled{index}",
        nozzle_enabled=tuple(
            f"MAIN.GuiNozzleEnabled{((index - 1) * NOZZLES_PER_ARRAY) + nozzle_index}"
            for nozzle_index in range(1, NOZZLES_PER_ARRAY + 1)
        ),
        pressure=f"MAIN.GuiPressureMbar{index}",
        delay=f"MAIN.GuiDelayMs{index}",
        pulse_duration=f"MAIN.GuiPulseDurationMs{index}",
        offset=f"MAIN.GuiOffsetMm{index}",
        estimated_velocity=f"MAIN.EstimatedVelocityMmPerSec{index}",
        estimated_delay=f"MAIN.EstimatedOffsetDelayMs{index}",
    )
    for index in range(1, ARRAY_COUNT + 1)
}


class AdsClient:
    def __init__(self) -> None:
        self.plc: pyads.Connection | None = None
        self.last_error = ""

    def connect(self) -> bool:
        self.close()
        try:
            pyads.set_timeout(ADS_TIMEOUT_MS)
            self.plc = pyads.Connection(AMS_NET_ID, PLC_PORT, PLC_IP)
            self.plc.open()
            self.last_error = ""
            return True
        except Exception as exc:
            self.plc = None
            self.last_error = str(exc)
            return False

    def close(self) -> None:
        if self.plc is not None:
            try:
                self.plc.close()
            except Exception:
                pass
        self.plc = None

    @property
    def is_connected(self) -> bool:
        return self.plc is not None

    def read_array(self, index: int) -> dict:
        if self.plc is None:
            raise RuntimeError("ADS is offline")

        symbols = SYMBOLS[index]
        return {
            "enabled": self.plc.read_by_name(symbols.array_enabled, pyads.PLCTYPE_BOOL),
            "nozzles_enabled": [
                self.plc.read_by_name(symbol, pyads.PLCTYPE_BOOL)
                for symbol in symbols.nozzle_enabled
            ],
            "pressure_mbar": self.plc.read_by_name(symbols.pressure, pyads.PLCTYPE_INT),
            "delay_ms": self.plc.read_by_name(symbols.delay, pyads.PLCTYPE_UINT),
            "pulse_duration_ms": self.plc.read_by_name(symbols.pulse_duration, pyads.PLCTYPE_UINT),
            "offset_mm": self.plc.read_by_name(symbols.offset, pyads.PLCTYPE_REAL),
        }

    def write_array_value(self, index: int, field: str, value: bool | int | float) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")

        symbols = SYMBOLS[index]
        if field == "enabled":
            self.plc.write_by_name(symbols.array_enabled, bool(value), pyads.PLCTYPE_BOOL)
        elif field.startswith("nozzle_") and field.endswith("_enabled"):
            nozzle_index = int(field.removeprefix("nozzle_").removesuffix("_enabled"))
            self.plc.write_by_name(symbols.nozzle_enabled[nozzle_index - 1], bool(value), pyads.PLCTYPE_BOOL)
        elif field == "pressure_mbar":
            self.plc.write_by_name(symbols.pressure, int(value), pyads.PLCTYPE_INT)
        elif field == "delay_ms":
            self.plc.write_by_name(symbols.delay, int(value), pyads.PLCTYPE_UINT)
        elif field == "pulse_duration_ms":
            self.plc.write_by_name(symbols.pulse_duration, int(value), pyads.PLCTYPE_UINT)
        elif field == "offset_mm":
            self.plc.write_by_name(symbols.offset, float(value), pyads.PLCTYPE_REAL)
        else:
            raise ValueError(f"Unknown field: {field}")

    def read_sensor_spacings(self) -> tuple[float, float, float]:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return (
            self.plc.read_by_name("MAIN.GuiSensorSpacing12Mm", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.GuiSensorSpacing34Mm", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.GuiSensorSpacing56Mm", pyads.PLCTYPE_REAL),
        )

    def write_sensor_spacing(self, symbol_name: str, value: float) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name(symbol_name, float(value), pyads.PLCTYPE_REAL)

    def write_travel_time_bound(self, symbol_name: str, value: int) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name(symbol_name, int(value), pyads.PLCTYPE_UINT)

    def read_conveyor_settings(self) -> dict:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return {
            "enabled": self.plc.read_by_name("MAIN.GuiConveyorEnabled", pyads.PLCTYPE_BOOL),
            "reverse": self.plc.read_by_name("MAIN.GuiConveyorReverse", pyads.PLCTYPE_BOOL),
            "speed_mm_per_sec": self.plc.read_by_name("MAIN.GuiConveyorSpeedMmPerSec", pyads.PLCTYPE_REAL),
            "max_speed_mm_per_sec": self.plc.read_by_name("MAIN.GuiConveyorMaxSpeedMmPerSec", pyads.PLCTYPE_REAL),
        }

    def write_conveyor_setting(self, field: str, value: bool | float) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        if field == "enabled":
            self.plc.write_by_name("MAIN.GuiConveyorEnabled", bool(value), pyads.PLCTYPE_BOOL)
        elif field == "reverse":
            self.plc.write_by_name("MAIN.GuiConveyorReverse", bool(value), pyads.PLCTYPE_BOOL)
        elif field == "reset":
            self.plc.write_by_name("MAIN.GuiConveyorReset", bool(value), pyads.PLCTYPE_BOOL)
        elif field == "speed_mm_per_sec":
            self.plc.write_by_name("MAIN.GuiConveyorSpeedMmPerSec", float(value), pyads.PLCTYPE_REAL)
        elif field == "max_speed_mm_per_sec":
            self.plc.write_by_name("MAIN.GuiConveyorMaxSpeedMmPerSec", float(value), pyads.PLCTYPE_REAL)
        else:
            raise ValueError(f"Unknown conveyor field: {field}")

    def read_conveyor_calibration(self) -> dict:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return {
            "marker_distance_mm": self.plc.read_by_name(
                "MAIN.GuiCalibrationMarkerDistanceMm", pyads.PLCTYPE_REAL
            ),
            "jog_steps": self.plc.read_by_name(
                "MAIN.GuiCalibrationJogSteps", pyads.PLCTYPE_UDINT
            ),
            "jog_speed_full_steps_per_sec": self.plc.read_by_name(
                "MAIN.GuiCalibrationJogSpeedFullStepsPerSec", pyads.PLCTYPE_REAL
            ),
            "mm_per_full_step": self.plc.read_by_name(
                "MAIN.GuiConveyorMmPerFullStep", pyads.PLCTYPE_REAL
            ),
            "valid": self.plc.read_by_name(
                "MAIN.GuiConveyorCalibrationValid", pyads.PLCTYPE_BOOL
            ),
        }

    def write_conveyor_calibration(
        self, marker_distance_mm: float, mm_per_full_step: float, valid: bool
    ) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name(
            "MAIN.GuiCalibrationMarkerDistanceMm",
            float(marker_distance_mm),
            pyads.PLCTYPE_REAL,
        )
        self.plc.write_by_name(
            "MAIN.GuiConveyorMmPerFullStep",
            float(mm_per_full_step),
            pyads.PLCTYPE_REAL,
        )
        self.plc.write_by_name(
            "MAIN.GuiConveyorCalibrationValid", bool(valid), pyads.PLCTYPE_BOOL
        )

    def set_calibration_mode(self, enabled: bool) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        if enabled:
            self.plc.write_by_name("MAIN.GuiConveyorEnabled", False, pyads.PLCTYPE_BOOL)
        self.plc.write_by_name(
            "MAIN.GuiConveyorCalibrationMode", bool(enabled), pyads.PLCTYPE_BOOL
        )

    def command_calibration_move(self, direction: str, steps: int, speed: float) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        if direction not in {"left", "right"}:
            raise ValueError(f"Unknown calibration direction: {direction}")
        self.plc.write_by_name(
            "MAIN.GuiCalibrationJogSteps", int(steps), pyads.PLCTYPE_UDINT
        )
        self.plc.write_by_name(
            "MAIN.GuiCalibrationJogSpeedFullStepsPerSec",
            float(speed),
            pyads.PLCTYPE_REAL,
        )
        symbol = (
            "MAIN.GuiCalibrationMoveLeft"
            if direction == "left"
            else "MAIN.GuiCalibrationMoveRight"
        )
        self.plc.write_by_name(symbol, True, pyads.PLCTYPE_BOOL)

    def capture_calibration_mark(self, mark: str) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        if mark not in {"left", "right"}:
            raise ValueError(f"Unknown calibration mark: {mark}")
        symbol = (
            "MAIN.GuiCalibrationCaptureLeftMark"
            if mark == "left"
            else "MAIN.GuiCalibrationCaptureRightMark"
        )
        self.plc.write_by_name(symbol, True, pyads.PLCTYPE_BOOL)

    def stop_calibration_move(self) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name("MAIN.GuiCalibrationStop", True, pyads.PLCTYPE_BOOL)

    def write_calibration_marker_distance(self, distance_mm: float) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name(
            "MAIN.GuiCalibrationMarkerDistanceMm", float(distance_mm), pyads.PLCTYPE_REAL
        )

    def read_calibration_status(self) -> dict:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return {
            "busy": self.plc.read_by_name("MAIN.CalibrationBusy", pyads.PLCTYPE_BOOL),
            "error": self.plc.read_by_name("MAIN.CalibrationError", pyads.PLCTYPE_BOOL),
            "status_code": self.plc.read_by_name(
                "MAIN.CalibrationStatusCode", pyads.PLCTYPE_UINT
            ),
            "ready_to_execute": self.plc.read_by_name(
                "MAIN.StepperPosReadyToExecute", pyads.PLCTYPE_BOOL
            ),
            "left_valid": self.plc.read_by_name(
                "MAIN.CalibrationLeftMarkValid", pyads.PLCTYPE_BOOL
            ),
            "right_valid": self.plc.read_by_name(
                "MAIN.CalibrationRightMarkValid", pyads.PLCTYPE_BOOL
            ),
            "left_position": self.plc.read_by_name(
                "MAIN.CalibrationLeftPosition", pyads.PLCTYPE_UDINT
            ),
            "right_position": self.plc.read_by_name(
                "MAIN.CalibrationRightPosition", pyads.PLCTYPE_UDINT
            ),
            "increment_difference": self.plc.read_by_name(
                "MAIN.CalibrationPositionDifferenceIncrements", pyads.PLCTYPE_UDINT
            ),
            "full_step_difference": self.plc.read_by_name(
                "MAIN.CalibrationFullStepDifference", pyads.PLCTYPE_REAL
            ),
            "mm_per_full_step": self.plc.read_by_name(
                "MAIN.GuiConveyorMmPerFullStep", pyads.PLCTYPE_REAL
            ),
            "full_steps_per_mm": self.plc.read_by_name(
                "MAIN.CalibrationStepsPerMm", pyads.PLCTYPE_REAL
            ),
            "valid": self.plc.read_by_name(
                "MAIN.GuiConveyorCalibrationValid", pyads.PLCTYPE_BOOL
            ),
        }

    def reset_velocity_estimates(self) -> None:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        self.plc.write_by_name("MAIN.GuiResetVelocityEstimates", True, pyads.PLCTYPE_BOOL)

    def read_shot_counter(self) -> int:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return self.plc.read_by_name("MAIN.ShotCounter", pyads.PLCTYPE_UDINT)

    def read_pressure_log_values(self) -> tuple[float, float, float, float, float, float]:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return (
            self.plc.read_by_name("MAIN.AvgPressureN1", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.AvgPressureN2", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.EstimatedVelocityMmPerSec1", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.EstimatedVelocityMmPerSec2", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.EstimatedVelocityMmPerSec3", pyads.PLCTYPE_REAL),
            self.plc.read_by_name("MAIN.EstimatedVelocityMmPerSec4", pyads.PLCTYPE_REAL),
        )


class AdsWorker(QObject):
    connection_changed = pyqtSignal(bool, str)
    initial_snapshot_ready = pyqtSignal(object)
    live_snapshot_ready = pyqtSignal(object)
    calibration_status_ready = pyqtSignal(object)
    setup_status_ready = pyqtSignal(object)
    force_delay_status_ready = pyqtSignal(object)
    write_finished = pyqtSignal(str, object)
    operation_failed = pyqtSignal(str, str)
    shutdown_finished = pyqtSignal()

    SAFE_STOP_VALUES = {
        "MAIN.GuiCalibrationStop": True,
        "MAIN.GuiConveyorCalibrationMode": False,
        "MAIN.GuiVelocityCheckMode": False,
        "MAIN.GuiConveyorEnabled": False,
        "MAIN.GuiForceDelayMeasurementEnabled": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self.client = AdsClient()
        self.poll_timer: QTimer | None = None
        self.reconnect_timer: QTimer | None = None
        self.calibration_polling = False
        self.setup_polling = False
        self.force_delay_polling = False
        self.shutting_down = False

    @pyqtSlot()
    def start(self) -> None:
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(LOG_POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.poll)
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(ADS_RECONNECT_INTERVAL_MS)
        self.reconnect_timer.timeout.connect(self.connect_ads)
        self.connect_ads()

    def plc(self) -> pyads.Connection:
        if self.client.plc is None:
            raise RuntimeError("ADS is offline")
        return self.client.plc

    def read_values(self, names: list[str]) -> dict:
        return self.plc().read_list_by_name(names, cache_symbol_info=True)

    def write_values_impl(self, values: dict) -> None:
        errors = self.plc().write_list_by_name(values, cache_symbol_info=True)
        failed = {
            name: error
            for name, error in errors.items()
            if error and error.lower() != "no error"
        }
        if failed:
            raise RuntimeError(f"ADS sum write failed: {failed}")

    @pyqtSlot()
    def connect_ads(self) -> None:
        if self.shutting_down or self.client.is_connected:
            return
        if self.reconnect_timer is not None:
            self.reconnect_timer.stop()
        pyads.set_timeout(ADS_TIMEOUT_MS)
        if not self.client.connect():
            self.connection_changed.emit(False, self.client.last_error)
            if self.reconnect_timer is not None:
                self.reconnect_timer.start()
            return
        try:
            safe_values = dict(self.SAFE_STOP_VALUES)
            safe_values["MAIN.GuiResetVelocityEstimates"] = True
            self.write_values_impl(safe_values)
            snapshot = self.read_initial_snapshot()
        except Exception as exc:
            self.handle_failure("connect", exc)
            return
        self.connection_changed.emit(True, "")
        self.initial_snapshot_ready.emit(snapshot)
        if self.poll_timer is not None:
            self._update_poll_interval()
            self.poll_timer.start()

    def _update_poll_interval(self) -> None:
        if self.poll_timer is None:
            return
        if self.calibration_polling:
            self.poll_timer.setInterval(CALIBRATION_POLL_INTERVAL_MS)
        elif self.setup_polling:
            self.poll_timer.setInterval(SETUP_POLL_INTERVAL_MS)
        elif self.force_delay_polling:
            self.poll_timer.setInterval(FORCE_DELAY_POLL_INTERVAL_MS)
        else:
            self.poll_timer.setInterval(LOG_POLL_INTERVAL_MS)

    def disconnect_ads(self, message: str = "") -> None:
        if self.poll_timer is not None:
            self.poll_timer.stop()
        self.client.close()
        self.connection_changed.emit(False, message)
        if not self.shutting_down and self.reconnect_timer is not None:
            self.reconnect_timer.start()

    def handle_failure(self, context: str, exc: Exception) -> None:
        message = format_ads_error(exc)
        self.operation_failed.emit(context, message)
        self.disconnect_ads(message)

    def read_initial_snapshot(self) -> dict:
        names = [
            "MAIN.GuiSensorSpacing12Mm",
            "MAIN.GuiSensorSpacing34Mm",
            "MAIN.GuiSensorSpacing56Mm",
            "MAIN.GuiBarrierCalibrationDebounceMs",
            *[f"MAIN.GuiLightBarrierInvert{index}" for index in range(1, 7)],
            *[
                f"MAIN.GuiLightBarrierDebounceEnabled{index}"
                for index in range(1, 7)
            ],
            "MAIN.GuiConveyorEnabled",
            "MAIN.GuiConveyorReverse",
            "MAIN.GuiConveyorSpeedMmPerSec",
            "MAIN.GuiConveyorMaxSpeedMmPerSec",
            "MAIN.GuiCalibrationMarkerDistanceMm",
            "MAIN.GuiCalibrationJogSteps",
            "MAIN.GuiCalibrationJogSpeedFullStepsPerSec",
            "MAIN.GuiConveyorMmPerFullStep",
            "MAIN.GuiConveyorCalibrationValid",
            *[
                f"MAIN.GuiForceResponseDelayMs{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
            *[
                f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
        ]
        for index in range(1, ARRAY_COUNT + 1):
            symbols = SYMBOLS[index]
            names.extend(
                [
                    symbols.array_enabled,
                    *symbols.nozzle_enabled,
                    symbols.pressure,
                    symbols.delay,
                    symbols.pulse_duration,
                    symbols.offset,
                ]
            )
        values = self.read_values(names)
        arrays = []
        for index in range(1, ARRAY_COUNT + 1):
            symbols = SYMBOLS[index]
            arrays.append(
                {
                    "index": index,
                    "enabled": bool(values[symbols.array_enabled]),
                    "nozzles_enabled": [bool(values[name]) for name in symbols.nozzle_enabled],
                    "pressure_mbar": int(values[symbols.pressure]),
                    "delay_ms": int(values[symbols.delay]),
                    "pulse_duration_ms": int(values[symbols.pulse_duration]),
                    "offset_mm": float(values[symbols.offset]),
                }
            )
        return {
            "sensor_spacings": (
                float(values["MAIN.GuiSensorSpacing12Mm"]),
                float(values["MAIN.GuiSensorSpacing34Mm"]),
                float(values["MAIN.GuiSensorSpacing56Mm"]),
            ),
            "light_barrier_debounce_ms": int(
                values["MAIN.GuiBarrierCalibrationDebounceMs"]
            ),
            "light_barrier_inverted": [
                bool(values[f"MAIN.GuiLightBarrierInvert{index}"])
                for index in range(1, 7)
            ],
            "light_barrier_debounce_enabled": [
                bool(values[f"MAIN.GuiLightBarrierDebounceEnabled{index}"])
                for index in range(1, 7)
            ],
            "conveyor": {
                "enabled": bool(values["MAIN.GuiConveyorEnabled"]),
                "reverse": bool(values["MAIN.GuiConveyorReverse"]),
                "speed_mm_per_sec": float(values["MAIN.GuiConveyorSpeedMmPerSec"]),
                "max_speed_mm_per_sec": float(values["MAIN.GuiConveyorMaxSpeedMmPerSec"]),
            },
            "calibration": {
                "marker_distance_mm": float(values["MAIN.GuiCalibrationMarkerDistanceMm"]),
                "jog_steps": int(values["MAIN.GuiCalibrationJogSteps"]),
                "jog_speed_full_steps_per_sec": float(
                    values["MAIN.GuiCalibrationJogSpeedFullStepsPerSec"]
                ),
                "mm_per_full_step": float(values["MAIN.GuiConveyorMmPerFullStep"]),
                "valid": bool(values["MAIN.GuiConveyorCalibrationValid"]),
            },
            "force_response_delays_ms": [
                float(values[f"MAIN.GuiForceResponseDelayMs{index}"])
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "force_single_nozzle_response_delays_ms": [
                float(
                    values[f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}"]
                )
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "arrays": arrays,
        }

    def read_live_snapshot(self) -> dict:
        names = ["MAIN.ShotCounter", "MAIN.AvgPressureN1", "MAIN.AvgPressureN2"]
        for index in range(1, ARRAY_COUNT + 1):
            names.extend([SYMBOLS[index].estimated_velocity, SYMBOLS[index].estimated_delay])
        for index in range(1, 7):
            names.extend(
                [
                    f"MAIN.LightBarrierEventCount{index}",
                    f"MAIN.LightBarrierLastEventTimeMs{index}",
                    f"MAIN.LightBarrierLastEventPosition{index}",
                    f"MAIN.LightBarrierStable{index}",
                    f"MAIN.LightBarrierOn{index}",
                ]
            )
        names.extend(
            [
                "MAIN.LastVelocityTimeMs",
                "MAIN.LastVelocityTime2Ms",
                "MAIN.LastVelocityTime3Ms",
                "MAIN.VelocityMeasurementValid",
                "MAIN.VelocityMeasurement2Valid",
                "MAIN.VelocityMeasurement3Valid",
            ]
        )
        values = self.read_values(names)
        return {
            "shot_counter": int(values["MAIN.ShotCounter"]),
            "avg_pressure_n1": float(values["MAIN.AvgPressureN1"]),
            "avg_pressure_n2": float(values["MAIN.AvgPressureN2"]),
            "velocities": [
                float(values[SYMBOLS[index].estimated_velocity])
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "delays": [
                float(values[SYMBOLS[index].estimated_delay])
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "light_barrier_events": [
                {
                    "barrier": index,
                    "count": int(values[f"MAIN.LightBarrierEventCount{index}"]),
                    "plc_time_ms": int(
                        values[f"MAIN.LightBarrierLastEventTimeMs{index}"]
                    ),
                    "position_increments": int(
                        values[f"MAIN.LightBarrierLastEventPosition{index}"]
                    ),
                    "state": bool(values[f"MAIN.LightBarrierStable{index}"]),
                    "raw_state": bool(values[f"MAIN.LightBarrierOn{index}"]),
                }
                for index in range(1, 7)
            ],
            "velocity_times_ms": (
                int(values["MAIN.LastVelocityTimeMs"]),
                int(values["MAIN.LastVelocityTime2Ms"]),
                int(values["MAIN.LastVelocityTime3Ms"]),
            ),
            "velocity_valid": (
                bool(values["MAIN.VelocityMeasurementValid"]),
                bool(values["MAIN.VelocityMeasurement2Valid"]),
                bool(values["MAIN.VelocityMeasurement3Valid"]),
            ),
        }

    def read_force_delay_snapshot(self) -> dict:
        names = [
            "MAIN.GuiForceDelayMeasurementEnabled",
            "MAIN.GuiForceDelayLightBarrier",
            "MAIN.GuiForceDelaySensor",
            "MAIN.GuiForceDelayWindowMs",
            "MAIN.GuiForceDelayMinRise",
            "MAIN.ForceDelayBusy",
            "MAIN.ForceDelayStatusCode",
            "MAIN.ForceDelayResultCounter",
            "MAIN.ForceDelayValidCount",
            "MAIN.ForceDelayInvalidCount",
            "MAIN.ForceDelayLastValid",
            "MAIN.ForceDelayLightBarrierTimeMs",
            "MAIN.ForceDelayPeakTimeMs",
            "MAIN.ForceDelayPeakDelayMs",
            "MAIN.ForceDelayBaseline",
            "MAIN.ForceDelayPeak",
            "MAIN.ForceDelayPeakRise",
            "MAIN.ForceDelayCurrentSignal",
            *[
                f"MAIN.GuiForceResponseDelayMs{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
            *[
                f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
            *[
                f"MAIN.EffectiveForceResponseDelayMs{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
            *[
                f"MAIN.ActiveNozzleCount{index}"
                for index in range(1, ARRAY_COUNT + 1)
            ],
        ]
        values = self.read_values(names)
        return {
            "enabled": bool(values["MAIN.GuiForceDelayMeasurementEnabled"]),
            "light_barrier": int(values["MAIN.GuiForceDelayLightBarrier"]),
            "sensor": int(values["MAIN.GuiForceDelaySensor"]),
            "window_ms": int(values["MAIN.GuiForceDelayWindowMs"]),
            "minimum_rise": float(values["MAIN.GuiForceDelayMinRise"]),
            "busy": bool(values["MAIN.ForceDelayBusy"]),
            "status_code": int(values["MAIN.ForceDelayStatusCode"]),
            "result_counter": int(values["MAIN.ForceDelayResultCounter"]),
            "valid_count": int(values["MAIN.ForceDelayValidCount"]),
            "invalid_count": int(values["MAIN.ForceDelayInvalidCount"]),
            "last_valid": bool(values["MAIN.ForceDelayLastValid"]),
            "light_barrier_time_ms": int(
                values["MAIN.ForceDelayLightBarrierTimeMs"]
            ),
            "peak_time_ms": int(values["MAIN.ForceDelayPeakTimeMs"]),
            "peak_delay_ms": int(values["MAIN.ForceDelayPeakDelayMs"]),
            "baseline": float(values["MAIN.ForceDelayBaseline"]),
            "peak": float(values["MAIN.ForceDelayPeak"]),
            "peak_rise": float(values["MAIN.ForceDelayPeakRise"]),
            "current_signal": float(values["MAIN.ForceDelayCurrentSignal"]),
            "response_delays_ms": [
                float(values[f"MAIN.GuiForceResponseDelayMs{index}"])
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "single_nozzle_response_delays_ms": [
                float(
                    values[f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}"]
                )
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "effective_response_delays_ms": [
                float(values[f"MAIN.EffectiveForceResponseDelayMs{index}"])
                for index in range(1, ARRAY_COUNT + 1)
            ],
            "active_nozzle_counts": [
                int(values[f"MAIN.ActiveNozzleCount{index}"])
                for index in range(1, ARRAY_COUNT + 1)
            ],
        }

    def read_calibration_snapshot(self) -> dict:
        names = [
            "MAIN.CalibrationBusy",
            "MAIN.CalibrationError",
            "MAIN.CalibrationStatusCode",
            "MAIN.StepperPosReadyToExecute",
            "MAIN.CalibrationLeftMarkValid",
            "MAIN.CalibrationRightMarkValid",
            "MAIN.CalibrationLeftPosition",
            "MAIN.CalibrationRightPosition",
            "MAIN.CalibrationPositionDifferenceIncrements",
            "MAIN.CalibrationFullStepDifference",
            "MAIN.GuiConveyorMmPerFullStep",
            "MAIN.CalibrationStepsPerMm",
            "MAIN.GuiConveyorCalibrationValid",
            "MAIN.GuiCalibrationMarkerDistanceMm",
        ]
        values = self.read_values(names)
        return {
            "busy": bool(values["MAIN.CalibrationBusy"]),
            "error": bool(values["MAIN.CalibrationError"]),
            "status_code": int(values["MAIN.CalibrationStatusCode"]),
            "ready_to_execute": bool(values["MAIN.StepperPosReadyToExecute"]),
            "left_valid": bool(values["MAIN.CalibrationLeftMarkValid"]),
            "right_valid": bool(values["MAIN.CalibrationRightMarkValid"]),
            "left_position": int(values["MAIN.CalibrationLeftPosition"]),
            "right_position": int(values["MAIN.CalibrationRightPosition"]),
            "increment_difference": int(values["MAIN.CalibrationPositionDifferenceIncrements"]),
            "full_step_difference": float(values["MAIN.CalibrationFullStepDifference"]),
            "mm_per_full_step": float(values["MAIN.GuiConveyorMmPerFullStep"]),
            "full_steps_per_mm": float(values["MAIN.CalibrationStepsPerMm"]),
            "valid": bool(values["MAIN.GuiConveyorCalibrationValid"]),
            "marker_distance_mm": float(values["MAIN.GuiCalibrationMarkerDistanceMm"]),
        }

    def read_setup_snapshot(self) -> dict:
        names = [
            *[f"MAIN.LightBarrierOn{index}" for index in range(1, 7)],
            *[f"MAIN.LightBarrierStable{index}" for index in range(1, 7)],
            *[f"MAIN.GuiLightBarrierInvert{index}" for index in range(1, 7)],
            *[
                f"MAIN.GuiLightBarrierDebounceEnabled{index}"
                for index in range(1, 7)
            ],
            *[f"MAIN.LightBarrierEventCount{index}" for index in range(1, 7)],
            *[
                f"MAIN.LightBarrierLastEventTimeMs{index}"
                for index in range(1, 7)
            ],
            "MAIN.StepperInternalPosition",
            "MAIN.StepperPosReadyToExecute",
            "MAIN.StepperPosBusy",
            "MAIN.StepperPosError",
            "MAIN.BarrierCalibrationActive",
            "MAIN.BarrierCalibrationFirstCaptured",
            "MAIN.BarrierCalibrationSecondCaptured",
            "MAIN.BarrierCalibrationValid",
            "MAIN.BarrierCalibrationFirstPosition",
            "MAIN.BarrierCalibrationSecondPosition",
            "MAIN.BarrierCalibrationDifferenceIncrements",
            "MAIN.BarrierCalibrationDistanceMm",
            "MAIN.BarrierCalibrationStatusCode",
            "MAIN.GuiBarrierCalibrationFirstSensor",
            "MAIN.GuiBarrierCalibrationSecondSensor",
            "MAIN.GuiBarrierCalibrationDebounceMs",
            "MAIN.GuiConveyorMmPerFullStep",
            "MAIN.GuiConveyorCalibrationValid",
            "MAIN.ConveyorFullStepsPerSec",
            "MAIN.ConveyorVelocityRaw",
            "MAIN.GuiSensorSpacing12Mm",
            "MAIN.GuiSensorSpacing34Mm",
            "MAIN.GuiSensorSpacing56Mm",
            "MAIN.LastVelocityTimeMs",
            "MAIN.LastVelocityTime2Ms",
            "MAIN.LastVelocityTime3Ms",
            "MAIN.VelocityMeasurementValid",
            "MAIN.VelocityMeasurement2Valid",
            "MAIN.VelocityMeasurement3Valid",
            "MAIN.EstimatedVelocityMmPerSec1",
            "MAIN.EstimatedVelocityMmPerSec2",
            "MAIN.EstimatedVelocityMmPerSec3",
        ]
        values = self.read_values(names)
        return {
            "light_barriers": [
                bool(values[f"MAIN.LightBarrierStable{index}"])
                for index in range(1, 7)
            ],
            "raw_light_barriers": [
                bool(values[f"MAIN.LightBarrierOn{index}"])
                for index in range(1, 7)
            ],
            "light_barrier_inverted": [
                bool(values[f"MAIN.GuiLightBarrierInvert{index}"])
                for index in range(1, 7)
            ],
            "light_barrier_debounce_enabled": [
                bool(values[f"MAIN.GuiLightBarrierDebounceEnabled{index}"])
                for index in range(1, 7)
            ],
            "light_barrier_event_counts": [
                int(values[f"MAIN.LightBarrierEventCount{index}"])
                for index in range(1, 7)
            ],
            "light_barrier_event_times_ms": [
                int(values[f"MAIN.LightBarrierLastEventTimeMs{index}"])
                for index in range(1, 7)
            ],
            "internal_position": int(values["MAIN.StepperInternalPosition"]),
            "ready_to_execute": bool(values["MAIN.StepperPosReadyToExecute"]),
            "drive_busy": bool(values["MAIN.StepperPosBusy"]),
            "drive_error": bool(values["MAIN.StepperPosError"]),
            "active": bool(values["MAIN.BarrierCalibrationActive"]),
            "first_captured": bool(values["MAIN.BarrierCalibrationFirstCaptured"]),
            "second_captured": bool(values["MAIN.BarrierCalibrationSecondCaptured"]),
            "valid": bool(values["MAIN.BarrierCalibrationValid"]),
            "first_position": int(values["MAIN.BarrierCalibrationFirstPosition"]),
            "second_position": int(values["MAIN.BarrierCalibrationSecondPosition"]),
            "difference_increments": int(
                values["MAIN.BarrierCalibrationDifferenceIncrements"]
            ),
            "distance_mm": float(values["MAIN.BarrierCalibrationDistanceMm"]),
            "status_code": int(values["MAIN.BarrierCalibrationStatusCode"]),
            "first_sensor": int(values["MAIN.GuiBarrierCalibrationFirstSensor"]),
            "second_sensor": int(values["MAIN.GuiBarrierCalibrationSecondSensor"]),
            "debounce_ms": int(values["MAIN.GuiBarrierCalibrationDebounceMs"]),
            "mm_per_full_step": float(values["MAIN.GuiConveyorMmPerFullStep"]),
            "conveyor_calibration_valid": bool(
                values["MAIN.GuiConveyorCalibrationValid"]
            ),
            "full_steps_per_sec": float(values["MAIN.ConveyorFullStepsPerSec"]),
            "velocity_raw": int(values["MAIN.ConveyorVelocityRaw"]),
            "sensor_spacings": (
                float(values["MAIN.GuiSensorSpacing12Mm"]),
                float(values["MAIN.GuiSensorSpacing34Mm"]),
                float(values["MAIN.GuiSensorSpacing56Mm"]),
            ),
            "velocity_times_ms": (
                int(values["MAIN.LastVelocityTimeMs"]),
                int(values["MAIN.LastVelocityTime2Ms"]),
                int(values["MAIN.LastVelocityTime3Ms"]),
            ),
            "velocity_valid": (
                bool(values["MAIN.VelocityMeasurementValid"]),
                bool(values["MAIN.VelocityMeasurement2Valid"]),
                bool(values["MAIN.VelocityMeasurement3Valid"]),
            ),
            "estimated_velocities": (
                float(values["MAIN.EstimatedVelocityMmPerSec1"]),
                float(values["MAIN.EstimatedVelocityMmPerSec2"]),
                float(values["MAIN.EstimatedVelocityMmPerSec3"]),
            ),
        }

    @pyqtSlot()
    def poll(self) -> None:
        if not self.client.is_connected or self.shutting_down:
            return
        try:
            if self.calibration_polling:
                self.calibration_status_ready.emit(self.read_calibration_snapshot())
            elif self.setup_polling:
                self.setup_status_ready.emit(self.read_setup_snapshot())
            elif self.force_delay_polling:
                self.force_delay_status_ready.emit(self.read_force_delay_snapshot())
            else:
                self.live_snapshot_ready.emit(self.read_live_snapshot())
        except Exception as exc:
            self.handle_failure("poll", exc)

    @pyqtSlot(object, str)
    def write_values(self, values: dict, context: str) -> None:
        if not self.client.is_connected:
            self.operation_failed.emit(context, "ADS offline")
            return
        try:
            self.write_values_impl(values)
            self.write_finished.emit(context, dict(values))
        except Exception as exc:
            self.handle_failure(context, exc)

    @pyqtSlot(bool)
    def set_calibration_mode(self, enabled: bool) -> None:
        if not self.client.is_connected:
            self.operation_failed.emit("calibration", "ADS offline")
            return
        try:
            if enabled:
                self.write_values_impl(
                    {
                        "MAIN.GuiConveyorEnabled": False,
                        "MAIN.GuiConveyorCalibrationMode": True,
                    }
                )
                self.calibration_polling = True
                self._update_poll_interval()
                self.calibration_status_ready.emit(self.read_calibration_snapshot())
            else:
                self.write_values_impl(self.SAFE_STOP_VALUES)
                self.calibration_polling = False
                self._update_poll_interval()
            self.write_finished.emit("calibration_mode", {})
        except Exception as exc:
            self.handle_failure("calibration", exc)

    @pyqtSlot(bool)
    def set_setup_polling(self, enabled: bool) -> None:
        self.setup_polling = enabled
        self._update_poll_interval()
        if enabled and self.client.is_connected and not self.calibration_polling:
            try:
                self.setup_status_ready.emit(self.read_setup_snapshot())
            except Exception as exc:
                self.handle_failure("setup_poll", exc)

    @pyqtSlot(bool)
    def set_force_delay_polling(self, enabled: bool) -> None:
        self.force_delay_polling = enabled
        self._update_poll_interval()
        if (
            enabled
            and self.client.is_connected
            and not self.calibration_polling
            and not self.setup_polling
        ):
            try:
                self.force_delay_status_ready.emit(
                    self.read_force_delay_snapshot()
                )
            except Exception as exc:
                self.handle_failure("force_delay_poll", exc)

    @pyqtSlot()
    def reconnect(self) -> None:
        self.disconnect_ads("")
        self.connect_ads()

    @pyqtSlot()
    def shutdown(self) -> None:
        self.shutting_down = True
        if self.reconnect_timer is not None:
            self.reconnect_timer.stop()
        if self.poll_timer is not None:
            self.poll_timer.stop()
        if self.client.is_connected:
            try:
                self.write_values_impl(self.SAFE_STOP_VALUES)
            except Exception:
                pass
        self.client.close()
        self.shutdown_finished.emit()


class AdsController(QObject):
    connection_changed = pyqtSignal(bool, str)
    initial_snapshot_ready = pyqtSignal(object)
    live_snapshot_ready = pyqtSignal(object)
    calibration_status_ready = pyqtSignal(object)
    setup_status_ready = pyqtSignal(object)
    force_delay_status_ready = pyqtSignal(object)
    write_finished = pyqtSignal(str)
    operation_failed = pyqtSignal(str, str)

    write_requested = pyqtSignal(object, str)
    calibration_mode_requested = pyqtSignal(bool)
    setup_polling_requested = pyqtSignal(bool)
    force_delay_polling_requested = pyqtSignal(bool)
    reconnect_requested = pyqtSignal()
    shutdown_requested = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.connected = False
        self.pending_writes: dict[str, object] = {}
        self.pending_contexts: set[str] = set()
        self.calibration_cache = {
            "marker_distance_mm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
            "jog_steps": CALIBRATION_JOG_STEPS_DEFAULT,
            "jog_speed_full_steps_per_sec": CALIBRATION_JOG_SPEED_DEFAULT,
            "mm_per_full_step": CONVEYOR_MM_PER_FULL_STEP_DEFAULT,
            "valid": True,
        }
        self.force_response_delays_ms = list(FORCE_RESPONSE_DELAY_DEFAULTS_MS)
        self.force_single_nozzle_response_delays_ms = list(
            FORCE_SINGLE_NOZZLE_RESPONSE_DELAY_DEFAULTS_MS
        )

        self.write_timer = QTimer(self)
        self.write_timer.setSingleShot(True)
        self.write_timer.setInterval(ADS_WRITE_DEBOUNCE_MS)
        self.write_timer.timeout.connect(self.flush_writes)

        self.thread = QThread(self)
        self.worker = AdsWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.write_requested.connect(self.worker.write_values)
        self.calibration_mode_requested.connect(self.worker.set_calibration_mode)
        self.setup_polling_requested.connect(self.worker.set_setup_polling)
        self.force_delay_polling_requested.connect(
            self.worker.set_force_delay_polling
        )
        self.reconnect_requested.connect(self.worker.reconnect)
        self.shutdown_requested.connect(self.worker.shutdown)
        self.worker.connection_changed.connect(self.on_connection_changed)
        self.worker.initial_snapshot_ready.connect(self.on_initial_snapshot)
        self.worker.live_snapshot_ready.connect(self.live_snapshot_ready)
        self.worker.calibration_status_ready.connect(self.on_calibration_status)
        self.worker.setup_status_ready.connect(self.setup_status_ready)
        self.worker.force_delay_status_ready.connect(
            self.on_force_delay_status
        )
        self.worker.write_finished.connect(self.on_write_finished)
        self.worker.operation_failed.connect(self.operation_failed)
        self.worker.shutdown_finished.connect(
            self.thread.quit, Qt.ConnectionType.DirectConnection
        )

    @property
    def is_connected(self) -> bool:
        return self.connected

    def start(self) -> None:
        if not self.thread.isRunning():
            self.thread.start()

    @pyqtSlot(bool, str)
    def on_connection_changed(self, connected: bool, message: str) -> None:
        self.connected = connected
        if not connected:
            self.write_timer.stop()
            self.pending_writes.clear()
            self.pending_contexts.clear()
        self.connection_changed.emit(connected, message)

    @pyqtSlot(object)
    def on_initial_snapshot(self, snapshot: dict) -> None:
        calibration = dict(snapshot["calibration"])
        if (
            not bool(calibration["valid"])
            or float(calibration["mm_per_full_step"]) <= 0.0
        ):
            calibration.update(
                {
                    "marker_distance_mm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
                    "mm_per_full_step": CONVEYOR_MM_PER_FULL_STEP_DEFAULT,
                    "valid": True,
                }
            )
            snapshot = dict(snapshot)
            snapshot["calibration"] = calibration
            self.write_now(
                {
                    "MAIN.GuiCalibrationMarkerDistanceMm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
                    "MAIN.GuiConveyorMmPerFullStep": CONVEYOR_MM_PER_FULL_STEP_DEFAULT,
                    "MAIN.GuiConveyorCalibrationValid": True,
                },
                "default_conveyor_calibration",
            )
        self.calibration_cache.update(calibration)
        self.force_response_delays_ms = [
            float(value)
            for value in snapshot.get(
                "force_response_delays_ms", FORCE_RESPONSE_DELAY_DEFAULTS_MS
            )
        ]
        self.force_single_nozzle_response_delays_ms = [
            float(value)
            for value in snapshot.get(
                "force_single_nozzle_response_delays_ms",
                FORCE_SINGLE_NOZZLE_RESPONSE_DELAY_DEFAULTS_MS,
            )
        ]
        self.initial_snapshot_ready.emit(snapshot)

    @pyqtSlot(object)
    def on_calibration_status(self, status: dict) -> None:
        self.calibration_cache.update(
            {
                "marker_distance_mm": status["marker_distance_mm"],
                "mm_per_full_step": status["mm_per_full_step"],
                "valid": status["valid"],
            }
        )
        self.calibration_status_ready.emit(status)

    @pyqtSlot(object)
    def on_force_delay_status(self, status: dict) -> None:
        self.force_response_delays_ms = [
            float(value)
            for value in status.get(
                "response_delays_ms", self.force_response_delays_ms
            )
        ]
        self.force_single_nozzle_response_delays_ms = [
            float(value)
            for value in status.get(
                "single_nozzle_response_delays_ms",
                self.force_single_nozzle_response_delays_ms,
            )
        ]
        self.force_delay_status_ready.emit(status)

    @pyqtSlot(str, object)
    def on_write_finished(self, context: str, values: dict) -> None:
        calibration_symbols = {
            "MAIN.GuiCalibrationMarkerDistanceMm": "marker_distance_mm",
            "MAIN.GuiCalibrationJogSteps": "jog_steps",
            "MAIN.GuiCalibrationJogSpeedFullStepsPerSec": "jog_speed_full_steps_per_sec",
            "MAIN.GuiConveyorMmPerFullStep": "mm_per_full_step",
            "MAIN.GuiConveyorCalibrationValid": "valid",
        }
        for symbol, cache_key in calibration_symbols.items():
            if symbol in values:
                self.calibration_cache[cache_key] = values[symbol]
        for index in range(1, ARRAY_COUNT + 1):
            symbol = f"MAIN.GuiForceResponseDelayMs{index}"
            if symbol in values:
                self.force_response_delays_ms[index - 1] = float(values[symbol])
            single_symbol = f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}"
            if single_symbol in values:
                self.force_single_nozzle_response_delays_ms[index - 1] = float(
                    values[single_symbol]
                )
        self.write_finished.emit(context)

    def queue_write(self, symbol: str, value: object, context: str) -> None:
        if not self.connected:
            self.operation_failed.emit(context, "ADS offline")
            return
        self.pending_writes[symbol] = value
        self.pending_contexts.add(context)
        self.write_timer.start()

    def write_now(self, values: dict, context: str) -> None:
        if not self.connected:
            self.operation_failed.emit(context, "ADS offline")
            return
        for symbol in values:
            self.pending_writes.pop(symbol, None)
        self.write_requested.emit(dict(values), context)

    @pyqtSlot()
    def flush_writes(self) -> None:
        if not self.connected or not self.pending_writes:
            self.pending_writes.clear()
            self.pending_contexts.clear()
            return
        values = dict(self.pending_writes)
        context = ", ".join(sorted(self.pending_contexts)) or "settings"
        self.pending_writes.clear()
        self.pending_contexts.clear()
        self.write_requested.emit(values, context)

    def enter_calibration(self) -> None:
        self.pending_writes.pop("MAIN.GuiConveyorEnabled", None)
        self.pending_writes.pop("MAIN.GuiConveyorCalibrationMode", None)
        self.calibration_mode_requested.emit(True)

    def leave_calibration(self) -> None:
        for symbol in AdsWorker.SAFE_STOP_VALUES:
            self.pending_writes.pop(symbol, None)
        self.calibration_mode_requested.emit(False)

    def command_calibration_move(self, direction: str, steps: int, speed: float) -> None:
        if direction not in {"left", "right"}:
            raise ValueError(f"Unknown calibration direction: {direction}")
        symbol = (
            "MAIN.GuiCalibrationMoveLeft"
            if direction == "left"
            else "MAIN.GuiCalibrationMoveRight"
        )
        self.write_now(
            {
                "MAIN.GuiCalibrationJogSteps": int(steps),
                "MAIN.GuiCalibrationJogSpeedFullStepsPerSec": float(speed),
                symbol: True,
            },
            f"calibration_move_{direction}",
        )

    def capture_calibration_mark(self, mark: str) -> None:
        if mark not in {"left", "right"}:
            raise ValueError(f"Unknown calibration mark: {mark}")
        symbol = (
            "MAIN.GuiCalibrationCaptureLeftMark"
            if mark == "left"
            else "MAIN.GuiCalibrationCaptureRightMark"
        )
        self.write_now({symbol: True}, f"capture_{mark}_mark")

    def stop_calibration_move(self) -> None:
        self.write_now({"MAIN.GuiCalibrationStop": True}, "calibration_stop")

    def set_setup_polling(self, enabled: bool) -> None:
        self.setup_polling_requested.emit(enabled)

    def set_force_delay_polling(self, enabled: bool) -> None:
        self.force_delay_polling_requested.emit(enabled)

    def start_force_delay_measurement(
        self, light_barrier: int, sensor: int, window_ms: int, minimum_rise: float
    ) -> None:
        self.write_now(
            {
                "MAIN.GuiForceDelayMeasurementEnabled": True,
                "MAIN.GuiForceDelayLightBarrier": int(light_barrier),
                "MAIN.GuiForceDelaySensor": int(sensor),
                "MAIN.GuiForceDelayWindowMs": int(window_ms),
                "MAIN.GuiForceDelayMinRise": float(minimum_rise),
            },
            "force_delay_start",
        )

    def stop_force_delay_measurement(self) -> None:
        self.write_now(
            {"MAIN.GuiForceDelayMeasurementEnabled": False},
            "force_delay_stop",
        )

    def reset_force_delay_measurement(self) -> None:
        self.write_now(
            {"MAIN.GuiForceDelayReset": True},
            "force_delay_reset",
        )

    def set_force_response_delays(
        self, array_index: int, single_nozzle_ms: float, four_nozzle_ms: float
    ) -> None:
        if array_index not in range(1, ARRAY_COUNT + 1):
            raise ValueError(f"Unknown array index: {array_index}")
        self.write_now(
            {
                f"MAIN.GuiForceSingleNozzleResponseDelayMs{array_index}": float(
                    single_nozzle_ms
                ),
                f"MAIN.GuiForceResponseDelayMs{array_index}": float(
                    four_nozzle_ms
                ),
            },
            f"force_response_delays_array_{array_index}",
        )

    def start_barrier_calibration(
        self,
        first_sensor: int,
        second_sensor: int,
        max_steps: int,
        speed: float,
        debounce_ms: int,
    ) -> None:
        self.write_now(
            {
                "MAIN.GuiConveyorEnabled": False,
                "MAIN.GuiConveyorCalibrationMode": True,
                "MAIN.GuiBarrierCalibrationFirstSensor": int(first_sensor),
                "MAIN.GuiBarrierCalibrationSecondSensor": int(second_sensor),
                "MAIN.GuiBarrierCalibrationDebounceMs": int(debounce_ms),
                "MAIN.GuiCalibrationJogSteps": int(max_steps),
                "MAIN.GuiCalibrationJogSpeedFullStepsPerSec": float(speed),
                "MAIN.GuiBarrierCalibrationStart": True,
                "MAIN.GuiCalibrationMoveRight": True,
            },
            "barrier_calibration_start",
        )

    def stop_setup_motion(self) -> None:
        self.write_now(dict(AdsWorker.SAFE_STOP_VALUES), "setup_stop")

    def start_velocity_check(self, speed_mm_per_sec: float) -> None:
        self.write_now(
            {
                "MAIN.GuiCalibrationStop": False,
                "MAIN.GuiConveyorCalibrationMode": False,
                "MAIN.GuiVelocityCheckMode": True,
                "MAIN.GuiResetVelocityEstimates": True,
                "MAIN.GuiConveyorSpeedMmPerSec": float(speed_mm_per_sec),
                "MAIN.GuiConveyorEnabled": True,
            },
            "velocity_check_start",
        )

    def reconnect(self) -> None:
        self.write_timer.stop()
        self.pending_writes.clear()
        self.pending_contexts.clear()
        self.reconnect_requested.emit()

    def shutdown(self) -> None:
        self.write_timer.stop()
        self.pending_writes.clear()
        self.pending_contexts.clear()
        if self.thread.isRunning():
            self.shutdown_requested.emit()
            if not self.thread.wait(ADS_TIMEOUT_MS + 1000):
                self.thread.quit()
                self.thread.wait(ADS_TIMEOUT_MS)


class UrAngleWorker(QObject):
    angle_applied = pyqtSignal(float)
    operation_failed = pyqtSignal(str)

    def __init__(self, client: UrAngleClient | None = None) -> None:
        super().__init__()
        self.client = client or UrAngleClient()

    @pyqtSlot(float)
    def apply_angle(self, angle_deg: float) -> None:
        try:
            result = self.client.apply_angle(angle_deg)
            self.angle_applied.emit(float(result["angle_deg"]))
        except Exception as exc:
            self.operation_failed.emit(str(exc))


class UrAngleController(QObject):
    angle_applied = pyqtSignal(float)
    operation_failed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    apply_requested = pyqtSignal(float)

    def __init__(
        self,
        parent: QObject | None = None,
        client: UrAngleClient | None = None,
    ) -> None:
        super().__init__(parent)
        self.busy = False
        self.thread = QThread(self)
        self.worker = UrAngleWorker(client)
        self.worker.moveToThread(self.thread)
        self.apply_requested.connect(self.worker.apply_angle)
        self.worker.angle_applied.connect(self._on_angle_applied)
        self.worker.operation_failed.connect(self._on_operation_failed)
        self.thread.start()

    def apply_angle(self, angle_deg: float) -> None:
        if self.busy:
            return
        self.busy = True
        self.busy_changed.emit(True)
        self.apply_requested.emit(float(angle_deg))

    @pyqtSlot(float)
    def _on_angle_applied(self, angle_deg: float) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.angle_applied.emit(angle_deg)

    @pyqtSlot(str)
    def _on_operation_failed(self, message: str) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.operation_failed.emit(message)

    def shutdown(self) -> None:
        self.thread.quit()
        self.thread.wait(1000)


def format_ads_error(exc: Exception) -> str:
    text = str(exc)
    if "symbol not found" in text.lower() or "1808" in text:
        return "PLC symbols not updated: compile/download the TwinCAT PLC project"
    if "1861" in text or "timeout" in text.lower() or "timed out" in text.lower():
        return "ADS controller not reachable: connection timed out"
    if "ADS is offline" in text:
        return "ADS offline"
    return text


class ArrayRow:
    def __init__(self, index: int) -> None:
        self.index = index

        self.enabled = QCheckBox()
        self.enabled.setChecked(index <= 2)
        self.enabled.setToolTip(f"Enable or disable array {index}")

        self.nozzle_enabled = []
        self.nozzle_controls = QWidget()
        nozzle_layout = QHBoxLayout(self.nozzle_controls)
        nozzle_layout.setContentsMargins(0, 0, 0, 0)
        nozzle_layout.setSpacing(18)
        primary_axis = "Z axis" if index in {1, 3} else "Y axis"
        self.axis_group_labels = [primary_axis, "X axis"]
        for axis_name, nozzle_numbers in (
            (primary_axis, range(1, 4)),
            ("X axis", range(4, 7)),
        ):
            group = QWidget()
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(2)
            axis_label = QLabel(axis_name)
            axis_label.setStyleSheet("font-weight: 600;")
            group_layout.addWidget(axis_label)
            checkbox_layout = QHBoxLayout()
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            checkbox_layout.setSpacing(8)
            for nozzle_number in nozzle_numbers:
                checkbox = QCheckBox(f"N{nozzle_number}")
                checkbox.setChecked(nozzle_number <= 4)
                checkbox.setToolTip(
                    f"Array {index}, nozzle {nozzle_number}: {axis_name} flip"
                )
                self.nozzle_enabled.append(checkbox)
                checkbox_layout.addWidget(checkbox)
            group_layout.addLayout(checkbox_layout)
            nozzle_layout.addWidget(group)

        self.pressure = QSpinBox()
        self.pressure.setRange(PRESSURE_MIN_MBAR, PRESSURE_MAX_MBAR)
        self.pressure.setSuffix(" mbar")
        self.pressure.setSingleStep(10)
        self.pressure.setValue(3000)
        self.pressure.setToolTip("Pressure setpoint for this array")

        self.delay = QSpinBox()
        self.delay.setRange(DELAY_MIN_MS, DELAY_MAX_MS)
        self.delay.setSuffix(" ms")
        self.delay.setSingleStep(1)
        self.delay.setValue(0)
        self.delay.setToolTip("Start delay after the light barrier edge")

        self.pulse_duration = QSpinBox()
        self.pulse_duration.setRange(PULSE_MIN_MS, PULSE_MAX_MS)
        self.pulse_duration.setSuffix(" ms")
        self.pulse_duration.setSingleStep(1)
        self.pulse_duration.setValue(100)
        self.pulse_duration.setToolTip("Opening duration of the nozzle pulse")

        self.offset = QDoubleSpinBox()
        self.offset.setRange(OFFSET_MIN_MM, OFFSET_MAX_MM)
        self.offset.setSuffix(" mm")
        self.offset.setDecimals(1)
        self.offset.setSingleStep(1.0)
        self.offset.setValue(0.0)
        self.offset.setToolTip("Distance from the detected front edge to the target impulse location")

        self.estimated_delay = QLabel("0.0 ms")
        self.estimated_delay.setMinimumWidth(90)
        self.estimated_delay.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.last_displayed_delay: float | None = 0.0

        self.estimated_velocity = QLabel("0.0 mm/s")
        self.estimated_velocity.setMinimumWidth(100)
        self.estimated_velocity.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.last_displayed_velocity: float | None = 0.0

        self.status = QLabel("not written")
        self.status.setMinimumWidth(180)
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def values(self) -> dict:
        nozzles_enabled = [checkbox.isChecked() for checkbox in self.nozzle_enabled]
        return {
            "index": self.index,
            "enabled": self.enabled.isChecked(),
            "nozzles_enabled": nozzles_enabled,
            **{
                f"nozzle_{nozzle_index}_enabled": enabled
                for nozzle_index, enabled in enumerate(nozzles_enabled, start=1)
            },
            "pressure_mbar": self.pressure.value(),
            "delay_ms": self.delay.value(),
            "pulse_duration_ms": self.pulse_duration.value(),
            "offset_mm": self.offset.value(),
        }

    def set_values(self, values: dict) -> None:
        blockers = [
            QSignalBlocker(self.enabled),
            *(QSignalBlocker(checkbox) for checkbox in self.nozzle_enabled),
            QSignalBlocker(self.pressure),
            QSignalBlocker(self.delay),
            QSignalBlocker(self.pulse_duration),
            QSignalBlocker(self.offset),
        ]
        try:
            self.enabled.setChecked(bool(values.get("enabled", self.enabled.isChecked())))
            nozzles_enabled = values.get("nozzles_enabled")
            if not isinstance(nozzles_enabled, list):
                has_specific_nozzle_values = any(
                    f"nozzle_{nozzle_index}_enabled" in values
                    for nozzle_index in range(1, NOZZLES_PER_ARRAY + 1)
                )
                nozzles_enabled = [
                    values.get(
                        f"nozzle_{nozzle_index}_enabled",
                        False if has_specific_nozzle_values else values.get("enabled", checkbox.isChecked()),
                    )
                    for nozzle_index, checkbox in enumerate(self.nozzle_enabled, start=1)
                ]
            else:
                nozzles_enabled = [
                    *nozzles_enabled[:NOZZLES_PER_ARRAY],
                    *[False] * max(0, NOZZLES_PER_ARRAY - len(nozzles_enabled)),
                ]
            for checkbox, enabled in zip(self.nozzle_enabled, nozzles_enabled):
                checkbox.setChecked(bool(enabled))
            self.pressure.setValue(int(values.get("pressure_mbar", self.pressure.value())))
            self.delay.setValue(int(values.get("delay_ms", self.delay.value())))
            self.pulse_duration.setValue(
                int(values.get("pulse_duration_ms", self.pulse_duration.value()))
            )
            self.offset.setValue(float(values.get("offset_mm", self.offset.value())))
        finally:
            del blockers


class LightBarrierSettingsDialog(QDialog):
    setting_changed = pyqtSignal(int, bool, bool)

    def __init__(
        self,
        ads: AdsController,
        inverted: list[bool],
        debounce_enabled: list[bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ads = ads
        self.setWindowTitle("Light Barrier Settings")
        self.setModal(True)

        layout = QVBoxLayout(self)
        settings_box = QGroupBox("Signal Settings")
        grid = QGridLayout(settings_box)
        grid.addWidget(QLabel("Light barrier"), 0, 0)
        grid.addWidget(QLabel("Invert"), 0, 1)
        grid.addWidget(QLabel("Debounce"), 0, 2)

        self.invert_controls: list[QCheckBox] = []
        self.debounce_controls: list[QCheckBox] = []
        for sensor in range(1, 7):
            invert_control = QCheckBox()
            invert_control.setChecked(bool(inverted[sensor - 1]))
            invert_control.setToolTip(
                f"Invert the electrical signal from light barrier {sensor}"
            )
            debounce_control = QCheckBox()
            debounce_control.setChecked(bool(debounce_enabled[sensor - 1]))
            debounce_control.setToolTip(
                "Require the configured stable signal time before accepting a transition"
            )
            invert_control.toggled.connect(
                lambda _checked, index=sensor: self._write_setting(index)
            )
            debounce_control.toggled.connect(
                lambda _checked, index=sensor: self._write_setting(index)
            )
            self.invert_controls.append(invert_control)
            self.debounce_controls.append(debounce_control)
            grid.addWidget(QLabel(f"LB {sensor}"), sensor, 0)
            grid.addWidget(invert_control, sensor, 1, Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(debounce_control, sensor, 2, Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(settings_box)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

        self.ads.connection_changed.connect(self._connection_changed)
        self._connection_changed(self.ads.is_connected, "")

    def _write_setting(self, sensor: int) -> None:
        inverted = self.invert_controls[sensor - 1].isChecked()
        debounce_enabled = self.debounce_controls[sensor - 1].isChecked()
        self.setting_changed.emit(sensor, inverted, debounce_enabled)
        self.ads.write_now(
            {
                f"MAIN.GuiLightBarrierInvert{sensor}": inverted,
                f"MAIN.GuiLightBarrierDebounceEnabled{sensor}": debounce_enabled,
            },
            f"light_barrier_{sensor}_settings",
        )

    @pyqtSlot(bool, str)
    def _connection_changed(self, connected: bool, _message: str) -> None:
        for control in (*self.invert_controls, *self.debounce_controls):
            control.setEnabled(connected)

    def done(self, result: int) -> None:
        try:
            self.ads.connection_changed.disconnect(self._connection_changed)
        except (TypeError, RuntimeError):
            pass
        super().done(result)


class ConveyorCalibrationDialog(QDialog):
    STATUS_TEXT = {
        0: "Ready",
        1: "Starting move",
        2: "Moving",
        3: "Move complete",
        4: "Command rejected",
        5: "EL7047 error",
    }

    def __init__(self, ads: AdsController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ads = ads
        self._calibration_mode_requested = False
        self.setWindowTitle("Conveyor Calibration")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build_ui()
        self._connect_signals()

        settings = self.ads.calibration_cache
        with (
            QSignalBlocker(self.marker_distance),
            QSignalBlocker(self.jog_steps),
            QSignalBlocker(self.jog_speed),
        ):
            self.marker_distance.setValue(float(settings["marker_distance_mm"]))
            self.jog_steps.setValue(int(settings["jog_steps"]))
            self.jog_speed.setValue(float(settings["jog_speed_full_steps_per_sec"]))
        self.ads.calibration_status_ready.connect(self.refresh_status)
        self.ads.operation_failed.connect(self._on_ads_error)
        self._calibration_mode_requested = True
        self.ads.enter_calibration()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        parameters = QGroupBox("Calibration Settings")
        form = QFormLayout(parameters)

        self.marker_distance = QDoubleSpinBox()
        self.marker_distance.setRange(1.0, 5000.0)
        self.marker_distance.setDecimals(1)
        self.marker_distance.setSingleStep(1.0)
        self.marker_distance.setSuffix(" mm")
        self.marker_distance.setValue(CALIBRATION_MARKER_DISTANCE_DEFAULT_MM)
        form.addRow("Marker distance", self.marker_distance)

        self.jog_steps = QSpinBox()
        self.jog_steps.setRange(1, 100000)
        self.jog_steps.setValue(CALIBRATION_JOG_STEPS_DEFAULT)
        self.jog_steps.setSuffix(" full steps")
        form.addRow("Move distance", self.jog_steps)

        self.jog_speed = QDoubleSpinBox()
        self.jog_speed.setRange(1.0, 500.0)
        self.jog_speed.setDecimals(1)
        self.jog_speed.setSingleStep(10.0)
        self.jog_speed.setSuffix(" full steps/s")
        self.jog_speed.setValue(CALIBRATION_JOG_SPEED_DEFAULT)
        form.addRow("Jog speed", self.jog_speed)
        layout.addWidget(parameters)

        movement_layout = QHBoxLayout()
        self.move_left_button = QPushButton("Move Left")
        self.move_left_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowLeft)
        )
        self.move_left_button.setToolTip("Move left by the configured number of full steps")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.stop_button.setToolTip("Stop the active calibration movement")
        self.move_right_button = QPushButton("Move Right")
        self.move_right_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        )
        self.move_right_button.setToolTip("Move right by the configured number of full steps")
        self.move_left_button.setEnabled(False)
        self.move_right_button.setEnabled(False)
        movement_layout.addWidget(self.move_left_button)
        movement_layout.addWidget(self.stop_button)
        movement_layout.addWidget(self.move_right_button)
        layout.addLayout(movement_layout)

        mark_layout = QHBoxLayout()
        self.capture_left_button = QPushButton("Calibrate Left Marking")
        self.capture_right_button = QPushButton("Calibrate Right Marking")
        self.capture_left_button.setEnabled(False)
        self.capture_right_button.setEnabled(False)
        mark_layout.addWidget(self.capture_left_button)
        mark_layout.addWidget(self.capture_right_button)
        layout.addLayout(mark_layout)

        results = QGroupBox("Calibration Result")
        result_form = QFormLayout(results)
        self.left_position_label = QLabel("Not captured")
        self.right_position_label = QLabel("Not captured")
        self.increment_difference_label = QLabel("0 increments")
        self.step_difference_label = QLabel("0.0 full steps")
        self.mm_per_step_label = QLabel("Not calibrated")
        self.steps_per_mm_label = QLabel("Not calibrated")
        self.state_label = QLabel("Connecting")
        result_form.addRow("Left position", self.left_position_label)
        result_form.addRow("Right position", self.right_position_label)
        result_form.addRow("Position difference", self.increment_difference_label)
        result_form.addRow("Step difference", self.step_difference_label)
        result_form.addRow("Travel per full step", self.mm_per_step_label)
        result_form.addRow("Full steps per mm", self.steps_per_mm_label)
        result_form.addRow("State", self.state_label)
        layout.addWidget(results)

        close_layout = QHBoxLayout()
        close_layout.addStretch(1)
        self.close_button = QPushButton("Close")
        close_layout.addWidget(self.close_button)
        layout.addLayout(close_layout)

    def _connect_signals(self) -> None:
        self.marker_distance.valueChanged.connect(self._write_marker_distance)
        self.move_left_button.clicked.connect(lambda: self._move("left"))
        self.move_right_button.clicked.connect(lambda: self._move("right"))
        self.stop_button.clicked.connect(self._stop)
        self.capture_left_button.clicked.connect(lambda: self._capture("left"))
        self.capture_right_button.clicked.connect(lambda: self._capture("right"))
        self.close_button.clicked.connect(self.close)

    def _write_marker_distance(self, value: float) -> None:
        self.ads.queue_write(
            "MAIN.GuiCalibrationMarkerDistanceMm", float(value), "marker_distance"
        )

    def _move(self, direction: str) -> None:
        try:
            self.ads.command_calibration_move(
                direction, self.jog_steps.value(), self.jog_speed.value()
            )
            self.move_left_button.setEnabled(False)
            self.move_right_button.setEnabled(False)
            self.capture_left_button.setEnabled(False)
            self.capture_right_button.setEnabled(False)
            self.jog_steps.setEnabled(False)
            self.jog_speed.setEnabled(False)
            self.marker_distance.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.state_label.setText("Command sent")
        except Exception as exc:
            self._show_error(exc)

    def _capture(self, mark: str) -> None:
        self.ads.capture_calibration_mark(mark)

    def _stop(self) -> None:
        self.ads.stop_calibration_move()
        self.state_label.setText("Stopping")

    @pyqtSlot(object)
    def refresh_status(self, status: dict) -> None:
        busy = bool(status["busy"])
        error = bool(status["error"])
        ready = bool(status["ready_to_execute"])
        self.move_left_button.setEnabled(ready and not busy and not error)
        self.move_right_button.setEnabled(ready and not busy and not error)
        self.capture_left_button.setEnabled(not busy and not error)
        self.capture_right_button.setEnabled(not busy and not error)
        self.jog_steps.setEnabled(not busy)
        self.jog_speed.setEnabled(not busy)
        self.marker_distance.setEnabled(not busy)
        self.stop_button.setEnabled(busy or error)

        self.left_position_label.setText(
            f'{status["left_position"]} increments' if status["left_valid"] else "Not captured"
        )
        self.right_position_label.setText(
            f'{status["right_position"]} increments'
            if status["right_valid"]
            else "Not captured"
        )
        self.increment_difference_label.setText(f'{status["increment_difference"]} increments')
        self.step_difference_label.setText(f'{status["full_step_difference"]:.3f} full steps')
        if status["valid"]:
            self.mm_per_step_label.setText(f'{status["mm_per_full_step"]:.6f} mm/full step')
            self.steps_per_mm_label.setText(
                f'{status["full_steps_per_mm"]:.3f} full steps/mm'
            )
        else:
            self.mm_per_step_label.setText("Not calibrated")
            self.steps_per_mm_label.setText("Not calibrated")

        state_text = self.STATUS_TEXT.get(status["status_code"], "Unknown state")
        if error:
            state_text = "EL7047 error"
        elif not ready:
            state_text = "Drive not ready - verify Positioning Interface PDOs"
        self.state_label.setText(state_text)

    @pyqtSlot(str, str)
    def _on_ads_error(self, _context: str, message: str) -> None:
        self._show_error(message)

    def _show_error(self, error: Exception | str) -> None:
        message = error if isinstance(error, str) else format_ads_error(error)
        self.state_label.setText(message)
        self.move_left_button.setEnabled(False)
        self.move_right_button.setEnabled(False)
        self.capture_left_button.setEnabled(False)
        self.capture_right_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _leave_calibration_mode(self) -> None:
        if not self._calibration_mode_requested:
            return
        self._calibration_mode_requested = False
        try:
            self.ads.calibration_status_ready.disconnect(self.refresh_status)
            self.ads.operation_failed.disconnect(self._on_ads_error)
        except (TypeError, RuntimeError):
            pass
        self.ads.leave_calibration()

    def done(self, result: int) -> None:
        self._leave_calibration_mode()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._leave_calibration_mode()
        super().closeEvent(event)


class ConveyorJogDialog(QDialog):
    def __init__(self, ads: AdsController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ads = ads
        self.mm_per_full_step = float(self.ads.calibration_cache["mm_per_full_step"])
        self._calibration_mode_requested = False
        self.setWindowTitle("Conveyor Jogging")
        self.setModal(True)
        self.setMinimumWidth(470)
        self._build_ui()
        self._connect_signals()
        self._update_command_preview()

        self.ads.calibration_status_ready.connect(self.refresh_status)
        self.ads.operation_failed.connect(self._on_ads_error)
        self._calibration_mode_requested = True
        self.ads.enter_calibration()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        settings = QGroupBox("Jog Settings")
        form = QFormLayout(settings)
        self.distance = QDoubleSpinBox()
        self.distance.setRange(0.1, CONVEYOR_JOG_DISTANCE_MAX_MM)
        self.distance.setDecimals(2)
        self.distance.setSingleStep(1.0)
        self.distance.setSuffix(" mm")
        self.distance.setValue(CONVEYOR_JOG_DISTANCE_DEFAULT_MM)
        form.addRow("Move distance", self.distance)

        self.speed = QDoubleSpinBox()
        self.speed.setRange(self.mm_per_full_step, 500.0 * self.mm_per_full_step)
        self.speed.setDecimals(2)
        self.speed.setSingleStep(max(0.1, self.mm_per_full_step))
        self.speed.setSuffix(" mm/s")
        initial_speed = float(
            self.ads.calibration_cache.get(
                "jog_speed_full_steps_per_sec", CALIBRATION_JOG_SPEED_DEFAULT
            )
        ) * self.mm_per_full_step
        self.speed.setValue(initial_speed)
        form.addRow("Jog speed", self.speed)

        self.command_preview = QLabel()
        form.addRow("Commanded movement", self.command_preview)
        layout.addWidget(settings)

        movement_layout = QHBoxLayout()
        self.move_left_button = QPushButton("Move Left")
        self.move_left_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowLeft)
        )
        self.move_left_button.setEnabled(False)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.move_right_button = QPushButton("Move Right")
        self.move_right_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        )
        self.move_right_button.setEnabled(False)
        movement_layout.addWidget(self.move_left_button)
        movement_layout.addWidget(self.stop_button)
        movement_layout.addWidget(self.move_right_button)
        layout.addLayout(movement_layout)

        status_box = QGroupBox("Jog Status")
        status_form = QFormLayout(status_box)
        self.state_label = QLabel("Connecting")
        status_form.addRow("State", self.state_label)
        layout.addWidget(status_box)

        close_layout = QHBoxLayout()
        close_layout.addStretch(1)
        self.close_button = QPushButton("Close")
        close_layout.addWidget(self.close_button)
        layout.addLayout(close_layout)

    def _connect_signals(self) -> None:
        self.distance.valueChanged.connect(self._update_command_preview)
        self.speed.valueChanged.connect(self._update_command_preview)
        self.move_left_button.clicked.connect(lambda: self._move("left"))
        self.move_right_button.clicked.connect(lambda: self._move("right"))
        self.stop_button.clicked.connect(self._stop)
        self.close_button.clicked.connect(self.close)

    def _jog_values(self) -> tuple[int, float, float]:
        return calculate_conveyor_jog(
            self.distance.value(), self.speed.value(), self.mm_per_full_step
        )

    def _update_command_preview(self) -> None:
        full_steps, actual_distance_mm, _speed = self._jog_values()
        self.command_preview.setText(
            f"{actual_distance_mm:.3f} mm ({full_steps} full steps)"
        )

    def _move(self, direction: str) -> None:
        full_steps, actual_distance_mm, full_steps_per_sec = self._jog_values()
        self.ads.command_calibration_move(direction, full_steps, full_steps_per_sec)
        self.move_left_button.setEnabled(False)
        self.move_right_button.setEnabled(False)
        self.distance.setEnabled(False)
        self.speed.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.state_label.setText(f"Moving {actual_distance_mm:.3f} mm")

    def _stop(self) -> None:
        self.ads.stop_calibration_move()
        self.state_label.setText("Stopping")

    @pyqtSlot(object)
    def refresh_status(self, status: dict) -> None:
        busy = bool(status["busy"])
        error = bool(status["error"])
        ready = bool(status["ready_to_execute"])
        controls_enabled = ready and not busy and not error
        self.move_left_button.setEnabled(controls_enabled)
        self.move_right_button.setEnabled(controls_enabled)
        self.distance.setEnabled(not busy)
        self.speed.setEnabled(not busy)
        self.stop_button.setEnabled(busy or error)

        state_text = ConveyorCalibrationDialog.STATUS_TEXT.get(
            status["status_code"], "Unknown state"
        )
        if error:
            state_text = "EL7047 error"
        elif not ready:
            state_text = "Drive not ready - verify Positioning Interface PDOs"
        self.state_label.setText(state_text)

    @pyqtSlot(str, str)
    def _on_ads_error(self, _context: str, message: str) -> None:
        self.state_label.setText(message)
        self.move_left_button.setEnabled(False)
        self.move_right_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _leave_calibration_mode(self) -> None:
        if not self._calibration_mode_requested:
            return
        self._calibration_mode_requested = False
        try:
            self.ads.calibration_status_ready.disconnect(self.refresh_status)
            self.ads.operation_failed.disconnect(self._on_ads_error)
        except (TypeError, RuntimeError):
            pass
        self.ads.leave_calibration()

    def done(self, result: int) -> None:
        self._leave_calibration_mode()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._leave_calibration_mode()
        super().closeEvent(event)


class ForceDelayDialog(QDialog):
    STATUS_TEXT = {
        0: "Disabled",
        1: "Armed - waiting for light barrier",
        2: "Measuring force peak",
        3: "Last measurement valid",
        4: "Last measurement rejected",
    }

    def __init__(self, ads: AdsController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ads = ads
        self.session_running = False
        self.polling_requested = False
        self.last_result_counter: int | None = None
        self.measurements: list[dict] = []
        self.setWindowTitle("Force Peak Delay Measurement")
        self.setModal(True)
        self.setMinimumSize(820, 560)
        self._build_ui()
        self._connect_signals()
        self.polling_requested = True
        self.ads.set_force_delay_polling(True)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        settings_box = QGroupBox("Measurement Settings")
        settings = QGridLayout(settings_box)
        self.array_input = QComboBox()
        for index in range(1, ARRAY_COUNT + 1):
            self.array_input.addItem(f"Array {index}", index)
        self.light_barrier_input = QComboBox()
        for index in (2, 4, 6):
            self.light_barrier_input.addItem(f"Light barrier {index}", index)
        self.force_sensor_input = QComboBox()
        self.force_sensor_input.addItem("Force sensor 1", 1)
        self.force_sensor_input.addItem("Force sensor 2", 2)
        self.window_input = QSpinBox()
        self.window_input.setRange(
            FORCE_DELAY_WINDOW_MIN_MS, FORCE_DELAY_WINDOW_MAX_MS
        )
        self.window_input.setSuffix(" ms")
        self.window_input.setValue(FORCE_DELAY_WINDOW_DEFAULT_MS)
        self.minimum_rise_input = QDoubleSpinBox()
        self.minimum_rise_input.setRange(0.0, 10.0)
        self.minimum_rise_input.setDecimals(3)
        self.minimum_rise_input.setSingleStep(0.01)
        self.minimum_rise_input.setValue(FORCE_DELAY_MIN_RISE_DEFAULT)
        self.minimum_rise_input.setSuffix(" signal")
        self.response_delay_input = QDoubleSpinBox()
        self.response_delay_input.setRange(0.0, 1000.0)
        self.response_delay_input.setDecimals(1)
        self.response_delay_input.setSingleStep(0.1)
        self.response_delay_input.setSuffix(" ms")
        self.response_delay_input.setValue(
            self.ads.force_response_delays_ms[0]
        )
        self.single_response_delay_input = QDoubleSpinBox()
        self.single_response_delay_input.setRange(0.0, 1000.0)
        self.single_response_delay_input.setDecimals(1)
        self.single_response_delay_input.setSingleStep(0.1)
        self.single_response_delay_input.setSuffix(" ms")
        self.single_response_delay_input.setValue(
            self.ads.force_single_nozzle_response_delays_ms[0]
        )
        self.effective_response_label = QLabel("15.0 ms (4 active nozzles)")
        self.apply_response_button = QPushButton("Apply Compensation")
        settings.addWidget(QLabel("Array"), 0, 0)
        settings.addWidget(self.array_input, 0, 1)
        settings.addWidget(QLabel("Light barrier"), 0, 2)
        settings.addWidget(self.light_barrier_input, 0, 3)
        settings.addWidget(QLabel("Force sensor"), 1, 0)
        settings.addWidget(self.force_sensor_input, 1, 1)
        settings.addWidget(QLabel("Peak window"), 1, 2)
        settings.addWidget(self.window_input, 1, 3)
        settings.addWidget(QLabel("Minimum peak rise"), 2, 0)
        settings.addWidget(self.minimum_rise_input, 2, 1)
        settings.addWidget(QLabel("Four-nozzle response"), 2, 2)
        settings.addWidget(self.response_delay_input, 2, 3)
        settings.addWidget(QLabel("Single-nozzle response"), 3, 2)
        settings.addWidget(self.single_response_delay_input, 3, 3)
        settings.addWidget(QLabel("Effective response"), 4, 2)
        settings.addWidget(self.effective_response_label, 4, 3)
        settings.addWidget(self.apply_response_button, 5, 3)
        layout.addWidget(settings_box)

        command_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Measurement")
        self.start_button.setEnabled(self.ads.is_connected)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.stop_button.setEnabled(False)
        self.reset_button = QPushButton("Reset Session")
        command_layout.addWidget(self.start_button)
        command_layout.addWidget(self.stop_button)
        command_layout.addWidget(self.reset_button)
        command_layout.addStretch(1)
        layout.addLayout(command_layout)

        live_box = QGroupBox("Live Measurement")
        live = QGridLayout(live_box)
        self.state_label = QLabel("Connecting")
        self.current_signal_label = QLabel("0.000")
        self.last_delay_label = QLabel("-")
        self.last_peak_label = QLabel("-")
        self.count_label = QLabel("0 valid / 0 invalid")
        live.addWidget(QLabel("State"), 0, 0)
        live.addWidget(self.state_label, 0, 1)
        live.addWidget(QLabel("Current signal"), 0, 2)
        live.addWidget(self.current_signal_label, 0, 3)
        live.addWidget(QLabel("Last peak delay"), 1, 0)
        live.addWidget(self.last_delay_label, 1, 1)
        live.addWidget(QLabel("Last peak"), 1, 2)
        live.addWidget(self.last_peak_label, 1, 3)
        live.addWidget(QLabel("Session results"), 2, 0)
        live.addWidget(self.count_label, 2, 1)
        layout.addWidget(live_box)

        statistics_box = QGroupBox("Valid Delay Statistics")
        statistics_layout = QGridLayout(statistics_box)
        self.mean_label = QLabel("-")
        self.std_label = QLabel("-")
        self.range_label = QLabel("-")
        self.cv_label = QLabel("-")
        statistics_layout.addWidget(QLabel("Mean"), 0, 0)
        statistics_layout.addWidget(self.mean_label, 0, 1)
        statistics_layout.addWidget(QLabel("Standard deviation"), 0, 2)
        statistics_layout.addWidget(self.std_label, 0, 3)
        statistics_layout.addWidget(QLabel("Range"), 1, 0)
        statistics_layout.addWidget(self.range_label, 1, 1)
        statistics_layout.addWidget(QLabel("Coefficient of variation"), 1, 2)
        statistics_layout.addWidget(self.cv_label, 1, 3)
        layout.addWidget(statistics_box)

        self.results_table = QTableWidget(0, 7)
        self.results_table.setHorizontalHeaderLabels(
            ["#", "Time", "Result", "Delay", "Baseline", "Peak", "Rise"]
        )
        self.results_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self.results_table, 1)

        close_layout = QHBoxLayout()
        close_layout.addStretch(1)
        self.close_button = QPushButton("Close")
        close_layout.addWidget(self.close_button)
        layout.addLayout(close_layout)

    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        self.reset_button.clicked.connect(self._reset_session)
        self.close_button.clicked.connect(self.close)
        self.array_input.currentIndexChanged.connect(self._array_changed)
        self.apply_response_button.clicked.connect(self._apply_response_delay)
        self.ads.force_delay_status_ready.connect(self._apply_status)
        self.ads.connection_changed.connect(self._connection_changed)
        self.ads.operation_failed.connect(self._on_ads_error)

    def _set_settings_enabled(self, enabled: bool) -> None:
        self.array_input.setEnabled(enabled)
        self.light_barrier_input.setEnabled(enabled)
        self.force_sensor_input.setEnabled(enabled)
        self.window_input.setEnabled(enabled)
        self.minimum_rise_input.setEnabled(enabled)
        self.response_delay_input.setEnabled(enabled)
        self.single_response_delay_input.setEnabled(enabled)
        self.apply_response_button.setEnabled(enabled and self.ads.is_connected)

    def _array_changed(self) -> None:
        array_index = int(self.array_input.currentData())
        with QSignalBlocker(self.response_delay_input):
            self.response_delay_input.setValue(
                self.ads.force_response_delays_ms[array_index - 1]
            )
        with QSignalBlocker(self.single_response_delay_input):
            self.single_response_delay_input.setValue(
                self.ads.force_single_nozzle_response_delays_ms[array_index - 1]
            )

    def _apply_response_delay(self) -> None:
        array_index = int(self.array_input.currentData())
        four_nozzle_ms = self.response_delay_input.value()
        single_nozzle_ms = self.single_response_delay_input.value()
        self.ads.force_response_delays_ms[array_index - 1] = four_nozzle_ms
        self.ads.force_single_nozzle_response_delays_ms[
            array_index - 1
        ] = single_nozzle_ms
        self.ads.set_force_response_delays(
            array_index, single_nozzle_ms, four_nozzle_ms
        )
        self.state_label.setText(
            f"Array {array_index} compensation queued"
        )

    def _start(self) -> None:
        if not self.ads.is_connected:
            self.state_label.setText("ADS offline")
            return
        self._clear_session()
        self.session_running = True
        self._set_settings_enabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.state_label.setText("Starting")
        self.ads.start_force_delay_measurement(
            int(self.light_barrier_input.currentData()),
            int(self.force_sensor_input.currentData()),
            self.window_input.value(),
            self.minimum_rise_input.value(),
        )

    def _stop(self) -> None:
        if self.session_running:
            self.ads.stop_force_delay_measurement()
        self.session_running = False
        self._set_settings_enabled(True)
        self.start_button.setEnabled(self.ads.is_connected)
        self.stop_button.setEnabled(False)
        self.state_label.setText("Stopped")

    def _reset_session(self) -> None:
        self._clear_session()

    def _clear_session(self) -> None:
        self.measurements.clear()
        self.results_table.setRowCount(0)
        self.last_delay_label.setText("-")
        self.last_peak_label.setText("-")
        self._update_statistics()

    @pyqtSlot(object)
    def _apply_status(self, status: dict) -> None:
        self.current_signal_label.setText(f'{status["current_signal"]:.3f}')
        selected_array = int(self.array_input.currentData())
        active_counts = status.get("active_nozzle_counts", [4] * ARRAY_COUNT)
        effective_delays = status.get("effective_response_delays_ms")
        active_count = int(active_counts[selected_array - 1])
        if effective_delays is None:
            effective_delay = calculate_force_response_delay(
                self.ads.force_single_nozzle_response_delays_ms[
                    selected_array - 1
                ],
                self.ads.force_response_delays_ms[selected_array - 1],
                active_count,
            )
        else:
            effective_delay = float(effective_delays[selected_array - 1])
        self.effective_response_label.setText(
            f"{effective_delay:.2f} ms ({active_count} active nozzles)"
        )
        self.state_label.setText(
            self.STATUS_TEXT.get(int(status["status_code"]), "Unknown state")
        )
        if not self.session_running:
            self.last_result_counter = int(status["result_counter"])
            return

        result_counter = int(status["result_counter"])
        if self.last_result_counter is None:
            self.last_result_counter = result_counter
            return
        if result_counter == self.last_result_counter:
            return
        self.last_result_counter = result_counter
        measurement = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "array": int(self.array_input.currentData()),
            "light_barrier": int(status["light_barrier"]),
            "sensor": int(status["sensor"]),
            "window_ms": int(status["window_ms"]),
            "minimum_rise": float(status["minimum_rise"]),
            "response_delay_ms": float(
                self.ads.force_response_delays_ms[
                    int(self.array_input.currentData()) - 1
                ]
            ),
            "single_nozzle_response_delay_ms": float(
                self.ads.force_single_nozzle_response_delays_ms[
                    int(self.array_input.currentData()) - 1
                ]
            ),
            "effective_response_delay_ms": effective_delay,
            "active_nozzle_count": active_count,
            "light_barrier_time_ms": int(status["light_barrier_time_ms"]),
            "peak_time_ms": int(status["peak_time_ms"]),
            "delay_ms": int(status["peak_delay_ms"]),
            "baseline": float(status["baseline"]),
            "peak": float(status["peak"]),
            "rise": float(status["peak_rise"]),
            "valid": bool(status["last_valid"]),
        }
        measurement["reason"] = (
            "" if measurement["valid"] else "peak rise below minimum"
        )
        self.measurements.append(measurement)
        self._append_result_row(measurement)
        self._append_csv(measurement)
        self.last_delay_label.setText(f'{measurement["delay_ms"]} ms')
        self.last_peak_label.setText(
            f'{measurement["peak"]:.3f} (rise {measurement["rise"]:.3f})'
        )
        self._update_statistics()

    def _append_result_row(self, measurement: dict) -> None:
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        values = [
            str(len(self.measurements)),
            measurement["timestamp"].split("T")[-1],
            "Valid" if measurement["valid"] else "Rejected",
            f'{measurement["delay_ms"]} ms',
            f'{measurement["baseline"]:.3f}',
            f'{measurement["peak"]:.3f}',
            f'{measurement["rise"]:.3f}',
        ]
        for column, value in enumerate(values):
            self.results_table.setItem(row, column, QTableWidgetItem(value))
        self.results_table.scrollToBottom()

    def _update_statistics(self) -> None:
        valid_delays = [
            float(item["delay_ms"])
            for item in self.measurements
            if item["valid"]
        ]
        invalid_count = len(self.measurements) - len(valid_delays)
        self.count_label.setText(
            f"{len(valid_delays)} valid / {invalid_count} invalid"
        )
        if not valid_delays:
            self.mean_label.setText("-")
            self.std_label.setText("-")
            self.range_label.setText("-")
            self.cv_label.setText("-")
            return
        result = calculate_force_delay_statistics(valid_delays)
        self.mean_label.setText(f'{result["mean"]:.1f} ms')
        self.std_label.setText(f'{result["standard_deviation"]:.1f} ms')
        self.range_label.setText(
            f'{result["minimum"]:.0f} .. {result["maximum"]:.0f} ms'
        )
        self.cv_label.setText(
            f'{result["coefficient_of_variation"]:.2f} %'
        )

    def _append_csv(self, measurement: dict) -> None:
        header = [
            "local_timestamp",
            "array",
            "light_barrier",
            "force_sensor",
            "window_ms",
            "minimum_rise",
            "response_compensation_ms",
            "single_nozzle_response_ms",
            "effective_response_ms",
            "active_nozzle_count",
            "light_barrier_plc_time_ms",
            "peak_plc_time_ms",
            "peak_delay_ms",
            "baseline",
            "peak",
            "peak_rise",
            "valid",
            "reason",
        ]
        try:
            write_header = (
                not FORCE_DELAY_LOG_FILE.exists()
                or FORCE_DELAY_LOG_FILE.stat().st_size == 0
            )
            with FORCE_DELAY_LOG_FILE.open(
                "a", newline="", encoding="utf-8"
            ) as file:
                writer = csv.writer(file)
                if write_header:
                    writer.writerow(header)
                writer.writerow(
                    [
                        measurement["timestamp"],
                        measurement["array"],
                        measurement["light_barrier"],
                        measurement["sensor"],
                        measurement["window_ms"],
                        measurement["minimum_rise"],
                        measurement["response_delay_ms"],
                        measurement["single_nozzle_response_delay_ms"],
                        measurement["effective_response_delay_ms"],
                        measurement["active_nozzle_count"],
                        measurement["light_barrier_time_ms"],
                        measurement["peak_time_ms"],
                        measurement["delay_ms"],
                        measurement["baseline"],
                        measurement["peak"],
                        measurement["rise"],
                        int(measurement["valid"]),
                        measurement["reason"],
                    ]
                )
        except OSError as exc:
            self.state_label.setText(f"CSV log failed: {exc}")

    @pyqtSlot(bool, str)
    def _connection_changed(self, connected: bool, message: str) -> None:
        self.start_button.setEnabled(connected and not self.session_running)
        self.apply_response_button.setEnabled(
            connected and not self.session_running
        )
        if not connected:
            self.session_running = False
            self.stop_button.setEnabled(False)
            self._set_settings_enabled(True)
            self.state_label.setText(message or "ADS offline")

    @pyqtSlot(str, str)
    def _on_ads_error(self, context: str, message: str) -> None:
        if context.startswith("force_delay"):
            self.state_label.setText(message)

    def _leave_measurement(self) -> None:
        if not self.polling_requested:
            return
        self.polling_requested = False
        if self.ads.is_connected:
            self.ads.stop_force_delay_measurement()
        self.ads.set_force_delay_polling(False)
        try:
            self.ads.force_delay_status_ready.disconnect(self._apply_status)
            self.ads.connection_changed.disconnect(self._connection_changed)
            self.ads.operation_failed.disconnect(self._on_ads_error)
        except (TypeError, RuntimeError):
            pass

    def done(self, result: int) -> None:
        self._leave_measurement()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._leave_measurement()
        super().closeEvent(event)


class PressureControlWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.rows = [ArrayRow(index) for index in range(1, ARRAY_COUNT + 1)]
        self.last_shot_counter: int | None = None
        self.last_light_barrier_event_counts: list[int | None] = [None] * 6
        self.light_barrier_inverted = list(LIGHT_BARRIER_INVERT_DEFAULTS)
        self.light_barrier_debounce_enabled = list(
            LIGHT_BARRIER_DEBOUNCE_ENABLED_DEFAULTS
        )
        self.conveyor_calibration = {
            "marker_distance_mm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
            "mm_per_full_step": CONVEYOR_MM_PER_FULL_STEP_DEFAULT,
            "valid": True,
        }

        self.setWindowTitle("Nozzle Array Pressure Control")
        self.resize(1350, 440)

        self._build_ui()
        self.ads = AdsController(self)
        self.ur_angle = UrAngleController(self)
        self._connect_signals()
        self.logging_status.setText("Logging: connecting")
        self.statusBar().showMessage(f"Connecting to ADS controller ({ADS_TIMEOUT_MS} ms timeout)...")
        self.ads.start()

    def _build_ui(self) -> None:
        root = QWidget()
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        machine_layout = QHBoxLayout()
        machine_layout.addWidget(QLabel("Sensor spacing 1-2"))
        self.sensor_spacing_12 = QDoubleSpinBox()
        self.sensor_spacing_12.setRange(SENSOR_SPACING_MIN_MM, SENSOR_SPACING_MAX_MM)
        self.sensor_spacing_12.setSuffix(" mm")
        self.sensor_spacing_12.setDecimals(1)
        self.sensor_spacing_12.setSingleStep(1.0)
        self.sensor_spacing_12.setValue(SENSOR_SPACING_12_DEFAULT_MM)
        self.sensor_spacing_12.setToolTip("Physical distance between light barrier 1 and light barrier 2")
        machine_layout.addWidget(self.sensor_spacing_12)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Sensor spacing 3-4"))
        self.sensor_spacing_34 = QDoubleSpinBox()
        self.sensor_spacing_34.setRange(SENSOR_SPACING_MIN_MM, SENSOR_SPACING_MAX_MM)
        self.sensor_spacing_34.setSuffix(" mm")
        self.sensor_spacing_34.setDecimals(1)
        self.sensor_spacing_34.setSingleStep(1.0)
        self.sensor_spacing_34.setValue(SENSOR_SPACING_34_DEFAULT_MM)
        self.sensor_spacing_34.setToolTip("Physical distance between light barrier 3 and light barrier 4")
        machine_layout.addWidget(self.sensor_spacing_34)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Sensor spacing 5-6"))
        self.sensor_spacing_56 = QDoubleSpinBox()
        self.sensor_spacing_56.setRange(SENSOR_SPACING_MIN_MM, SENSOR_SPACING_MAX_MM)
        self.sensor_spacing_56.setSuffix(" mm")
        self.sensor_spacing_56.setDecimals(1)
        self.sensor_spacing_56.setSingleStep(1.0)
        self.sensor_spacing_56.setValue(SENSOR_SPACING_56_DEFAULT_MM)
        self.sensor_spacing_56.setToolTip("Physical distance between light barrier 5 and light barrier 6")
        machine_layout.addWidget(self.sensor_spacing_56)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Light barrier debounce"))
        self.light_barrier_debounce = QSpinBox()
        self.light_barrier_debounce.setRange(
            LIGHT_BARRIER_DEBOUNCE_MIN_MS, LIGHT_BARRIER_DEBOUNCE_MAX_MS
        )
        self.light_barrier_debounce.setSuffix(" ms")
        self.light_barrier_debounce.setValue(LIGHT_BARRIER_DEBOUNCE_DEFAULT_MS)
        self.light_barrier_debounce.setToolTip(
            "Stable signal time used by velocity measurement and every valve trigger"
        )
        machine_layout.addWidget(self.light_barrier_debounce)
        machine_layout.addSpacing(20)
        self.conveyor_enabled = QCheckBox("Conveyor")
        self.conveyor_enabled.setChecked(False)
        self.conveyor_enabled.setToolTip("Enable or disable the EL7047 conveyor motor")
        machine_layout.addWidget(self.conveyor_enabled)
        self.conveyor_reverse = QCheckBox("Reverse")
        self.conveyor_reverse.setChecked(False)
        self.conveyor_reverse.setToolTip("Reverse the conveyor motor direction")
        machine_layout.addWidget(self.conveyor_reverse)
        self.conveyor_reset_button = QPushButton("Reset")
        self.conveyor_reset_button.setToolTip("Pulse the EL7047 reset bit")
        machine_layout.addWidget(self.conveyor_reset_button)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Conveyor speed"))
        self.conveyor_speed = QDoubleSpinBox()
        self.conveyor_speed.setRange(CONVEYOR_SPEED_MIN_MM_PER_SEC, CONVEYOR_SPEED_MAX_MM_PER_SEC)
        self.conveyor_speed.setSuffix(" mm/s")
        self.conveyor_speed.setDecimals(1)
        self.conveyor_speed.setSingleStep(1.0)
        self.conveyor_speed.setValue(0.0)
        self.conveyor_speed.setToolTip("Conveyor belt speed setpoint stored in the motion profile")
        machine_layout.addWidget(self.conveyor_speed)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Conveyor max"))
        self.conveyor_max_speed = QDoubleSpinBox()
        self.conveyor_max_speed.setRange(CONVEYOR_MAX_SPEED_MIN_MM_PER_SEC, CONVEYOR_MAX_SPEED_MAX_MM_PER_SEC)
        self.conveyor_max_speed.setSuffix(" mm/s")
        self.conveyor_max_speed.setDecimals(1)
        self.conveyor_max_speed.setSingleStep(10.0)
        self.conveyor_max_speed.setValue(1000.0)
        self.conveyor_max_speed.setToolTip("Speed that corresponds to 100 percent EL7047 STM Velocity")
        machine_layout.addWidget(self.conveyor_max_speed)
        machine_layout.addStretch(1)
        main_layout.addLayout(machine_layout)

        control_box = QGroupBox("Online Settings")
        grid = QGridLayout(control_box)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        headers = [
            "Array",
            "Enabled",
            "Nozzles",
            "Pressure",
            "Delay",
            "Pulse Duration",
            "Offset",
            "Est. Velocity",
            "Est. Offset Delay",
            "Status",
        ]
        for column, text in enumerate(headers):
            label = QLabel(text)
            label.setStyleSheet("font-weight: 600;")
            grid.addWidget(label, 0, column)

        for row_number, row in enumerate(self.rows, start=1):
            grid.addWidget(QLabel(f"Array {row.index}"), row_number, 0)
            grid.addWidget(row.enabled, row_number, 1)
            grid.addWidget(row.nozzle_controls, row_number, 2)
            grid.addWidget(row.pressure, row_number, 3)
            grid.addWidget(row.delay, row_number, 4)
            grid.addWidget(row.pulse_duration, row_number, 5)
            grid.addWidget(row.offset, row_number, 6)
            grid.addWidget(row.estimated_velocity, row_number, 7)
            grid.addWidget(row.estimated_delay, row_number, 8)
            grid.addWidget(row.status, row_number, 9)

        grid.setColumnStretch(9, 1)
        main_layout.addWidget(control_box)

        ur_layout = QHBoxLayout()
        ur_layout.addWidget(QLabel("UR Ry (RPY)"))
        self.ur_angle_input = QDoubleSpinBox()
        self.ur_angle_input.setRange(UR_ANGLE_MIN_DEG, UR_ANGLE_MAX_DEG)
        self.ur_angle_input.setDecimals(1)
        self.ur_angle_input.setSingleStep(UR_ANGLE_STEP_DEG)
        self.ur_angle_input.setSuffix(" deg")
        self.ur_angle_input.setValue(UR_ANGLE_DEFAULT_DEG)
        self.ur_angle_input.setKeyboardTracking(False)
        self.ur_angle_input.setToolTip(
            "UR base-frame RPY pitch; roll remains -45 degrees and yaw -90 degrees"
        )
        ur_layout.addWidget(self.ur_angle_input)
        self.apply_ur_angle_button = QPushButton("Apply UR Angle")
        self.apply_ur_angle_button.setToolTip(
            "Move the running BiBaZu_Continuous UR program to this orientation"
        )
        ur_layout.addWidget(self.apply_ur_angle_button)
        self.ur_angle_status = QLabel(f"UR {UR_HOST}: ready")
        self.ur_angle_status.setMinimumWidth(260)
        ur_layout.addWidget(self.ur_angle_status)
        ur_layout.addStretch(1)
        main_layout.addLayout(ur_layout)

        button_layout = QHBoxLayout()
        self.reconnect_button = QPushButton("Reconnect")
        self.calibrate_conveyor_button = QPushButton("Calibrate Conveyor")
        self.calibrate_conveyor_button.setToolTip("Open the conveyor step calibration")
        self.jog_conveyor_button = QPushButton("Jog Conveyor")
        self.jog_conveyor_button.setToolTip("Move the conveyor by a calibrated distance")
        self.measure_force_delay_button = QPushButton("Measure Force Delay")
        self.measure_force_delay_button.setToolTip(
            "Measure the time from a light barrier edge to a force-sensor peak"
        )
        self.light_barrier_settings_button = QPushButton("Light Barrier Settings")
        self.light_barrier_settings_button.setToolTip(
            "Configure inversion and debounce separately for each light barrier"
        )
        self.light_barrier_settings_button.setEnabled(False)
        self.logging_status = QLabel("Logging: offline")
        self.logging_status.setMinimumWidth(170)
        self.load_button = QPushButton("Load Profile")
        self.save_button = QPushButton("Save Profile")
        self.write_all_button = QPushButton("Write All Values")

        button_layout.addWidget(self.reconnect_button)
        button_layout.addWidget(self.calibrate_conveyor_button)
        button_layout.addWidget(self.jog_conveyor_button)
        button_layout.addWidget(self.measure_force_delay_button)
        button_layout.addWidget(self.light_barrier_settings_button)
        button_layout.addWidget(self.logging_status)
        button_layout.addStretch(1)
        button_layout.addWidget(self.load_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.write_all_button)
        main_layout.addLayout(button_layout)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def _connect_signals(self) -> None:
        self.ads.connection_changed.connect(self.on_connection_changed)
        self.ads.initial_snapshot_ready.connect(self.apply_initial_snapshot)
        self.ads.live_snapshot_ready.connect(self.apply_live_snapshot)
        self.ads.write_finished.connect(self.on_write_finished)
        self.ads.operation_failed.connect(self.on_ads_error)
        self.reconnect_button.clicked.connect(self.reconnect)
        self.calibrate_conveyor_button.clicked.connect(self.open_conveyor_calibration)
        self.jog_conveyor_button.clicked.connect(self.open_conveyor_jogging)
        self.measure_force_delay_button.clicked.connect(self.open_force_delay_measurement)
        self.load_button.clicked.connect(self.load_profile)
        self.save_button.clicked.connect(self.save_profile)
        self.write_all_button.clicked.connect(self.write_all_values)
        self.apply_ur_angle_button.clicked.connect(self.apply_ur_angle)
        self.ur_angle.busy_changed.connect(self.on_ur_angle_busy_changed)
        self.ur_angle.angle_applied.connect(self.on_ur_angle_applied)
        self.ur_angle.operation_failed.connect(self.on_ur_angle_error)
        self.sensor_spacing_12.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing12Mm", value, "Sensor spacing 1-2")
        )
        self.sensor_spacing_34.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing34Mm", value, "Sensor spacing 3-4")
        )
        self.sensor_spacing_56.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing56Mm", value, "Sensor spacing 5-6")
        )
        self.light_barrier_debounce.valueChanged.connect(
            lambda value: self.ads.queue_write(
                "MAIN.GuiBarrierCalibrationDebounceMs",
                int(value),
                "Light barrier debounce",
            )
        )
        self.light_barrier_settings_button.clicked.connect(
            self.open_light_barrier_settings
        )
        self.conveyor_enabled.stateChanged.connect(
            lambda _state: self.write_conveyor_setting("enabled", self.conveyor_enabled.isChecked())
        )
        self.conveyor_reverse.stateChanged.connect(
            lambda _state: self.write_conveyor_setting("reverse", self.conveyor_reverse.isChecked())
        )
        self.conveyor_reset_button.clicked.connect(
            lambda: self.write_conveyor_setting("reset", True)
        )
        self.conveyor_speed.valueChanged.connect(
            lambda value: self.write_conveyor_setting("speed_mm_per_sec", value)
        )
        self.conveyor_max_speed.valueChanged.connect(
            lambda value: self.write_conveyor_setting("max_speed_mm_per_sec", value)
        )

        for row in self.rows:
            row.enabled.stateChanged.connect(
                lambda _state, r=row: self.write_value(r, "enabled", r.enabled.isChecked())
            )
            for nozzle_index, checkbox in enumerate(row.nozzle_enabled, start=1):
                checkbox.stateChanged.connect(
                    lambda _state, r=row, n=nozzle_index, c=checkbox: self.write_value(
                        r, f"nozzle_{n}_enabled", c.isChecked()
                    )
                )
            row.pressure.valueChanged.connect(
                lambda value, r=row: self.write_value(r, "pressure_mbar", value)
            )
            row.delay.valueChanged.connect(
                lambda value, r=row: self.write_value(r, "delay_ms", value)
            )
            row.pulse_duration.valueChanged.connect(
                lambda value, r=row: self.write_value(r, "pulse_duration_ms", value)
            )
            row.offset.valueChanged.connect(
                lambda value, r=row: self.write_value(r, "offset_mm", value)
            )

    def reconnect(self) -> None:
        self.last_shot_counter = None
        self.logging_status.setText("Logging: connecting")
        self.reconnect_button.setEnabled(False)
        self.statusBar().showMessage(f"Connecting to ADS controller ({ADS_TIMEOUT_MS} ms timeout)...")
        for row in self.rows:
            row.status.setText("connecting")
        self.ads.reconnect()

    def apply_ur_angle(self) -> None:
        angle_deg = float(self.ur_angle_input.value())
        self.ur_angle_status.setText(
            f"UR {UR_HOST}: applying Ry {angle_deg:.1f} deg"
        )
        self.ur_angle.apply_angle(angle_deg)

    @pyqtSlot(bool)
    def on_ur_angle_busy_changed(self, busy: bool) -> None:
        self.apply_ur_angle_button.setEnabled(not busy)
        self.ur_angle_input.setEnabled(not busy)

    @pyqtSlot(float)
    def on_ur_angle_applied(self, angle_deg: float) -> None:
        self.ur_angle_status.setText(
            f"UR {UR_HOST}: Ry {angle_deg:.1f} deg applied"
        )

    @pyqtSlot(str)
    def on_ur_angle_error(self, message: str) -> None:
        self.ur_angle_status.setText(f"UR {UR_HOST}: {message}")

    @pyqtSlot(bool, str)
    def on_connection_changed(self, connected: bool, message: str) -> None:
        self.reconnect_button.setEnabled(True)
        self.light_barrier_settings_button.setEnabled(connected)
        if connected:
            self.statusBar().showMessage(f"ADS online: {AMS_NET_ID} / {PLC_IP}")
            self.logging_status.setText("Logging: waiting")
        else:
            detail = message or "automatic reconnect pending"
            self.statusBar().showMessage(f"ADS offline: {detail}")
            self.logging_status.setText("Logging: offline")
            self.last_shot_counter = None
            self.last_light_barrier_event_counts = [None] * 6
            for row in self.rows:
                row.status.setText("offline")

    @pyqtSlot(object)
    def apply_initial_snapshot(self, snapshot: dict) -> None:
        spacing_12, spacing_34, spacing_56 = snapshot["sensor_spacings"]
        light_barrier_debounce = snapshot["light_barrier_debounce_ms"]
        self.light_barrier_inverted = list(
            snapshot.get("light_barrier_inverted", LIGHT_BARRIER_INVERT_DEFAULTS)
        )
        self.light_barrier_debounce_enabled = list(
            snapshot.get(
                "light_barrier_debounce_enabled",
                LIGHT_BARRIER_DEBOUNCE_ENABLED_DEFAULTS,
            )
        )
        conveyor_settings = snapshot["conveyor"]
        calibration = snapshot["calibration"]
        self.conveyor_calibration = {
            "marker_distance_mm": float(calibration["marker_distance_mm"]),
            "mm_per_full_step": float(calibration["mm_per_full_step"]),
            "valid": bool(calibration["valid"]),
        }
        with (
            QSignalBlocker(self.sensor_spacing_12),
            QSignalBlocker(self.sensor_spacing_34),
            QSignalBlocker(self.sensor_spacing_56),
            QSignalBlocker(self.light_barrier_debounce),
            QSignalBlocker(self.conveyor_enabled),
            QSignalBlocker(self.conveyor_reverse),
            QSignalBlocker(self.conveyor_speed),
            QSignalBlocker(self.conveyor_max_speed),
        ):
            self.sensor_spacing_12.setValue(spacing_12)
            self.sensor_spacing_34.setValue(spacing_34)
            self.sensor_spacing_56.setValue(spacing_56)
            self.light_barrier_debounce.setValue(light_barrier_debounce)
            self.conveyor_enabled.setChecked(bool(conveyor_settings["enabled"]))
            self.conveyor_reverse.setChecked(bool(conveyor_settings["reverse"]))
            self.conveyor_speed.setValue(float(conveyor_settings["speed_mm_per_sec"]))
            self.conveyor_max_speed.setValue(float(conveyor_settings["max_speed_mm_per_sec"]))

        arrays_by_index = {int(values["index"]): values for values in snapshot["arrays"]}
        for row in self.rows:
            if row.index in arrays_by_index:
                row.set_values(arrays_by_index[row.index])
            row.status.setText("read from PLC")

        for row in self.rows:
            row.estimated_velocity.setText("0.0 mm/s")
            row.estimated_delay.setText("0.0 ms")
            row.last_displayed_velocity = 0.0
            row.last_displayed_delay = 0.0

        self.last_shot_counter = None
        self.logging_status.setText("Logging: waiting")

    @pyqtSlot(str)
    def on_write_finished(self, context: str) -> None:
        calibration = self.ads.calibration_cache
        self.conveyor_calibration = {
            "marker_distance_mm": float(calibration["marker_distance_mm"]),
            "mm_per_full_step": float(calibration["mm_per_full_step"]),
            "valid": bool(calibration["valid"]),
        }
        self.statusBar().showMessage(f"ADS write complete: {context}")

    @pyqtSlot(str, str)
    def on_ads_error(self, context: str, message: str) -> None:
        self.statusBar().showMessage(f"{context}: {message}")

    def write_value(self, row: ArrayRow, field: str, value: bool | int | float) -> None:
        symbols = SYMBOLS[row.index]
        field_symbols = {
            "enabled": symbols.array_enabled,
            "pressure_mbar": symbols.pressure,
            "delay_ms": symbols.delay,
            "pulse_duration_ms": symbols.pulse_duration,
            "offset_mm": symbols.offset,
        }
        if field.startswith("nozzle_") and field.endswith("_enabled"):
            nozzle_index = int(field.split("_")[1]) - 1
            symbol = symbols.nozzle_enabled[nozzle_index]
        else:
            symbol = field_symbols[field]
        context = f"Array {row.index} {field}"
        self.ads.queue_write(symbol, value, context)
        row.status.setText(f"queued {datetime.now().strftime('%H:%M:%S')}")

    def write_all_values(self) -> None:
        values = {
            "MAIN.GuiSensorSpacing12Mm": float(self.sensor_spacing_12.value()),
            "MAIN.GuiSensorSpacing34Mm": float(self.sensor_spacing_34.value()),
            "MAIN.GuiSensorSpacing56Mm": float(self.sensor_spacing_56.value()),
            "MAIN.GuiBarrierCalibrationDebounceMs": int(
                self.light_barrier_debounce.value()
            ),
            **{
                f"MAIN.GuiLightBarrierInvert{index}": bool(inverted)
                for index, inverted in enumerate(
                    self.light_barrier_inverted, start=1
                )
            },
            **{
                f"MAIN.GuiLightBarrierDebounceEnabled{index}": bool(enabled)
                for index, enabled in enumerate(
                    self.light_barrier_debounce_enabled, start=1
                )
            },
            "MAIN.GuiConveyorEnabled": self.conveyor_enabled.isChecked(),
            "MAIN.GuiConveyorReverse": self.conveyor_reverse.isChecked(),
            "MAIN.GuiConveyorSpeedMmPerSec": float(self.conveyor_speed.value()),
            "MAIN.GuiConveyorMaxSpeedMmPerSec": float(self.conveyor_max_speed.value()),
            "MAIN.GuiCalibrationMarkerDistanceMm": float(
                self.conveyor_calibration["marker_distance_mm"]
            ),
            "MAIN.GuiConveyorMmPerFullStep": float(
                self.conveyor_calibration["mm_per_full_step"]
            ),
            "MAIN.GuiConveyorCalibrationValid": bool(
                self.conveyor_calibration["valid"]
            ),
        }
        values.update(
            {
                f"MAIN.GuiForceResponseDelayMs{index}": float(delay_ms)
                for index, delay_ms in enumerate(
                    self.ads.force_response_delays_ms, start=1
                )
            }
        )
        values.update(
            {
                f"MAIN.GuiForceSingleNozzleResponseDelayMs{index}": float(
                    delay_ms
                )
                for index, delay_ms in enumerate(
                    self.ads.force_single_nozzle_response_delays_ms, start=1
                )
            }
        )
        for row in self.rows:
            row_values = row.values()
            symbols = SYMBOLS[row.index]
            values[symbols.array_enabled] = bool(row_values["enabled"])
            values.update(
                {
                    symbol: bool(enabled)
                    for symbol, enabled in zip(
                        symbols.nozzle_enabled, row_values["nozzles_enabled"]
                    )
                }
            )
            values[symbols.pressure] = int(row_values["pressure_mbar"])
            values[symbols.delay] = int(row_values["delay_ms"])
            values[symbols.pulse_duration] = int(row_values["pulse_duration_ms"])
            values[symbols.offset] = float(row_values["offset_mm"])
            row.status.setText("queued")
        self.ads.write_now(values, "all settings")

    def write_sensor_spacing(self, symbol_name: str, value: float, label: str) -> None:
        self.ads.queue_write(symbol_name, float(value), label)
        self.statusBar().showMessage(f"{label} queued: {value:.1f} mm")

    def open_light_barrier_settings(self) -> None:
        if not self.ads.is_connected:
            self.statusBar().showMessage("Light barrier settings: ADS offline")
            return
        dialog = LightBarrierSettingsDialog(
            self.ads,
            self.light_barrier_inverted,
            self.light_barrier_debounce_enabled,
            self,
        )
        dialog.setting_changed.connect(self._update_light_barrier_setting)
        dialog.exec()

    @pyqtSlot(int, bool, bool)
    def _update_light_barrier_setting(
        self, sensor: int, inverted: bool, debounce_enabled: bool
    ) -> None:
        self.light_barrier_inverted[sensor - 1] = inverted
        self.light_barrier_debounce_enabled[sensor - 1] = debounce_enabled

    def write_conveyor_setting(self, field: str, value: bool | float) -> None:
        labels = {
            "enabled": "Conveyor enable",
            "reverse": "Conveyor reverse",
            "speed_mm_per_sec": "Conveyor speed",
            "max_speed_mm_per_sec": "Conveyor max speed",
            "reset": "Conveyor reset",
        }
        symbols = {
            "enabled": "MAIN.GuiConveyorEnabled",
            "reverse": "MAIN.GuiConveyorReverse",
            "speed_mm_per_sec": "MAIN.GuiConveyorSpeedMmPerSec",
            "max_speed_mm_per_sec": "MAIN.GuiConveyorMaxSpeedMmPerSec",
            "reset": "MAIN.GuiConveyorReset",
        }
        label = labels.get(field, field)
        typed_value = bool(value) if field in {"enabled", "reverse", "reset"} else float(value)
        if field in {"enabled", "reverse", "reset"}:
            self.ads.write_now({symbols[field]: typed_value}, label)
        else:
            self.ads.queue_write(symbols[field], typed_value, label)
        if isinstance(typed_value, bool):
            display_value = "on" if typed_value else "off"
        else:
            display_value = f"{typed_value:.1f} mm/s"
        self.statusBar().showMessage(f"{label} queued: {display_value}")

    def open_conveyor_calibration(self) -> None:
        if not self.ads.is_connected:
            self.statusBar().showMessage("Conveyor calibration: ADS offline")
            return

        with QSignalBlocker(self.conveyor_enabled):
            self.conveyor_enabled.setChecked(False)
        dialog = ConveyorCalibrationDialog(self.ads, self)
        dialog.exec()
        calibration = self.ads.calibration_cache
        self.conveyor_calibration = {
            "marker_distance_mm": float(calibration["marker_distance_mm"]),
            "mm_per_full_step": float(calibration["mm_per_full_step"]),
            "valid": bool(calibration["valid"]),
        }
        if self.conveyor_calibration["valid"]:
            self.statusBar().showMessage(
                "Conveyor calibrated: "
                f'{self.conveyor_calibration["mm_per_full_step"]:.6f} mm/full step'
            )
        else:
            self.statusBar().showMessage("Conveyor calibration closed")

    def open_conveyor_jogging(self) -> None:
        if not self.ads.is_connected:
            self.statusBar().showMessage("Conveyor jogging: ADS offline")
            return
        calibration = self.ads.calibration_cache
        if not bool(calibration["valid"]) or float(calibration["mm_per_full_step"]) <= 0.0:
            self.statusBar().showMessage("Conveyor jogging: calibrate the conveyor first")
            return

        with QSignalBlocker(self.conveyor_enabled):
            self.conveyor_enabled.setChecked(False)
        dialog = ConveyorJogDialog(self.ads, self)
        dialog.exec()
        self.statusBar().showMessage("Conveyor jogging closed")

    def open_force_delay_measurement(self) -> None:
        if not self.ads.is_connected:
            self.statusBar().showMessage("Force delay measurement: ADS offline")
            return
        dialog = ForceDelayDialog(self.ads, self)
        dialog.exec()
        self.statusBar().showMessage("Force delay measurement closed")

    @pyqtSlot(object)
    def apply_live_snapshot(self, snapshot: dict) -> None:
        velocities = snapshot["velocities"]
        delays = snapshot["delays"]
        self._log_light_barrier_events(snapshot)
        for row, velocity, delay in zip(self.rows, velocities, delays):
            if (
                row.last_displayed_velocity is None
                or abs(velocity - row.last_displayed_velocity) > ESTIMATE_DISPLAY_EPSILON
            ):
                row.estimated_velocity.setText(f"{velocity:.1f} mm/s")
                row.last_displayed_velocity = velocity
            if (
                row.last_displayed_delay is None
                or abs(delay - row.last_displayed_delay) > ESTIMATE_DISPLAY_EPSILON
            ):
                row.estimated_delay.setText(f"{delay:.1f} ms")
                row.last_displayed_delay = delay
        shot_counter = int(snapshot["shot_counter"])
        if self.last_shot_counter is None:
            self.last_shot_counter = shot_counter
            self.logging_status.setText("Logging: waiting")
        elif shot_counter != self.last_shot_counter:
            self.append_pressure_log(
                float(snapshot["avg_pressure_n1"]),
                float(snapshot["avg_pressure_n2"]),
                *[float(velocity) for velocity in velocities],
            )
            self.last_shot_counter = shot_counter
            self.logging_status.setText(f"Logged shot {shot_counter}")

    def _log_light_barrier_events(self, snapshot: dict) -> None:
        events = snapshot.get("light_barrier_events", [])
        if not events:
            return
        rows = []
        velocities = snapshot["velocities"]
        velocity_times = snapshot["velocity_times_ms"]
        velocity_valid = snapshot["velocity_valid"]
        for event in events:
            index = int(event["barrier"]) - 1
            count = int(event["count"])
            previous_count = self.last_light_barrier_event_counts[index]
            self.last_light_barrier_event_counts[index] = count
            if previous_count is None:
                continue
            event_delta = (count - previous_count) & 0xFFFFFFFF
            if event_delta == 0:
                continue
            pair_index = index // 2
            rows.append(
                [
                    datetime.now().isoformat(timespec="milliseconds"),
                    int(event["plc_time_ms"]),
                    index + 1,
                    "ON" if bool(event["state"]) else "OFF",
                    int(bool(event["raw_state"])),
                    count,
                    event_delta,
                    max(0, event_delta - 1),
                    int(event["position_increments"]),
                    int(velocity_times[pair_index]),
                    float(velocities[pair_index]),
                    int(bool(velocity_valid[pair_index])),
                ]
            )
        if not rows:
            return
        try:
            write_header = not LIGHT_BARRIER_EVENT_LOG_FILE.exists()
            with LIGHT_BARRIER_EVENT_LOG_FILE.open(
                "a", newline="", encoding="utf-8"
            ) as file:
                writer = csv.writer(file)
                if write_header:
                    writer.writerow(
                        [
                            "local_timestamp",
                            "plc_event_time_ms",
                            "light_barrier",
                            "filtered_state",
                            "raw_state_at_poll",
                            "event_count",
                            "events_since_poll",
                            "events_not_individually_logged",
                            "conveyor_position_increments",
                            "pair_travel_time_ms",
                            "pair_velocity_mm_per_sec",
                            "pair_measurement_valid",
                        ]
                    )
                writer.writerows(rows)
        except OSError as exc:
            self.statusBar().showMessage(f"Light barrier log failed: {exc}")

    def append_pressure_log(
        self,
        avg_n1: float,
        avg_n2: float,
        velocity_1: float,
        velocity_2: float,
        velocity_3: float,
        velocity_4: float,
    ) -> None:
        write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0

        with CSV_FILE.open("a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if write_header:
                writer.writerow(CSV_HEADER)
            writer.writerow([
                datetime.now().isoformat(timespec="milliseconds"),
                avg_n1,
                avg_n2,
                velocity_1,
                velocity_2,
                velocity_3,
                velocity_4,
            ])

    def save_profile(self) -> None:
        PROFILE_DIR.mkdir(exist_ok=True)
        default_name = f"profile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Profile",
            str(PROFILE_DIR / default_name),
            "JSON Profile (*.json)",
        )
        if not path:
            return

        profile = {
            "version": PROFILE_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "ur_ry_angle_deg": self.ur_angle_input.value(),
            "light_barrier_debounce_ms": self.light_barrier_debounce.value(),
            "light_barrier_inverted": list(self.light_barrier_inverted),
            "light_barrier_debounce_enabled": list(
                self.light_barrier_debounce_enabled
            ),
            "conveyor_enabled": self.conveyor_enabled.isChecked(),
            "conveyor_reverse": self.conveyor_reverse.isChecked(),
            "conveyor_speed_mm_per_sec": self.conveyor_speed.value(),
            "conveyor_max_speed_mm_per_sec": self.conveyor_max_speed.value(),
            "conveyor_calibration": self.conveyor_calibration.copy(),
            "force_response_delays_ms": list(
                self.ads.force_response_delays_ms
            ),
            "force_single_nozzle_response_delays_ms": list(
                self.ads.force_single_nozzle_response_delays_ms
            ),
            "arrays": [row.values() for row in self.rows],
        }

        try:
            Path(path).write_text(json.dumps(profile, indent=2), encoding="utf-8")
            self.statusBar().showMessage(f"Profile saved: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))

    def load_profile(self) -> None:
        PROFILE_DIR.mkdir(exist_ok=True)
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Load Profile",
            str(PROFILE_DIR),
            "JSON Profile (*.json)",
        )
        if not path:
            return

        try:
            profile = json.loads(Path(path).read_text(encoding="utf-8"))
            profile_version = int(profile.get("version", 1))
            if profile_version not in range(1, PROFILE_VERSION + 1):
                raise ValueError("Unknown profile version")

            ur_angle_deg = float(
                profile.get("ur_ry_angle_deg", UR_ANGLE_DEFAULT_DEG)
            )
            if not UR_ANGLE_MIN_DEG <= ur_angle_deg <= UR_ANGLE_MAX_DEG:
                raise ValueError("UR Ry angle must be between 15.5 and 21.0 degrees")

            conveyor_enabled = bool(profile.get("conveyor_enabled", False))
            conveyor_reverse = bool(profile.get("conveyor_reverse", False))
            conveyor_speed = float(profile.get("conveyor_speed_mm_per_sec", self.conveyor_speed.value()))
            conveyor_max_speed = float(
                profile.get("conveyor_max_speed_mm_per_sec", self.conveyor_max_speed.value())
            )
            if profile_version >= 2:
                calibration_data = profile.get("conveyor_calibration", {})
                calibration_mm_per_step = float(
                    calibration_data.get("mm_per_full_step", 0.0)
                )
                self.conveyor_calibration = {
                    "marker_distance_mm": float(
                        calibration_data.get(
                            "marker_distance_mm", CALIBRATION_MARKER_DISTANCE_DEFAULT_MM
                        )
                    ),
                    "mm_per_full_step": calibration_mm_per_step,
                    "valid": bool(calibration_data.get("valid", False))
                    and calibration_mm_per_step > 0.0,
                }
            else:
                self.conveyor_calibration = {
                    "marker_distance_mm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
                    "mm_per_full_step": 0.0,
                    "valid": False,
                }
            if profile_version >= 3:
                response_delays = profile.get(
                    "force_response_delays_ms", FORCE_RESPONSE_DELAY_DEFAULTS_MS
                )
                if len(response_delays) != ARRAY_COUNT:
                    raise ValueError("Force response delay list must contain four values")
                self.ads.force_response_delays_ms = [
                    max(0.0, min(1000.0, float(value)))
                    for value in response_delays
                ]
            else:
                self.ads.force_response_delays_ms = list(
                    FORCE_RESPONSE_DELAY_DEFAULTS_MS
                )
            if profile_version >= 4:
                single_response_delays = profile.get(
                    "force_single_nozzle_response_delays_ms",
                    FORCE_SINGLE_NOZZLE_RESPONSE_DELAY_DEFAULTS_MS,
                )
                if len(single_response_delays) != ARRAY_COUNT:
                    raise ValueError(
                        "Single-nozzle response delay list must contain four values"
                    )
                self.ads.force_single_nozzle_response_delays_ms = [
                    max(0.0, min(1000.0, float(value)))
                    for value in single_response_delays
                ]
            else:
                self.ads.force_single_nozzle_response_delays_ms = list(
                    FORCE_SINGLE_NOZZLE_RESPONSE_DELAY_DEFAULTS_MS
                )
            light_barrier_debounce = int(
                profile.get(
                    "light_barrier_debounce_ms",
                    self.light_barrier_debounce.value(),
                )
            )
            profile_inversions = profile.get(
                "light_barrier_inverted", self.light_barrier_inverted
            )
            if not isinstance(profile_inversions, list) or len(profile_inversions) != 6:
                raise ValueError("Light barrier inversion list must contain six values")
            self.light_barrier_inverted = [
                bool(value) for value in profile_inversions
            ]
            profile_debounce_enabled = profile.get(
                "light_barrier_debounce_enabled",
                self.light_barrier_debounce_enabled,
            )
            if (
                not isinstance(profile_debounce_enabled, list)
                or len(profile_debounce_enabled) != 6
            ):
                raise ValueError(
                    "Light barrier debounce enable list must contain six values"
                )
            self.light_barrier_debounce_enabled = [
                bool(value) for value in profile_debounce_enabled
            ]
            with (
                QSignalBlocker(self.ur_angle_input),
                QSignalBlocker(self.light_barrier_debounce),
                QSignalBlocker(self.conveyor_enabled),
                QSignalBlocker(self.conveyor_reverse),
                QSignalBlocker(self.conveyor_speed),
                QSignalBlocker(self.conveyor_max_speed),
            ):
                self.ur_angle_input.setValue(ur_angle_deg)
                self.light_barrier_debounce.setValue(light_barrier_debounce)
                self.conveyor_enabled.setChecked(conveyor_enabled)
                self.conveyor_reverse.setChecked(conveyor_reverse)
                self.conveyor_speed.setValue(conveyor_speed)
                self.conveyor_max_speed.setValue(conveyor_max_speed)

            arrays = profile.get("arrays", [])
            if len(arrays) > ARRAY_COUNT:
                values_by_index = {}
                legacy_by_index = {int(item["index"]): item for item in arrays}
                legacy_nozzles_per_array = 2
                for row_index in range(1, ARRAY_COUNT + 1):
                    first_index = ((row_index - 1) * legacy_nozzles_per_array) + 1
                    legacy_nozzles = [
                        legacy_by_index.get(first_index + nozzle_offset, {})
                        for nozzle_offset in range(legacy_nozzles_per_array)
                    ]
                    parameter_source = legacy_nozzles[0] if legacy_nozzles else {}
                    for legacy_nozzle in legacy_nozzles[1:]:
                        if bool(legacy_nozzle.get("enabled", False)) and not bool(parameter_source.get("enabled", False)):
                            parameter_source = legacy_nozzle
                    values_by_index[row_index] = {
                        **parameter_source,
                        "index": row_index,
                        "enabled": any(bool(nozzle.get("enabled", False)) for nozzle in legacy_nozzles),
                        "nozzles_enabled": [
                            *[bool(nozzle.get("enabled", False)) for nozzle in legacy_nozzles],
                            *[False] * (NOZZLES_PER_ARRAY - legacy_nozzles_per_array),
                        ],
                    }
            else:
                values_by_index = {int(item["index"]): item for item in arrays}
            for row in self.rows:
                if row.index in values_by_index:
                    row.set_values(values_by_index[row.index])

            self.write_all_values()
            self.statusBar().showMessage(f"Profile loaded: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Load Failed", str(exc))

    def closeEvent(self, event) -> None:
        self.ur_angle.shutdown()
        self.ads.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = PressureControlWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
