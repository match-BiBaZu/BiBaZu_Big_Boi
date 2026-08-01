import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyads
from PyQt6.QtCore import QObject, Qt, QSignalBlocker, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)


AMS_NET_ID = "10.145.4.14.1.1"
PLC_IP = "192.168.10.23"
PLC_PORT = pyads.PORT_TC3PLC1

PROFILE_DIR = Path("pressure_profiles")
PROFILE_VERSION = 2
CSV_FILE = Path("pressure_log.csv")
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
ADS_TIMEOUT_MS = 1000

ARRAY_COUNT = 4
NOZZLES_PER_ARRAY = 4
PRESSURE_MIN_MBAR = 0
PRESSURE_MAX_MBAR = 6000
DELAY_MIN_MS = 0
DELAY_MAX_MS = 1000
PULSE_MIN_MS = 1
PULSE_MAX_MS = 500
SENSOR_SPACING_MIN_MM = 1.0
SENSOR_SPACING_MAX_MM = 5000.0
OFFSET_MIN_MM = 0.0
OFFSET_MAX_MM = 5000.0
TRAVEL_TIME_MIN_MS = 1
TRAVEL_TIME_MAX_MS = 30000
CONVEYOR_SPEED_MIN_MM_PER_SEC = 0.0
CONVEYOR_SPEED_MAX_MM_PER_SEC = 5000.0
CONVEYOR_MAX_SPEED_MIN_MM_PER_SEC = 1.0
CONVEYOR_MAX_SPEED_MAX_MM_PER_SEC = 5000.0
ESTIMATE_POLL_INTERVAL_MS = 750
ESTIMATE_DISPLAY_EPSILON = 0.05
CALIBRATION_POLL_INTERVAL_MS = 100
CALIBRATION_MARKER_DISTANCE_DEFAULT_MM = 315.0
CALIBRATION_JOG_STEPS_DEFAULT = 100
CALIBRATION_JOG_SPEED_DEFAULT = 10.0

pyads.set_timeout(ADS_TIMEOUT_MS)


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
    measured_valve_delay: str | None


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
        measured_valve_delay=(
            f"MAIN.MeasuredValveTriggerDelayMs{index}" if index <= 3 else None
        ),
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

    def read_travel_time_bounds(self) -> tuple[int, int]:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        return (
            self.plc.read_by_name("MAIN.GuiMinTravelTimeMs", pyads.PLCTYPE_UINT),
            self.plc.read_by_name("MAIN.GuiMaxTravelTimeMs", pyads.PLCTYPE_UINT),
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

    def read_estimates(self) -> tuple[list[float], list[float], list[float | None]]:
        if self.plc is None:
            raise RuntimeError("ADS is offline")
        velocities = [
            self.plc.read_by_name(SYMBOLS[index].estimated_velocity, pyads.PLCTYPE_REAL)
            for index in range(1, ARRAY_COUNT + 1)
        ]
        delays = [
            self.plc.read_by_name(SYMBOLS[index].estimated_delay, pyads.PLCTYPE_REAL)
            for index in range(1, ARRAY_COUNT + 1)
        ]
        measured_valve_delays = [
            self.plc.read_by_name(symbol, pyads.PLCTYPE_REAL) if symbol is not None else None
            for index in range(1, ARRAY_COUNT + 1)
            for symbol in [SYMBOLS[index].measured_valve_delay]
        ]
        return velocities, delays, measured_valve_delays

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


class AdsConnectWorker(QObject):
    finished = pyqtSignal(object, str)

    def run(self) -> None:
        client = AdsClient()
        if client.connect():
            self.finished.emit(client, "")
        else:
            self.finished.emit(None, client.last_error)


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
        first_nozzle = ((index - 1) * NOZZLES_PER_ARRAY) + 1

        self.enabled = QCheckBox()
        self.enabled.setChecked(index <= 2)
        self.enabled.setToolTip(f"Enable or disable array {index}")

        self.nozzle_enabled = []
        self.nozzle_controls = QWidget()
        nozzle_layout = QHBoxLayout(self.nozzle_controls)
        nozzle_layout.setContentsMargins(0, 0, 0, 0)
        nozzle_layout.setSpacing(8)
        for nozzle_number in range(first_nozzle, first_nozzle + NOZZLES_PER_ARRAY):
            checkbox = QCheckBox(f"N{nozzle_number}")
            checkbox.setChecked(True)
            checkbox.setToolTip(f"Enable or disable nozzle {nozzle_number}")
            self.nozzle_enabled.append(checkbox)
            nozzle_layout.addWidget(checkbox)

        self.pressure = QSpinBox()
        self.pressure.setRange(PRESSURE_MIN_MBAR, PRESSURE_MAX_MBAR)
        self.pressure.setSuffix(" mbar")
        self.pressure.setSingleStep(50)
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

        self.measured_valve_delay = QLabel("0.0 ms" if index <= 3 else "N/A")
        self.measured_valve_delay.setMinimumWidth(90)
        self.measured_valve_delay.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.measured_valve_delay.setToolTip(
            f"Measured delay from light barrier {index * 2} to the first array valve command"
            if index <= 3
            else "Array 4 has no corresponding delay measurement"
        )
        self.last_displayed_valve_delay: float | None = 0.0

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


class ConveyorCalibrationDialog(QDialog):
    STATUS_TEXT = {
        0: "Ready",
        1: "Starting move",
        2: "Moving",
        3: "Move complete",
        4: "Command rejected",
        5: "EL7047 error",
    }

    def __init__(self, ads: AdsClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ads = ads
        self._calibration_mode_requested = False
        self.setWindowTitle("Conveyor Calibration")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build_ui()
        self._connect_signals()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(CALIBRATION_POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.refresh_status)

        try:
            settings = self.ads.read_conveyor_calibration()
            with (
                QSignalBlocker(self.marker_distance),
                QSignalBlocker(self.jog_steps),
                QSignalBlocker(self.jog_speed),
            ):
                self.marker_distance.setValue(float(settings["marker_distance_mm"]))
                self.jog_steps.setValue(int(settings["jog_steps"]))
                self.jog_speed.setValue(float(settings["jog_speed_full_steps_per_sec"]))
            self._calibration_mode_requested = True
            self.ads.set_calibration_mode(True)
            self.poll_timer.start()
            self.refresh_status()
        except Exception as exc:
            self._show_error(exc)

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
        movement_layout.addWidget(self.move_left_button)
        movement_layout.addWidget(self.stop_button)
        movement_layout.addWidget(self.move_right_button)
        layout.addLayout(movement_layout)

        mark_layout = QHBoxLayout()
        self.capture_left_button = QPushButton("Calibrate Left Marking")
        self.capture_right_button = QPushButton("Calibrate Right Marking")
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
        try:
            self.ads.write_calibration_marker_distance(value)
        except Exception as exc:
            self._show_error(exc)

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
        try:
            self.ads.capture_calibration_mark(mark)
        except Exception as exc:
            self._show_error(exc)

    def _stop(self) -> None:
        try:
            self.ads.stop_calibration_move()
            self.state_label.setText("Stopping")
        except Exception as exc:
            self._show_error(exc)

    def refresh_status(self) -> None:
        try:
            status = self.ads.read_calibration_status()
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
                f'{status["left_position"]} increments'
                if status["left_valid"]
                else "Not captured"
            )
            self.right_position_label.setText(
                f'{status["right_position"]} increments'
                if status["right_valid"]
                else "Not captured"
            )
            self.increment_difference_label.setText(
                f'{status["increment_difference"]} increments'
            )
            self.step_difference_label.setText(
                f'{status["full_step_difference"]:.3f} full steps'
            )
            if status["valid"]:
                self.mm_per_step_label.setText(
                    f'{status["mm_per_full_step"]:.6f} mm/full step'
                )
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
        except Exception as exc:
            self._show_error(exc)

    def _show_error(self, exc: Exception) -> None:
        self.state_label.setText(format_ads_error(exc))
        self.move_left_button.setEnabled(False)
        self.move_right_button.setEnabled(False)
        self.capture_left_button.setEnabled(False)
        self.capture_right_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _leave_calibration_mode(self) -> None:
        self.poll_timer.stop()
        if not self._calibration_mode_requested:
            return
        self._calibration_mode_requested = False
        try:
            self.ads.stop_calibration_move()
            self.ads.set_calibration_mode(False)
        except Exception:
            pass

    def done(self, result: int) -> None:
        self._leave_calibration_mode()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._leave_calibration_mode()
        super().closeEvent(event)


class PressureControlWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.ads = AdsClient()
        self.rows = [ArrayRow(index) for index in range(1, ARRAY_COUNT + 1)]
        self.last_shot_counter: int | None = None
        self.connect_thread: QThread | None = None
        self.connect_worker: AdsConnectWorker | None = None
        self.conveyor_calibration = {
            "marker_distance_mm": CALIBRATION_MARKER_DISTANCE_DEFAULT_MM,
            "mm_per_full_step": 0.0,
            "valid": False,
        }

        self.setWindowTitle("Nozzle Array Pressure Control")
        self.resize(1350, 440)

        self._build_ui()
        self._connect_signals()
        self.log_timer = QTimer(self)
        self.log_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.log_timer.setInterval(LOG_POLL_INTERVAL_MS)
        self.log_timer.timeout.connect(self.poll_pressure_log)
        self.log_timer.start()
        self.estimate_timer = QTimer(self)
        self.estimate_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.estimate_timer.setInterval(ESTIMATE_POLL_INTERVAL_MS)
        self.estimate_timer.timeout.connect(self.refresh_estimates)
        self.estimate_timer.start()
        self.reconnect()

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
        self.sensor_spacing_12.setValue(100.0)
        self.sensor_spacing_12.setToolTip("Physical distance between light barrier 1 and light barrier 2")
        machine_layout.addWidget(self.sensor_spacing_12)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Sensor spacing 3-4"))
        self.sensor_spacing_34 = QDoubleSpinBox()
        self.sensor_spacing_34.setRange(SENSOR_SPACING_MIN_MM, SENSOR_SPACING_MAX_MM)
        self.sensor_spacing_34.setSuffix(" mm")
        self.sensor_spacing_34.setDecimals(1)
        self.sensor_spacing_34.setSingleStep(1.0)
        self.sensor_spacing_34.setValue(100.0)
        self.sensor_spacing_34.setToolTip("Physical distance between light barrier 3 and light barrier 4")
        machine_layout.addWidget(self.sensor_spacing_34)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Sensor spacing 5-6"))
        self.sensor_spacing_56 = QDoubleSpinBox()
        self.sensor_spacing_56.setRange(SENSOR_SPACING_MIN_MM, SENSOR_SPACING_MAX_MM)
        self.sensor_spacing_56.setSuffix(" mm")
        self.sensor_spacing_56.setDecimals(1)
        self.sensor_spacing_56.setSingleStep(1.0)
        self.sensor_spacing_56.setValue(100.0)
        self.sensor_spacing_56.setToolTip("Physical distance between light barrier 5 and light barrier 6")
        machine_layout.addWidget(self.sensor_spacing_56)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Min travel time"))
        self.min_travel_time = QSpinBox()
        self.min_travel_time.setRange(TRAVEL_TIME_MIN_MS, TRAVEL_TIME_MAX_MS)
        self.min_travel_time.setSuffix(" ms")
        self.min_travel_time.setSingleStep(1)
        self.min_travel_time.setValue(20)
        self.min_travel_time.setToolTip("Clamp measured sensor travel times shorter than this value")
        machine_layout.addWidget(self.min_travel_time)
        machine_layout.addSpacing(20)
        machine_layout.addWidget(QLabel("Max travel time"))
        self.max_travel_time = QSpinBox()
        self.max_travel_time.setRange(TRAVEL_TIME_MIN_MS, TRAVEL_TIME_MAX_MS)
        self.max_travel_time.setSuffix(" ms")
        self.max_travel_time.setSingleStep(10)
        self.max_travel_time.setValue(2000)
        self.max_travel_time.setToolTip("Clamp measured sensor travel times longer than this value")
        machine_layout.addWidget(self.max_travel_time)
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
            "LB-to-Valve Delay",
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
            grid.addWidget(row.measured_valve_delay, row_number, 9)
            grid.addWidget(row.status, row_number, 10)

        grid.setColumnStretch(10, 1)
        main_layout.addWidget(control_box)

        button_layout = QHBoxLayout()
        self.reconnect_button = QPushButton("Reconnect")
        self.calibrate_conveyor_button = QPushButton("Calibrate Conveyor")
        self.calibrate_conveyor_button.setToolTip("Open the conveyor step calibration")
        self.logging_status = QLabel("Logging: offline")
        self.logging_status.setMinimumWidth(170)
        self.load_button = QPushButton("Load Profile")
        self.save_button = QPushButton("Save Profile")
        self.write_all_button = QPushButton("Write All Values")

        button_layout.addWidget(self.reconnect_button)
        button_layout.addWidget(self.calibrate_conveyor_button)
        button_layout.addWidget(self.logging_status)
        button_layout.addStretch(1)
        button_layout.addWidget(self.load_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.write_all_button)
        main_layout.addLayout(button_layout)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def _connect_signals(self) -> None:
        self.reconnect_button.clicked.connect(self.reconnect)
        self.calibrate_conveyor_button.clicked.connect(self.open_conveyor_calibration)
        self.load_button.clicked.connect(self.load_profile)
        self.save_button.clicked.connect(self.save_profile)
        self.write_all_button.clicked.connect(self.write_all_values)
        self.sensor_spacing_12.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing12Mm", value, "Sensor spacing 1-2")
        )
        self.sensor_spacing_34.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing34Mm", value, "Sensor spacing 3-4")
        )
        self.sensor_spacing_56.valueChanged.connect(
            lambda value: self.write_sensor_spacing("MAIN.GuiSensorSpacing56Mm", value, "Sensor spacing 5-6")
        )
        self.min_travel_time.valueChanged.connect(
            lambda value: self.write_travel_time_bound("MAIN.GuiMinTravelTimeMs", value, "Min travel time")
        )
        self.max_travel_time.valueChanged.connect(
            lambda value: self.write_travel_time_bound("MAIN.GuiMaxTravelTimeMs", value, "Max travel time")
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
        if self.connect_thread is not None and self.connect_thread.isRunning():
            return

        self.ads.close()
        self.last_shot_counter = None
        self.logging_status.setText("Logging: connecting")
        self.reconnect_button.setEnabled(False)
        self.statusBar().showMessage(f"Connecting to ADS controller ({ADS_TIMEOUT_MS} ms timeout)...")
        for row in self.rows:
            row.status.setText("connecting")

        self.connect_thread = QThread(self)
        self.connect_worker = AdsConnectWorker()
        self.connect_worker.moveToThread(self.connect_thread)
        self.connect_thread.started.connect(self.connect_worker.run)
        self.connect_worker.finished.connect(self.on_connect_finished)
        self.connect_worker.finished.connect(self.connect_thread.quit)
        self.connect_worker.finished.connect(self.connect_worker.deleteLater)
        self.connect_thread.finished.connect(self.connect_thread.deleteLater)
        self.connect_thread.finished.connect(self.on_connect_thread_finished)
        self.connect_thread.start()

    def on_connect_finished(self, client: object, error: str) -> None:
        if isinstance(client, AdsClient):
            self.ads = client
            self.statusBar().showMessage(f"ADS online: {AMS_NET_ID} / {PLC_IP}")
            self.reset_velocity_estimates()
            self.read_values_from_plc()
            self.initialize_pressure_logging()
        else:
            self.ads = AdsClient()
            self.statusBar().showMessage(f"ADS offline: {format_ads_error(RuntimeError(error))}")
            self.logging_status.setText("Logging: offline")
            self.last_shot_counter = None
            for row in self.rows:
                row.status.setText("offline")

        self.reconnect_button.setEnabled(True)

    def on_connect_thread_finished(self) -> None:
        self.connect_thread = None
        self.connect_worker = None

    def reset_velocity_estimates(self) -> None:
        self.estimate_timer.stop()
        for row in self.rows:
            row.estimated_velocity.setText("0.0 mm/s")
            row.estimated_delay.setText("0.0 ms")
            row.measured_valve_delay.setText("0.0 ms" if row.index <= 3 else "N/A")
            row.last_displayed_velocity = 0.0
            row.last_displayed_delay = 0.0
            row.last_displayed_valve_delay = 0.0

        try:
            self.ads.reset_velocity_estimates()
        except Exception as exc:
            self.statusBar().showMessage(f"Velocity reset: {format_ads_error(exc)}")

        QTimer.singleShot(500, self.estimate_timer.start)

    def read_values_from_plc(self) -> None:
        try:
            spacing_12, spacing_34, spacing_56 = self.ads.read_sensor_spacings()
            min_travel_time, max_travel_time = self.ads.read_travel_time_bounds()
            conveyor_settings = self.ads.read_conveyor_settings()
            calibration = self.ads.read_conveyor_calibration()
            self.conveyor_calibration = {
                "marker_distance_mm": float(calibration["marker_distance_mm"]),
                "mm_per_full_step": float(calibration["mm_per_full_step"]),
                "valid": bool(calibration["valid"]),
            }
            with (
                QSignalBlocker(self.sensor_spacing_12),
                QSignalBlocker(self.sensor_spacing_34),
                QSignalBlocker(self.sensor_spacing_56),
                QSignalBlocker(self.min_travel_time),
                QSignalBlocker(self.max_travel_time),
                QSignalBlocker(self.conveyor_enabled),
                QSignalBlocker(self.conveyor_reverse),
                QSignalBlocker(self.conveyor_speed),
                QSignalBlocker(self.conveyor_max_speed),
            ):
                self.sensor_spacing_12.setValue(spacing_12)
                self.sensor_spacing_34.setValue(spacing_34)
                self.sensor_spacing_56.setValue(spacing_56)
                self.min_travel_time.setValue(min_travel_time)
                self.max_travel_time.setValue(max_travel_time)
                self.conveyor_enabled.setChecked(bool(conveyor_settings["enabled"]))
                self.conveyor_reverse.setChecked(bool(conveyor_settings["reverse"]))
                self.conveyor_speed.setValue(float(conveyor_settings["speed_mm_per_sec"]))
                self.conveyor_max_speed.setValue(float(conveyor_settings["max_speed_mm_per_sec"]))
        except Exception as exc:
            self.statusBar().showMessage(format_ads_error(exc))

        for row in self.rows:
            try:
                row.set_values(self.ads.read_array(row.index))
                row.status.setText("read from PLC")
            except Exception as exc:
                row.status.setText(format_ads_error(exc))

    def write_value(self, row: ArrayRow, field: str, value: bool | int | float) -> None:
        try:
            self.ads.write_array_value(row.index, field, value)
            row.status.setText(f"written {datetime.now().strftime('%H:%M:%S')}")
            self.statusBar().showMessage(f"Array {row.index}: {field} written")
        except Exception as exc:
            message = format_ads_error(exc)
            row.status.setText(message)
            self.statusBar().showMessage(f"Array {row.index}: {message}")

    def write_all_values(self) -> None:
        self.write_sensor_spacing(
            "MAIN.GuiSensorSpacing12Mm",
            self.sensor_spacing_12.value(),
            "Sensor spacing 1-2",
        )
        self.write_sensor_spacing(
            "MAIN.GuiSensorSpacing34Mm",
            self.sensor_spacing_34.value(),
            "Sensor spacing 3-4",
        )
        self.write_sensor_spacing(
            "MAIN.GuiSensorSpacing56Mm",
            self.sensor_spacing_56.value(),
            "Sensor spacing 5-6",
        )
        self.write_travel_time_bound(
            "MAIN.GuiMinTravelTimeMs",
            self.min_travel_time.value(),
            "Min travel time",
        )
        self.write_travel_time_bound(
            "MAIN.GuiMaxTravelTimeMs",
            self.max_travel_time.value(),
            "Max travel time",
        )
        self.write_conveyor_setting("enabled", self.conveyor_enabled.isChecked())
        self.write_conveyor_setting("reverse", self.conveyor_reverse.isChecked())
        self.write_conveyor_setting("speed_mm_per_sec", self.conveyor_speed.value())
        self.write_conveyor_setting("max_speed_mm_per_sec", self.conveyor_max_speed.value())
        try:
            self.ads.write_conveyor_calibration(
                float(self.conveyor_calibration["marker_distance_mm"]),
                float(self.conveyor_calibration["mm_per_full_step"]),
                bool(self.conveyor_calibration["valid"]),
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Conveyor calibration: {format_ads_error(exc)}")
        for row in self.rows:
            values = row.values()
            self.write_value(row, "enabled", values["enabled"])
            for nozzle_index, enabled in enumerate(values["nozzles_enabled"], start=1):
                self.write_value(row, f"nozzle_{nozzle_index}_enabled", enabled)
            self.write_value(row, "pressure_mbar", values["pressure_mbar"])
            self.write_value(row, "delay_ms", values["delay_ms"])
            self.write_value(row, "pulse_duration_ms", values["pulse_duration_ms"])
            self.write_value(row, "offset_mm", values["offset_mm"])

    def write_sensor_spacing(self, symbol_name: str, value: float, label: str) -> None:
        try:
            self.ads.write_sensor_spacing(symbol_name, value)
            self.statusBar().showMessage(f"{label} written: {value:.1f} mm")
        except Exception as exc:
            self.statusBar().showMessage(f"{label}: {format_ads_error(exc)}")

    def write_travel_time_bound(self, symbol_name: str, value: int, label: str) -> None:
        if self.min_travel_time.value() > self.max_travel_time.value():
            if symbol_name == "MAIN.GuiMinTravelTimeMs":
                with QSignalBlocker(self.max_travel_time):
                    self.max_travel_time.setValue(value)
                self.write_travel_time_bound("MAIN.GuiMaxTravelTimeMs", value, "Max travel time")
            else:
                with QSignalBlocker(self.min_travel_time):
                    self.min_travel_time.setValue(value)
                self.write_travel_time_bound("MAIN.GuiMinTravelTimeMs", value, "Min travel time")

        try:
            self.ads.write_travel_time_bound(symbol_name, value)
            self.statusBar().showMessage(f"{label} written: {value} ms")
        except Exception as exc:
            self.statusBar().showMessage(f"{label}: {format_ads_error(exc)}")

    def write_conveyor_setting(self, field: str, value: bool | float) -> None:
        labels = {
            "enabled": "Conveyor enable",
            "reverse": "Conveyor reverse",
            "speed_mm_per_sec": "Conveyor speed",
            "max_speed_mm_per_sec": "Conveyor max speed",
            "reset": "Conveyor reset",
        }
        try:
            self.ads.write_conveyor_setting(field, value)
            label = labels.get(field, field)
            if isinstance(value, bool):
                self.statusBar().showMessage(f"{label} written: {'on' if value else 'off'}")
            else:
                self.statusBar().showMessage(f"{label} written: {value:.1f} mm/s")
        except Exception as exc:
            self.statusBar().showMessage(f"Conveyor: {format_ads_error(exc)}")

    def open_conveyor_calibration(self) -> None:
        if not self.ads.is_connected:
            self.statusBar().showMessage("Conveyor calibration: ADS offline")
            return

        with QSignalBlocker(self.conveyor_enabled):
            self.conveyor_enabled.setChecked(False)
        try:
            self.ads.write_conveyor_setting("enabled", False)
            dialog = ConveyorCalibrationDialog(self.ads, self)
            dialog.exec()
            calibration = self.ads.read_conveyor_calibration()
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
        except Exception as exc:
            self.statusBar().showMessage(f"Conveyor calibration: {format_ads_error(exc)}")

    def refresh_estimates(self) -> None:
        if not self.ads.is_connected:
            return

        try:
            velocities, delays, measured_valve_delays = self.ads.read_estimates()
            for row, velocity, delay, measured_valve_delay in zip(
                self.rows, velocities, delays, measured_valve_delays
            ):
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
                if measured_valve_delay is not None and (
                    row.last_displayed_valve_delay is None
                    or abs(measured_valve_delay - row.last_displayed_valve_delay)
                    > ESTIMATE_DISPLAY_EPSILON
                ):
                    row.measured_valve_delay.setText(f"{measured_valve_delay:.1f} ms")
                    row.last_displayed_valve_delay = measured_valve_delay
        except Exception as exc:
            message = format_ads_error(exc)
            for row in self.rows:
                row.estimated_velocity.setText(message)
                row.estimated_delay.setText(message)
                if row.index <= 3:
                    row.measured_valve_delay.setText(message)
                row.last_displayed_velocity = None
                row.last_displayed_delay = None
                row.last_displayed_valve_delay = None

    def initialize_pressure_logging(self) -> None:
        try:
            self.last_shot_counter = self.ads.read_shot_counter()
            self.logging_status.setText("Logging: waiting")
        except Exception as exc:
            self.last_shot_counter = None
            self.logging_status.setText(f"Logging: {format_ads_error(exc)}")

    def poll_pressure_log(self) -> None:
        if not self.ads.is_connected:
            return

        try:
            shot_counter = self.ads.read_shot_counter()
            if self.last_shot_counter is None:
                self.last_shot_counter = shot_counter
                self.logging_status.setText("Logging: waiting")
                return

            if shot_counter != self.last_shot_counter:
                avg_n1, avg_n2, velocity_1, velocity_2, velocity_3, velocity_4 = self.ads.read_pressure_log_values()
                self.append_pressure_log(avg_n1, avg_n2, velocity_1, velocity_2, velocity_3, velocity_4)
                self.last_shot_counter = shot_counter
                self.logging_status.setText(f"Logged shot {shot_counter}")
        except Exception as exc:
            self.logging_status.setText(f"Logging: {format_ads_error(exc)}")
            self.last_shot_counter = None
            self.ads.close()

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

        if self.ads.is_connected:
            try:
                calibration = self.ads.read_conveyor_calibration()
                self.conveyor_calibration = {
                    "marker_distance_mm": float(calibration["marker_distance_mm"]),
                    "mm_per_full_step": float(calibration["mm_per_full_step"]),
                    "valid": bool(calibration["valid"]),
                }
            except Exception:
                pass

        profile = {
            "version": PROFILE_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "sensor_spacing_12_mm": self.sensor_spacing_12.value(),
            "sensor_spacing_34_mm": self.sensor_spacing_34.value(),
            "sensor_spacing_56_mm": self.sensor_spacing_56.value(),
            "min_travel_time_ms": self.min_travel_time.value(),
            "max_travel_time_ms": self.max_travel_time.value(),
            "conveyor_enabled": self.conveyor_enabled.isChecked(),
            "conveyor_reverse": self.conveyor_reverse.isChecked(),
            "conveyor_speed_mm_per_sec": self.conveyor_speed.value(),
            "conveyor_max_speed_mm_per_sec": self.conveyor_max_speed.value(),
            "conveyor_calibration": self.conveyor_calibration.copy(),
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
            if profile_version not in {1, PROFILE_VERSION}:
                raise ValueError("Unknown profile version")

            spacing_12 = float(
                profile.get("sensor_spacing_12_mm", profile.get("sensor_spacing_mm", self.sensor_spacing_12.value()))
            )
            spacing_34 = float(
                profile.get("sensor_spacing_34_mm", profile.get("sensor_spacing_23_mm", self.sensor_spacing_34.value()))
            )
            spacing_56 = float(profile.get("sensor_spacing_56_mm", self.sensor_spacing_56.value()))
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
            min_travel_time = int(profile.get("min_travel_time_ms", self.min_travel_time.value()))
            max_travel_time = int(profile.get("max_travel_time_ms", self.max_travel_time.value()))
            if "min_travel_time_ms" not in profile and "max_velocity_mm_per_sec" in profile:
                max_velocity = float(profile["max_velocity_mm_per_sec"])
                if max_velocity > 0.0:
                    min_travel_time = int(round(spacing_12 * 1000.0 / max_velocity))
            if "max_travel_time_ms" not in profile and "min_velocity_mm_per_sec" in profile:
                min_velocity = float(profile["min_velocity_mm_per_sec"])
                if min_velocity > 0.0:
                    max_travel_time = int(round(spacing_12 * 1000.0 / min_velocity))
            with (
                QSignalBlocker(self.sensor_spacing_12),
                QSignalBlocker(self.sensor_spacing_34),
                QSignalBlocker(self.sensor_spacing_56),
                QSignalBlocker(self.min_travel_time),
                QSignalBlocker(self.max_travel_time),
                QSignalBlocker(self.conveyor_enabled),
                QSignalBlocker(self.conveyor_reverse),
                QSignalBlocker(self.conveyor_speed),
                QSignalBlocker(self.conveyor_max_speed),
            ):
                self.sensor_spacing_12.setValue(spacing_12)
                self.sensor_spacing_34.setValue(spacing_34)
                self.sensor_spacing_56.setValue(spacing_56)
                self.min_travel_time.setValue(min_travel_time)
                self.max_travel_time.setValue(max_travel_time)
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
        if self.connect_thread is not None and self.connect_thread.isRunning():
            self.connect_thread.quit()
            self.connect_thread.wait(ADS_TIMEOUT_MS + 500)
        self.ads.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = PressureControlWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
