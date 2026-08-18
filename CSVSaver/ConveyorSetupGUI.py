import csv
import math
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QSignalBlocker,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QStyle,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from read_ur_tcp_pose import read_tcp_pose_from_connection
from plc_control_lease import PlcControlLease

from PressureControlGUI import (
    ADS_TIMEOUT_MS,
    AMS_NET_ID,
    PLC_IP,
    LIGHT_BARRIER_COUNT,
    LIGHT_BARRIER_INVERT_DEFAULTS,
    LIGHT_BARRIER_PAIRS,
    AdsController,
    ConveyorCalibrationDialog,
    ConveyorJogDialog,
    calculate_conveyor_jog,
)


SENSOR_SPACING_SYMBOLS = {
    (1, 2): "MAIN.GuiSensorSpacing12Mm",
    (3, 4): "MAIN.GuiSensorSpacing34Mm",
    (5, 6): "MAIN.GuiSensorSpacing56Mm",
    (7, 8): "MAIN.GuiSensorSpacing78Mm",
}

ADJACENT_BARRIER_PAIRS = tuple(
    (sensor, sensor + 1) for sensor in range(1, LIGHT_BARRIER_COUNT)
)
DEFAULT_ADJACENT_SPACINGS_MM = (40.0, 196.0, 40.0, 196.0, 40.0, 196.0, 40.0)
CONSISTENCY_LOG_FILE = Path(__file__).resolve().parent / "light_barrier_consistency.csv"
CONSISTENCY_LOG_HEADER = [
    "local_timestamp",
    "part_number",
    "edge",
    *[f"distance_{first}_{second}_mm" for first, second in ADJACENT_BARRIER_PAIRS],
    *[f"plc_time_lb{sensor}_ms" for sensor in range(1, LIGHT_BARRIER_COUNT + 1)],
    *[f"speed_{first}_{second}_mm_per_sec" for first, second in ADJACENT_BARRIER_PAIRS],
    *[
        f"acceleration_after_lb{sensor}_mm_per_sec2"
        for sensor in range(2, LIGHT_BARRIER_COUNT)
    ],
    "maximum_speed_change_percent",
    "consistent",
    "diagnosis",
]

BARRIER_STATUS_TEXT = {
    0: "Ready",
    1: "Waiting for first light barrier",
    2: "Waiting for second light barrier",
    3: "Measurement complete",
    4: "Measurement cancelled or invalid",
    5: "EL7047 error",
}

UR_HOST = "10.10.10.10"
UR_PRIMARY_PORT = 30002
UR_POSE_TIMEOUT_SECONDS = 0.5
UR_RECONNECT_INTERVAL_SECONDS = 1.0
UR_SPEED_LOG_FILE = Path(__file__).resolve().parent / "ur_speed_plausibility.csv"
UR_SPEED_LOG_HEADER = [
    "local_timestamp",
    "target_speed_mm_per_sec",
    "first_barrier",
    "second_barrier",
    "sensor_spacing_mm",
    "edge",
    "direction",
    "start_plc_time_ms",
    "end_plc_time_ms",
    "elapsed_ms",
    "measured_speed_mm_per_sec",
    "error_percent",
]


def calculate_ur_pose_distance(
    first_pose: tuple[float, ...], second_pose: tuple[float, ...]
) -> tuple[float, tuple[float, float, float]]:
    if len(first_pose) < 3 or len(second_pose) < 3:
        raise ValueError("Both UR poses must contain X, Y and Z")
    delta_mm = tuple(
        (float(second_pose[index]) - float(first_pose[index])) * 1000.0
        for index in range(3)
    )
    distance_mm = math.sqrt(sum(component * component for component in delta_mm))
    return distance_mm, delta_mm


def calculate_speed_statistics(
    speeds_mm_per_sec: list[float], target_mm_per_sec: float
) -> dict[str, float]:
    if not speeds_mm_per_sec:
        return {}
    mean = sum(speeds_mm_per_sec) / len(speeds_mm_per_sec)
    variance = sum((speed - mean) ** 2 for speed in speeds_mm_per_sec) / len(
        speeds_mm_per_sec
    )
    return {
        "mean": mean,
        "standard_deviation": math.sqrt(variance),
        "minimum": min(speeds_mm_per_sec),
        "maximum": max(speeds_mm_per_sec),
        "mean_error_percent": (
            (mean - target_mm_per_sec) / target_mm_per_sec * 100.0
            if target_mm_per_sec > 0.0
            else 0.0
        ),
    }


def analyze_barrier_run(
    event_times_ms: list[int] | tuple[int, ...],
    spacings_mm: list[float] | tuple[float, ...],
    maximum_speed_change_percent: float,
) -> dict:
    """Calculate one chute traversal from eight PLC event timestamps."""
    if len(event_times_ms) != LIGHT_BARRIER_COUNT:
        raise ValueError(f"Exactly {LIGHT_BARRIER_COUNT} event timestamps are required")
    if len(spacings_mm) != LIGHT_BARRIER_COUNT - 1:
        raise ValueError(f"Exactly {LIGHT_BARRIER_COUNT - 1} spacings are required")
    if any(float(distance) <= 0.0 for distance in spacings_mm):
        raise ValueError("All light barrier spacings must be positive")
    if maximum_speed_change_percent <= 0.0:
        raise ValueError("Maximum speed change must be positive")

    elapsed_times_ms = []
    speeds = []
    for start, end, distance in zip(
        event_times_ms, event_times_ms[1:], spacings_mm
    ):
        elapsed_ms = (int(end) - int(start)) & 0xFFFFFFFF
        if elapsed_ms <= 0 or elapsed_ms > 60_000:
            raise ValueError("Barrier events are not a plausible ordered traversal")
        elapsed_times_ms.append(elapsed_ms)
        speeds.append(float(distance) * 1000.0 / elapsed_ms)

    positions = [0.0]
    for distance in spacings_mm:
        positions.append(positions[-1] + float(distance))
    segment_centres = [
        (start + end) / 2.0 for start, end in zip(positions, positions[1:])
    ]
    accelerations = []
    speed_changes_percent = []
    diagnoses = []
    for index, (first_speed, second_speed) in enumerate(zip(speeds, speeds[1:])):
        centre_distance = segment_centres[index + 1] - segment_centres[index]
        accelerations.append(
            (second_speed**2 - first_speed**2) / (2.0 * centre_distance)
        )
        change_percent = (
            abs(second_speed - first_speed) / max(first_speed, second_speed) * 100.0
        )
        speed_changes_percent.append(change_percent)
        if change_percent > maximum_speed_change_percent:
            first_pair = ADJACENT_BARRIER_PAIRS[index]
            second_pair = ADJACENT_BARRIER_PAIRS[index + 1]
            diagnoses.append(
                f"LB {first_pair[0]}-{first_pair[1]} -> "
                f"LB {second_pair[0]}-{second_pair[1]}: "
                f"speed change {change_percent:.1f}%"
            )

    return {
        "elapsed_times_ms": elapsed_times_ms,
        "speeds": speeds,
        "positions": positions,
        "segment_centres": segment_centres,
        "accelerations": accelerations,
        "speed_changes_percent": speed_changes_percent,
        "maximum_speed_change_percent": max(speed_changes_percent, default=0.0),
        "consistent": not diagnoses,
        "diagnoses": diagnoses,
    }


class BarrierRunCollector:
    """Join individual PLC barrier events into forward LB1-to-LB8 traversals."""

    def __init__(self, edge_state: bool) -> None:
        self.edge_state = bool(edge_state)
        self.event_counts = None
        self.active_runs: list[list[int]] = []

    def start(self, event_counts: list[int] | tuple[int, ...]) -> None:
        if len(event_counts) != LIGHT_BARRIER_COUNT:
            raise ValueError("An event count is required for every light barrier")
        self.event_counts = [int(value) for value in event_counts]
        self.active_runs = []

    def process(self, status: dict) -> tuple[list[tuple[int, ...]], bool]:
        counts = status.get("light_barrier_event_counts")
        event_times = status.get("light_barrier_event_times_ms")
        states = status.get("light_barriers")
        if (
            self.event_counts is None
            or counts is None
            or event_times is None
            or states is None
        ):
            return [], False

        events = []
        missed_event = False
        for index in range(LIGHT_BARRIER_COUNT):
            current_count = int(counts[index])
            event_delta = (current_count - self.event_counts[index]) & 0xFFFFFFFF
            self.event_counts[index] = current_count
            if event_delta == 1:
                events.append((index + 1, int(event_times[index]), bool(states[index])))
            elif event_delta > 1:
                missed_event = True

        if missed_event:
            self.active_runs = []

        completed = []
        for sensor, event_time, edge_state in events:
            if edge_state != self.edge_state:
                continue
            if sensor == 1:
                self.active_runs.append([event_time])
                continue
            matching_run = next(
                (run for run in self.active_runs if len(run) + 1 == sensor), None
            )
            if matching_run is None:
                continue
            matching_run.append(event_time)
            if sensor == LIGHT_BARRIER_COUNT:
                completed.append(tuple(matching_run))
                self.active_runs.remove(matching_run)
        return completed, missed_event


class SpeedCurveWidget(QWidget):
    COLORS = (
        "#2563eb",
        "#16a34a",
        "#dc2626",
        "#9333ea",
        "#ea580c",
        "#0891b2",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.samples = []
        self.setMinimumSize(760, 440)

    def set_samples(self, samples: list[dict]) -> None:
        self.samples = list(samples)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        left, top, right, bottom = 70, 30, 25, 60
        plot_width = max(1, self.width() - left - right)
        plot_height = max(1, self.height() - top - bottom)
        painter.setPen(QPen(QColor("#374151"), 1))
        painter.drawLine(left, top, left, top + plot_height)
        painter.drawLine(
            left, top + plot_height, left + plot_width, top + plot_height
        )
        painter.drawText(8, top + plot_height // 2, "mm/s")
        painter.drawText(
            left + plot_width // 2 - 45, self.height() - 14, "Position [mm]"
        )
        if not self.samples:
            painter.drawText(left + 20, top + 35, "No completed part traversals yet")
            return

        all_x = [x for sample in self.samples for x in sample["segment_centres"]]
        all_y = [speed for sample in self.samples for speed in sample["speeds"]]
        x_max = max(all_x, default=1.0)
        y_max = max(all_y, default=1.0) * 1.1
        painter.setPen(QPen(QColor("#9ca3af"), 1))
        for step in range(6):
            y_value = y_max * step / 5.0
            py = top + plot_height - round(y_value / y_max * plot_height)
            painter.drawLine(left - 4, py, left + plot_width, py)
            painter.drawText(10, py + 4, f"{y_value:.0f}")
        for step in range(6):
            x_value = x_max * step / 5.0
            px = left + round(x_value / x_max * plot_width)
            painter.drawText(px - 16, top + plot_height + 20, f"{x_value:.0f}")

        for index, sample in enumerate(self.samples):
            color = QColor(self.COLORS[index % len(self.COLORS)])
            if not sample["consistent"]:
                color = QColor("#dc2626")
            painter.setPen(QPen(color, 2))
            points = []
            for x_value, y_value in zip(sample["segment_centres"], sample["speeds"]):
                px = left + round(x_value / x_max * plot_width)
                py = top + plot_height - round(y_value / y_max * plot_height)
                points.append((px, py))
            for first, second in zip(points, points[1:]):
                painter.drawLine(first[0], first[1], second[0], second[1])
            for px, py in points:
                painter.drawEllipse(px - 3, py - 3, 6, 6)


class SpeedPlotDialog(QDialog):
    def __init__(self, samples: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Light Barrier Speed Curves")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.curve = SpeedCurveWidget()
        layout.addWidget(self.summary)
        layout.addWidget(self.curve, 1)
        self.set_samples(samples)

    def set_samples(self, samples: list[dict]) -> None:
        inconsistent = sum(not sample["consistent"] for sample in samples)
        self.summary.setText(
            f"{len(samples)} parts; {inconsistent} with abrupt speed changes. "
            "Red curves are flagged."
        )
        self.curve.set_samples(samples)


class UrPoseWorker(QObject):
    pose_ready = pyqtSignal(object)
    connection_changed = pyqtSignal(bool, str)
    finished = pyqtSignal()

    def __init__(self, host: str = UR_HOST, port: int = UR_PRIMARY_PORT) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.timer = None
        self.connection = None
        self.connected = False
        self.stopping = False
        self.next_connect_attempt = 0.0

    @pyqtSlot()
    def start(self) -> None:
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()

    def _set_connected(self, connected: bool, message: str = "") -> None:
        if connected != self.connected or message:
            self.connected = connected
            self.connection_changed.emit(connected, message)

    def _close_connection(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    @pyqtSlot()
    def poll(self) -> None:
        if self.stopping:
            return
        if self.connection is None and time.monotonic() < self.next_connect_attempt:
            return
        try:
            if self.connection is None:
                self.connection = socket.create_connection(
                    (self.host, self.port), timeout=UR_POSE_TIMEOUT_SECONDS
                )
            pose = read_tcp_pose_from_connection(
                self.connection, UR_POSE_TIMEOUT_SECONDS
            )
            self.next_connect_attempt = 0.0
            self._set_connected(True)
            self.pose_ready.emit((time.monotonic(), pose))
        except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
            self._close_connection()
            self.next_connect_attempt = time.monotonic() + UR_RECONNECT_INTERVAL_SECONDS
            self._set_connected(False, str(exc))

    @pyqtSlot()
    def stop(self) -> None:
        self.stopping = True
        if self.timer is not None:
            self.timer.stop()
        self._close_connection()
        self.finished.emit()


def calculate_velocity_plausibility(
    distance_mm: float,
    travel_time_ms: int,
    target_speed_mm_per_sec: float,
    tolerance_percent: float,
) -> dict:
    if distance_mm <= 0.0 or travel_time_ms <= 0 or target_speed_mm_per_sec <= 0.0:
        raise ValueError("Distance, travel time and target speed must be positive")
    measured_speed = distance_mm * 1000.0 / travel_time_ms
    difference = measured_speed - target_speed_mm_per_sec
    deviation_percent = abs(difference) / target_speed_mm_per_sec * 100.0
    return {
        "measured_speed": measured_speed,
        "difference": difference,
        "deviation_percent": deviation_percent,
        "plausible": deviation_percent <= tolerance_percent,
    }


class VelocityPlausibilityDialog(QDialog):
    def __init__(self, ads: AdsController, parent=None) -> None:
        super().__init__(parent)
        self.ads = ads
        self.running = False
        self.saw_invalid_measurement = False
        self.target_speed = 0.0
        self.latest_status = None

        self.setWindowTitle("Conveyor Speed Plausibility Check")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        settings = QGroupBox("Test Settings")
        settings_form = QFormLayout(settings)
        self.sensor_pair = QComboBox()
        self.sensor_pair.addItem("Light barriers 1-2", 0)
        self.sensor_pair.addItem("Light barriers 3-4", 1)
        self.sensor_pair.addItem("Light barriers 5-6", 2)
        self.target_speed_input = QDoubleSpinBox()
        self.target_speed_input.setRange(0.1, 500.0)
        self.target_speed_input.setDecimals(2)
        self.target_speed_input.setSuffix(" mm/s")
        self.target_speed_input.setValue(10.0)
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setRange(0.1, 100.0)
        self.tolerance_input.setDecimals(1)
        self.tolerance_input.setSuffix(" %")
        self.tolerance_input.setValue(10.0)
        self.distance_label = QLabel("-")
        settings_form.addRow("Sensor pair", self.sensor_pair)
        settings_form.addRow("Target conveyor speed", self.target_speed_input)
        settings_form.addRow("Allowed deviation", self.tolerance_input)
        settings_form.addRow("Calibrated spacing", self.distance_label)
        layout.addWidget(settings)

        results = QGroupBox("Measurement")
        results_form = QFormLayout(results)
        self.travel_time_label = QLabel("-")
        self.measured_speed_label = QLabel("-")
        self.difference_label = QLabel("-")
        self.deviation_label = QLabel("-")
        self.verdict_label = QLabel("Waiting")
        self.run_state_label = QLabel("Stopped")
        results_form.addRow("Barrier travel time", self.travel_time_label)
        results_form.addRow("Measured speed", self.measured_speed_label)
        results_form.addRow("Difference", self.difference_label)
        results_form.addRow("Deviation", self.deviation_label)
        results_form.addRow("Result", self.verdict_label)
        results_form.addRow("Conveyor", self.run_state_label)
        layout.addWidget(results)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start Constant Speed")
        self.start_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.stop_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        self.close_button.clicked.connect(self.accept)
        self.sensor_pair.currentIndexChanged.connect(self._refresh_distance)
        self.ads.setup_status_ready.connect(self._on_status)

    def _pair_index(self) -> int:
        return int(self.sensor_pair.currentData())

    def _refresh_distance(self) -> None:
        if self.latest_status:
            distance = float(self.latest_status["sensor_spacings"][self._pair_index()])
            self.distance_label.setText(f"{distance:.3f} mm")

    def _start(self) -> None:
        self.target_speed = self.target_speed_input.value()
        self.saw_invalid_measurement = False
        self.travel_time_label.setText("-")
        self.measured_speed_label.setText("-")
        self.difference_label.setText("-")
        self.deviation_label.setText("-")
        self.verdict_label.setText("Waiting for a fresh barrier measurement")
        self.run_state_label.setText("Starting")
        self.running = True
        self.sensor_pair.setEnabled(False)
        self.target_speed_input.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.ads.start_velocity_check(self.target_speed)

    def _stop(self) -> None:
        if self.running:
            self.ads.stop_setup_motion()
        self.running = False
        self.run_state_label.setText("Stopped")
        self.sensor_pair.setEnabled(True)
        self.target_speed_input.setEnabled(True)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    @pyqtSlot(object)
    def _on_status(self, status: dict) -> None:
        self.latest_status = status
        self._refresh_distance()
        if not self.running:
            return
        pair_index = self._pair_index()
        valid = bool(status["velocity_valid"][pair_index])
        if not valid:
            self.saw_invalid_measurement = True
            self.run_state_label.setText("Running - place the rod on the conveyor")
            return
        if not self.saw_invalid_measurement:
            return

        travel_time_ms = int(status["velocity_times_ms"][pair_index])
        distance_mm = float(status["sensor_spacings"][pair_index])
        try:
            result = calculate_velocity_plausibility(
                distance_mm,
                travel_time_ms,
                self.target_speed,
                self.tolerance_input.value(),
            )
        except ValueError:
            return
        self.saw_invalid_measurement = False
        self.travel_time_label.setText(f"{travel_time_ms} ms")
        self.measured_speed_label.setText(f'{result["measured_speed"]:.3f} mm/s')
        self.difference_label.setText(f'{result["difference"]:+.3f} mm/s')
        self.deviation_label.setText(f'{result["deviation_percent"]:.2f} %')
        if result["plausible"]:
            self.verdict_label.setText("Plausible")
            self.verdict_label.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.verdict_label.setText("Outside tolerance")
            self.verdict_label.setStyleSheet("color: #b91c1c; font-weight: 600;")
        self.run_state_label.setText("Running - stop when finished")

    def closeEvent(self, event) -> None:
        self._stop()
        super().closeEvent(event)


class ConveyorSetupWindow(QMainWindow):
    ur_stop_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.ads = AdsController(self)
        self.connected = False
        self.have_setup_status = False
        self.latest_status = None
        self.mm_per_full_step = 0.0
        self.pending_measurement = None
        self.debounce_initialized = False
        self.ur_connected = False
        self.latest_ur_pose = None
        self.ur_capture_active = False
        self.ur_first_initial_state = False
        self.ur_second_initial_state = False
        self.ur_first_pose = None
        self.ur_second_pose = None
        self.ur_distance_mm = None
        self.ur_monitor_active = False
        self.ur_monitor_event_counts = None
        self.ur_monitor_pending_edges = {False: None, True: None}
        self.ur_monitor_samples = []
        self.consistency_monitor_active = False
        self.consistency_collector = None
        self.consistency_samples = []
        self.consistency_session_spacings = None
        self.consistency_spacings_initialized = False
        self.consistency_plot_dialog = None

        self.setWindowTitle("Conveyor and Light Barrier Setup")
        self.resize(900, 790)
        self._build_ui()
        self._connect_signals()
        self._set_controls_enabled(False)

        self.statusBar().showMessage(
            f"Connecting to ADS controller ({ADS_TIMEOUT_MS} ms timeout)..."
        )
        self.ads.start()
        self.ads.set_setup_polling(True)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        conveyor_group = QGroupBox("Conveyor")
        conveyor_layout = QGridLayout(conveyor_group)
        self.calibration_factor = QLabel("Not calibrated")
        self.speed_conversion = QLabel("-")
        self.calibrate_button = QPushButton("Calibrate Conveyor")
        self.jog_button = QPushButton("Jog Conveyor")
        self.plausibility_button = QPushButton("Plausibility Check")
        self.calibrate_button.setToolTip("Calibrate conveyor travel per motor full step")
        self.jog_button.setToolTip("Move the conveyor by a calibrated distance")
        conveyor_layout.addWidget(QLabel("Calibration"), 0, 0)
        conveyor_layout.addWidget(self.calibration_factor, 0, 1)
        conveyor_layout.addWidget(QLabel("Current speed conversion"), 1, 0)
        conveyor_layout.addWidget(self.speed_conversion, 1, 1)
        conveyor_layout.addWidget(self.calibrate_button, 0, 2)
        conveyor_layout.addWidget(self.jog_button, 1, 2)
        conveyor_layout.addWidget(self.plausibility_button, 2, 2)
        conveyor_layout.setColumnStretch(1, 1)
        layout.addWidget(conveyor_group)

        barrier_group = QGroupBox("Light Barriers")
        barrier_layout = QGridLayout(barrier_group)
        self.barrier_lamps = []
        self.barrier_labels = []
        self.barrier_inverted = []
        for index in range(1, LIGHT_BARRIER_COUNT + 1):
            column = index - 1
            title = QLabel(f"LB {index}")
            title.setStyleSheet("font-weight: 600;")
            lamp = QLabel()
            lamp.setFixedSize(18, 18)
            state = QLabel("Unknown")
            state.setMinimumWidth(58)
            inverted = QCheckBox("Invert")
            inverted.setChecked(LIGHT_BARRIER_INVERT_DEFAULTS[index - 1])
            inverted.setToolTip(
                f"Invert the electrical signal of light barrier {index} before debouncing"
            )
            barrier_layout.addWidget(title, 0, column)
            barrier_layout.addWidget(lamp, 1, column)
            barrier_layout.addWidget(state, 2, column)
            barrier_layout.addWidget(inverted, 3, column)
            self.barrier_lamps.append(lamp)
            self.barrier_labels.append(state)
            self.barrier_inverted.append(inverted)
            self._set_barrier_indicator(index - 1, None)
        barrier_layout.setColumnStretch(LIGHT_BARRIER_COUNT, 1)
        layout.addWidget(barrier_group)

        measurement_group = QGroupBox("Light Barrier Distance Calibration")
        measurement_layout = QGridLayout(measurement_group)
        form = QFormLayout()
        self.first_sensor = QComboBox()
        self.second_sensor = QComboBox()
        for index in range(1, LIGHT_BARRIER_COUNT + 1):
            self.first_sensor.addItem(f"Light barrier {index}", index)
            self.second_sensor.addItem(f"Light barrier {index}", index)
        self.second_sensor.setCurrentIndex(1)
        form.addRow("First barrier", self.first_sensor)
        form.addRow("Second barrier", self.second_sensor)

        self.measurement_speed = QDoubleSpinBox()
        self.measurement_speed.setRange(0.1, 5000.0)
        self.measurement_speed.setDecimals(2)
        self.measurement_speed.setSingleStep(1.0)
        self.measurement_speed.setSuffix(" mm/s")
        self.measurement_speed.setValue(10.0)
        form.addRow("Travel speed", self.measurement_speed)

        self.maximum_travel = QDoubleSpinBox()
        self.maximum_travel.setRange(1.0, 10000.0)
        self.maximum_travel.setDecimals(1)
        self.maximum_travel.setSingleStep(10.0)
        self.maximum_travel.setSuffix(" mm")
        self.maximum_travel.setValue(1000.0)
        form.addRow("Maximum travel", self.maximum_travel)

        self.debounce_time = QSpinBox()
        self.debounce_time.setRange(1, 200)
        self.debounce_time.setSuffix(" ms")
        self.debounce_time.setValue(20)
        self.debounce_time.setToolTip(
            "Required stable signal time before a light barrier transition is accepted"
        )
        form.addRow("Signal stable time", self.debounce_time)
        measurement_layout.addLayout(form, 0, 0, 2, 1)

        result_form = QFormLayout()
        self.internal_position = QLabel("-")
        self.measured_pair = QLabel("-")
        self.first_position = QLabel("Not captured")
        self.second_position = QLabel("Not captured")
        self.position_difference = QLabel("-")
        self.measured_distance = QLabel("-")
        self.measurement_state = QLabel("Waiting for PLC")
        result_form.addRow("Current position", self.internal_position)
        result_form.addRow("Measured pair", self.measured_pair)
        result_form.addRow("First position", self.first_position)
        result_form.addRow("Second position", self.second_position)
        result_form.addRow("Position difference", self.position_difference)
        result_form.addRow("Measured distance", self.measured_distance)
        result_form.addRow("State", self.measurement_state)
        measurement_layout.addLayout(result_form, 0, 1, 2, 1)

        button_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Right")
        self.start_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        )
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.apply_button = QPushButton("Apply Sensor Spacing")
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.apply_button)
        measurement_layout.addLayout(button_layout, 2, 0, 1, 2)
        measurement_layout.setColumnStretch(0, 1)
        measurement_layout.setColumnStretch(1, 1)
        self.calibration_tabs = QTabWidget()
        self.calibration_tabs.addTab(measurement_group, "Conveyor")

        ur_group = QWidget()
        ur_layout = QGridLayout(ur_group)
        ur_settings = QFormLayout()
        self.ur_first_sensor = QComboBox()
        self.ur_second_sensor = QComboBox()
        for index in range(1, LIGHT_BARRIER_COUNT + 1):
            self.ur_first_sensor.addItem(f"Light barrier {index}", index)
            self.ur_second_sensor.addItem(f"Light barrier {index}", index)
        self.ur_second_sensor.setCurrentIndex(1)
        self.ur_connection_label = QLabel(f"Connecting to {UR_HOST}")
        self.ur_live_pose = QLabel("-")
        self.ur_live_pose.setMinimumWidth(330)
        ur_settings.addRow("UR controller", self.ur_connection_label)
        ur_settings.addRow("Live TCP position", self.ur_live_pose)
        ur_settings.addRow("First barrier", self.ur_first_sensor)
        ur_settings.addRow("Second barrier", self.ur_second_sensor)
        ur_layout.addLayout(ur_settings, 0, 0)

        ur_results = QFormLayout()
        self.ur_first_pose_label = QLabel("Not captured")
        self.ur_second_pose_label = QLabel("Not captured")
        self.ur_delta_label = QLabel("-")
        self.ur_distance_label = QLabel("-")
        self.ur_state_label = QLabel("Waiting for UR and PLC")
        ur_results.addRow("First TCP position", self.ur_first_pose_label)
        ur_results.addRow("Second TCP position", self.ur_second_pose_label)
        ur_results.addRow("Position delta", self.ur_delta_label)
        ur_results.addRow("Measured spacing", self.ur_distance_label)
        ur_results.addRow("State", self.ur_state_label)
        ur_layout.addLayout(ur_results, 0, 1)

        ur_buttons = QHBoxLayout()
        self.ur_start_button = QPushButton("Start UR Capture")
        self.ur_start_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.ur_cancel_button = QPushButton("Cancel")
        self.ur_cancel_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.ur_apply_button = QPushButton("Apply Sensor Spacing")
        ur_buttons.addWidget(self.ur_start_button)
        ur_buttons.addWidget(self.ur_cancel_button)
        ur_buttons.addStretch(1)
        ur_buttons.addWidget(self.ur_apply_button)
        ur_layout.addLayout(ur_buttons, 1, 0, 1, 2)

        monitor_group = QGroupBox("UR Speed Plausibility")
        monitor_layout = QGridLayout(monitor_group)
        monitor_settings = QFormLayout()
        self.ur_target_speed = QDoubleSpinBox()
        self.ur_target_speed.setRange(0.1, 1000.0)
        self.ur_target_speed.setDecimals(3)
        self.ur_target_speed.setValue(15.0)
        self.ur_target_speed.setSuffix(" mm/s")
        self.ur_monitor_distance = QLabel("-")
        monitor_settings.addRow("UR target speed", self.ur_target_speed)
        monitor_settings.addRow("Sensor spacing", self.ur_monitor_distance)
        monitor_layout.addLayout(monitor_settings, 0, 0)

        monitor_results = QFormLayout()
        self.ur_monitor_samples_label = QLabel("0")
        self.ur_monitor_latest_label = QLabel("-")
        self.ur_monitor_mean_label = QLabel("-")
        self.ur_monitor_range_label = QLabel("-")
        self.ur_monitor_directions_label = QLabel("-")
        self.ur_monitor_edges_label = QLabel("-")
        self.ur_monitor_state_label = QLabel("Ready")
        monitor_results.addRow("Samples", self.ur_monitor_samples_label)
        monitor_results.addRow("Latest", self.ur_monitor_latest_label)
        monitor_results.addRow("Mean and deviation", self.ur_monitor_mean_label)
        monitor_results.addRow("Range", self.ur_monitor_range_label)
        monitor_results.addRow("Directions", self.ur_monitor_directions_label)
        monitor_results.addRow("Edges", self.ur_monitor_edges_label)
        monitor_results.addRow("State", self.ur_monitor_state_label)
        monitor_layout.addLayout(monitor_results, 0, 1)

        monitor_buttons = QHBoxLayout()
        self.ur_monitor_start_button = QPushButton("Start New Test")
        self.ur_monitor_start_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.ur_monitor_stop_button = QPushButton("Stop")
        self.ur_monitor_stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        monitor_buttons.addWidget(self.ur_monitor_start_button)
        monitor_buttons.addWidget(self.ur_monitor_stop_button)
        monitor_buttons.addStretch(1)
        monitor_layout.addLayout(monitor_buttons, 1, 0, 1, 2)
        monitor_layout.setColumnStretch(0, 1)
        monitor_layout.setColumnStretch(1, 1)
        ur_layout.addWidget(monitor_group, 2, 0, 1, 2)
        ur_layout.setColumnStretch(0, 1)
        ur_layout.setColumnStretch(1, 1)
        self.calibration_tabs.addTab(ur_group, "UR TCP")

        consistency_tab = QWidget()
        consistency_layout = QVBoxLayout(consistency_tab)
        consistency_layout.setContentsMargins(8, 8, 8, 8)

        distances_group = QGroupBox("Adjacent Light Barrier Distances")
        distances_layout = QGridLayout(distances_group)
        self.consistency_spacing_inputs = []
        for index, (pair, default_distance) in enumerate(
            zip(ADJACENT_BARRIER_PAIRS, DEFAULT_ADJACENT_SPACINGS_MM)
        ):
            distance_input = QDoubleSpinBox()
            distance_input.setRange(0.1, 2000.0)
            distance_input.setDecimals(3)
            distance_input.setSuffix(" mm")
            distance_input.setValue(default_distance)
            distance_input.setToolTip(
                "Loaded from the PLC"
                if pair in SENSOR_SPACING_SYMBOLS
                else "Editable assumed distance; initially 196 mm"
            )
            row = index // 4
            column = (index % 4) * 2
            distances_layout.addWidget(
                QLabel(f"LB {pair[0]}-{pair[1]}"), row, column
            )
            distances_layout.addWidget(distance_input, row, column + 1)
            self.consistency_spacing_inputs.append(distance_input)
        self.consistency_reload_distances_button = QPushButton(
            "Reload 1-2 / 3-4 / 5-6 / 7-8 from PLC"
        )
        distances_layout.addWidget(
            self.consistency_reload_distances_button, 2, 0, 1, 8
        )
        consistency_layout.addWidget(distances_group)

        controls_group = QGroupBox("Traversal Logging and Consistency Check")
        controls_layout = QGridLayout(controls_group)
        self.consistency_edge = QComboBox()
        self.consistency_edge.addItem("ON transition", True)
        self.consistency_edge.addItem("OFF transition", False)
        self.consistency_tolerance = QDoubleSpinBox()
        self.consistency_tolerance.setRange(1.0, 100.0)
        self.consistency_tolerance.setDecimals(1)
        self.consistency_tolerance.setSuffix(" %")
        self.consistency_tolerance.setValue(50.0)
        self.consistency_tolerance.setToolTip(
            "Maximum allowed speed change between two neighboring chute sections"
        )
        self.consistency_start_button = QPushButton("Start New Log")
        self.consistency_start_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.consistency_stop_button = QPushButton("Stop")
        self.consistency_stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.consistency_clear_button = QPushButton("Clear Table")
        self.consistency_plot_button = QPushButton("Open Speed Curves")
        self.consistency_state_label = QLabel("Ready")
        controls_layout.addWidget(QLabel("Recorded edge"), 0, 0)
        controls_layout.addWidget(self.consistency_edge, 0, 1)
        controls_layout.addWidget(QLabel("Maximum adjacent speed change"), 0, 2)
        controls_layout.addWidget(self.consistency_tolerance, 0, 3)
        controls_layout.addWidget(self.consistency_start_button, 1, 0)
        controls_layout.addWidget(self.consistency_stop_button, 1, 1)
        controls_layout.addWidget(self.consistency_clear_button, 1, 2)
        controls_layout.addWidget(self.consistency_plot_button, 1, 3)
        controls_layout.addWidget(QLabel("State"), 2, 0)
        controls_layout.addWidget(self.consistency_state_label, 2, 1, 1, 3)
        controls_layout.setColumnStretch(3, 1)
        consistency_layout.addWidget(controls_group)

        self.consistency_table = QTableWidget(0, 11)
        self.consistency_table.setHorizontalHeaderLabels(
            [
                "Part",
                "Timestamp",
                *[f"LB {first}-{second}" for first, second in ADJACENT_BARRIER_PAIRS],
                "Max dV",
                "Result",
            ]
        )
        self.consistency_table.setAlternatingRowColors(True)
        self.consistency_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.consistency_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.consistency_table.horizontalHeader().setStretchLastSection(True)
        consistency_layout.addWidget(self.consistency_table, 1)
        self.consistency_summary_label = QLabel("No completed parts")
        consistency_layout.addWidget(self.consistency_summary_label)
        self.calibration_tabs.addTab(consistency_tab, "Consistency")
        layout.addWidget(self.calibration_tabs)

        spacing_group = QGroupBox("Velocity Sensor Spacings")
        spacing_layout = QHBoxLayout(spacing_group)
        self.spacing_labels = [QLabel("-") for _ in LIGHT_BARRIER_PAIRS]
        for pair, label in zip(LIGHT_BARRIER_PAIRS, self.spacing_labels):
            spacing_layout.addWidget(QLabel(f"LB {pair[0]}-{pair[1]}"))
            spacing_layout.addWidget(label)
            spacing_layout.addSpacing(18)
        spacing_layout.addStretch(1)
        layout.addWidget(spacing_group)
        layout.addStretch(1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def _connect_signals(self) -> None:
        self.ads.connection_changed.connect(self._on_connection_changed)
        self.ads.initial_snapshot_ready.connect(self._on_initial_snapshot)
        self.ads.setup_status_ready.connect(self._on_setup_status)
        self.ads.operation_failed.connect(self._on_ads_error)
        self.ads.write_finished.connect(self._on_write_finished)
        self.calibrate_button.clicked.connect(self._open_conveyor_calibration)
        self.jog_button.clicked.connect(self._open_conveyor_jogging)
        self.plausibility_button.clicked.connect(self._open_plausibility_check)
        self.start_button.clicked.connect(self._start_measurement)
        self.stop_button.clicked.connect(self._stop_measurement)
        self.apply_button.clicked.connect(self._apply_measurement)
        self.first_sensor.currentIndexChanged.connect(self._update_apply_state)
        self.second_sensor.currentIndexChanged.connect(self._update_apply_state)
        self.calibration_tabs.currentChanged.connect(self._on_calibration_tab_changed)
        self.ur_start_button.clicked.connect(self._start_ur_capture)
        self.ur_cancel_button.clicked.connect(self._cancel_ur_capture)
        self.ur_apply_button.clicked.connect(self._apply_ur_measurement)
        self.ur_monitor_start_button.clicked.connect(self._start_ur_speed_monitor)
        self.ur_monitor_stop_button.clicked.connect(
            lambda: self._stop_ur_speed_monitor()
        )
        self.consistency_start_button.clicked.connect(self._start_consistency_monitor)
        self.consistency_stop_button.clicked.connect(
            lambda: self._stop_consistency_monitor()
        )
        self.consistency_clear_button.clicked.connect(
            self._clear_consistency_samples
        )
        self.consistency_plot_button.clicked.connect(self._open_consistency_plot)
        self.consistency_reload_distances_button.clicked.connect(
            self._reload_consistency_distances
        )
        self.ur_first_sensor.currentIndexChanged.connect(self._update_ur_controls)
        self.ur_second_sensor.currentIndexChanged.connect(self._update_ur_controls)
        for index, checkbox in enumerate(self.barrier_inverted, start=1):
            checkbox.stateChanged.connect(
                lambda _state, sensor=index: self._write_barrier_inversion(sensor)
            )

    def _write_barrier_inversion(self, sensor: int) -> None:
        if not self.connected:
            return
        inverted = self.barrier_inverted[sensor - 1].isChecked()
        self.ads.write_now(
            {f"MAIN.GuiLightBarrierInvert{sensor}": inverted},
            f"light_barrier_{sensor}_inversion",
        )

    def _on_calibration_tab_changed(self, index: int) -> None:
        if index == 1:
            self._start_ur_worker()

    def _start_ur_worker(self) -> None:
        if hasattr(self, "ur_thread") and self.ur_thread.isRunning():
            return
        self.ur_thread = QThread(self)
        self.ur_worker = UrPoseWorker()
        self.ur_worker.moveToThread(self.ur_thread)
        self.ur_thread.started.connect(self.ur_worker.start)
        self.ur_stop_requested.connect(self.ur_worker.stop)
        self.ur_worker.pose_ready.connect(self._on_ur_pose)
        self.ur_worker.connection_changed.connect(self._on_ur_connection_changed)
        self.ur_worker.finished.connect(self.ur_thread.quit)
        self.ur_thread.start()

    @staticmethod
    def _format_ur_pose(pose: tuple[float, ...]) -> str:
        return (
            f"X {pose[0] * 1000.0:.3f}, Y {pose[1] * 1000.0:.3f}, "
            f"Z {pose[2] * 1000.0:.3f} mm"
        )

    @pyqtSlot(bool, str)
    def _on_ur_connection_changed(self, connected: bool, message: str) -> None:
        self.ur_connected = connected
        if connected:
            self.ur_connection_label.setText(f"Online at {UR_HOST}")
        else:
            self.latest_ur_pose = None
            self.ur_connection_label.setText(
                f"Offline: {message}" if message else f"Offline at {UR_HOST}"
            )
            if self.ur_capture_active:
                self.ur_capture_active = False
                self.ur_state_label.setText("Capture stopped: UR connection lost")
            if self.ur_monitor_active:
                self._stop_ur_speed_monitor("Stopped: UR connection lost")
        self._update_ur_controls()

    @pyqtSlot(object)
    def _on_ur_pose(self, sample: tuple[float, tuple[float, ...]]) -> None:
        self.latest_ur_pose = sample
        self.ur_live_pose.setText(self._format_ur_pose(sample[1]))
        self._update_ur_controls()

    def _selected_ur_sensors(self) -> tuple[int, int]:
        return (
            int(self.ur_first_sensor.currentData()),
            int(self.ur_second_sensor.currentData()),
        )

    def _update_ur_controls(self) -> None:
        first_sensor, second_sensor = self._selected_ur_sensors()
        pair = tuple(sorted((first_sensor, second_sensor)))
        supported_pair = pair in SENSOR_SPACING_SYMBOLS
        ready = (
            self.connected
            and self.have_setup_status
            and self.ur_connected
            and self.latest_ur_pose is not None
            and supported_pair
        )
        self.ur_start_button.setEnabled(
            ready and not self.ur_capture_active and not self.ur_monitor_active
        )
        self.ur_cancel_button.setEnabled(self.ur_capture_active)
        self.ur_apply_button.setEnabled(
            ready
            and not self.ur_capture_active
            and self.ur_distance_mm is not None
            and self.ur_distance_mm > 0.0
        )
        self.ur_monitor_start_button.setEnabled(
            ready and not self.ur_capture_active and not self.ur_monitor_active
        )
        self.ur_monitor_stop_button.setEnabled(self.ur_monitor_active)
        settings_enabled = not self.ur_capture_active and not self.ur_monitor_active
        self.ur_first_sensor.setEnabled(settings_enabled)
        self.ur_second_sensor.setEnabled(settings_enabled)
        self.ur_target_speed.setEnabled(not self.ur_monitor_active)

        spacings = self.latest_status.get("sensor_spacings") if self.latest_status else None
        if spacings is not None and supported_pair:
            spacing_index = LIGHT_BARRIER_PAIRS.index(pair)
            spacing = float(spacings[spacing_index])
            self.ur_monitor_distance.setText(f"{spacing:.3f} mm")
        else:
            self.ur_monitor_distance.setText("-")

    def _start_ur_capture(self) -> None:
        if not self.latest_status or self.latest_ur_pose is None:
            return
        first_sensor, second_sensor = self._selected_ur_sensors()
        if tuple(sorted((first_sensor, second_sensor))) not in SENSOR_SPACING_SYMBOLS:
            return
        states = self.latest_status["light_barriers"]
        self.ur_first_initial_state = bool(states[first_sensor - 1])
        self.ur_second_initial_state = bool(states[second_sensor - 1])
        self.ur_first_pose = None
        self.ur_second_pose = None
        self.ur_distance_mm = None
        self.ur_first_pose_label.setText("Not captured")
        self.ur_second_pose_label.setText("Not captured")
        self.ur_delta_label.setText("-")
        self.ur_distance_label.setText("-")
        self.ur_capture_active = True
        self.ur_state_label.setText(f"Waiting for light barrier {first_sensor}")
        self._update_ur_controls()

    def _process_ur_capture(self, states: list[bool]) -> None:
        if not self.ur_capture_active or self.latest_ur_pose is None:
            return
        first_sensor, second_sensor = self._selected_ur_sensors()
        current_pose = tuple(self.latest_ur_pose[1])
        if self.ur_first_pose is None:
            if bool(states[first_sensor - 1]) == self.ur_first_initial_state:
                return
            self.ur_first_pose = current_pose
            self.ur_first_pose_label.setText(self._format_ur_pose(current_pose))
            self.ur_state_label.setText(f"Waiting for light barrier {second_sensor}")
            return
        if bool(states[second_sensor - 1]) == self.ur_second_initial_state:
            return

        self.ur_second_pose = current_pose
        self.ur_second_pose_label.setText(self._format_ur_pose(current_pose))
        self.ur_distance_mm, delta = calculate_ur_pose_distance(
            self.ur_first_pose, self.ur_second_pose
        )
        self.ur_delta_label.setText(
            f"dX {delta[0]:.3f}, dY {delta[1]:.3f}, dZ {delta[2]:.3f} mm"
        )
        self.ur_distance_label.setText(f"{self.ur_distance_mm:.3f} mm")
        self.ur_capture_active = False
        self.ur_state_label.setText("Measurement complete")
        self._update_ur_controls()

    def _cancel_ur_capture(self) -> None:
        if self.ur_capture_active:
            self.ur_capture_active = False
            self.ur_state_label.setText("Measurement cancelled")
        self._update_ur_controls()

    def _apply_ur_measurement(self) -> None:
        if self.ur_distance_mm is None or self.ur_distance_mm <= 0.0:
            return
        pair = tuple(sorted(self._selected_ur_sensors()))
        symbol = SENSOR_SPACING_SYMBOLS.get(pair)
        if symbol is None:
            return
        self.ads.write_now(
            {symbol: float(self.ur_distance_mm)},
            f"ur_sensor_spacing_{pair[0]}{pair[1]}",
        )
        self.ur_state_label.setText(
            f"Applied {self.ur_distance_mm:.3f} mm to LB {pair[0]}-{pair[1]}"
        )

    def _start_ur_speed_monitor(self) -> None:
        if not self.latest_status or not self.ur_connected:
            return
        first_sensor, second_sensor = self._selected_ur_sensors()
        pair = tuple(sorted((first_sensor, second_sensor)))
        if pair not in SENSOR_SPACING_SYMBOLS:
            return
        counts = self.latest_status.get("light_barrier_event_counts")
        times = self.latest_status.get("light_barrier_event_times_ms")
        if counts is None or times is None:
            self.ur_monitor_state_label.setText("PLC event timestamps unavailable")
            return
        self.ur_monitor_event_counts = list(counts)
        self.ur_monitor_pending_edges = {False: None, True: None}
        self.ur_monitor_samples = []
        self.ur_monitor_active = True
        self.ur_monitor_state_label.setText("Monitoring stable sensor transitions")
        self._update_ur_monitor_results()
        self._update_ur_controls()

    def _stop_ur_speed_monitor(self, message: str = "Stopped") -> None:
        self.ur_monitor_active = False
        self.ur_monitor_pending_edges = {False: None, True: None}
        self.ur_monitor_state_label.setText(message)
        self._update_ur_controls()

    def _process_ur_speed_monitor(self, status: dict) -> None:
        if not self.ur_monitor_active or self.ur_monitor_event_counts is None:
            return
        counts = status.get("light_barrier_event_counts")
        event_times = status.get("light_barrier_event_times_ms")
        states = status.get("light_barriers")
        if counts is None or event_times is None or states is None:
            self._stop_ur_speed_monitor("Stopped: PLC event data unavailable")
            return

        selected_sensors = self._selected_ur_sensors()
        events = []
        missed_event = False
        for sensor in selected_sensors:
            index = sensor - 1
            previous_count = int(self.ur_monitor_event_counts[index])
            current_count = int(counts[index])
            event_delta = (current_count - previous_count) & 0xFFFFFFFF
            self.ur_monitor_event_counts[index] = current_count
            if event_delta == 1:
                events.append(
                    (int(event_times[index]), sensor, bool(states[index]))
                )
            elif event_delta > 1:
                missed_event = True

        if missed_event:
            self.ur_monitor_pending_edges = {False: None, True: None}
            self.ur_monitor_state_label.setText("Skipped events missed between polls")
        for event_time, sensor, edge_state in sorted(events):
            self._accept_ur_monitor_event(event_time, sensor, edge_state, status)

    def _accept_ur_monitor_event(
        self, event_time: int, sensor: int, edge_state: bool, status: dict
    ) -> None:
        pending = self.ur_monitor_pending_edges[edge_state]
        if pending is None or pending[1] == sensor:
            self.ur_monitor_pending_edges[edge_state] = (event_time, sensor)
            return

        elapsed_ms = (event_time - pending[0]) & 0xFFFFFFFF
        if elapsed_ms == 0 or elapsed_ms > 60_000:
            self.ur_monitor_pending_edges[edge_state] = (event_time, sensor)
            return

        pair = tuple(sorted(self._selected_ur_sensors()))
        spacing_index = LIGHT_BARRIER_PAIRS.index(pair)
        distance_mm = float(status["sensor_spacings"][spacing_index])
        expected_elapsed_ms = (
            distance_mm * 1000.0 / self.ur_target_speed.value()
        )
        if not 0.7 * expected_elapsed_ms <= elapsed_ms <= 1.5 * expected_elapsed_ms:
            self.ur_monitor_pending_edges[edge_state] = (event_time, sensor)
            self.ur_monitor_state_label.setText(
                f"Ignored implausible edge pair ({elapsed_ms} ms)"
            )
            return
        speed = distance_mm * 1000.0 / elapsed_ms
        self.ur_monitor_samples.append(
            {
                "speed": speed,
                "elapsed_ms": elapsed_ms,
                "direction": f"LB {pending[1]} -> LB {sensor}",
                "edge": "ON" if edge_state else "OFF",
                "start_time_ms": pending[0],
                "end_time_ms": event_time,
                "distance_mm": distance_mm,
            }
        )
        self._append_ur_speed_log(self.ur_monitor_samples[-1])
        self.ur_monitor_pending_edges[edge_state] = None
        self.ur_monitor_state_label.setText(
            f"Captured {self.ur_monitor_samples[-1]['direction']} ({elapsed_ms} ms)"
        )
        self._update_ur_monitor_results()

    def _update_ur_monitor_results(self) -> None:
        self.ur_monitor_samples_label.setText(str(len(self.ur_monitor_samples)))
        if not self.ur_monitor_samples:
            self.ur_monitor_latest_label.setText("-")
            self.ur_monitor_mean_label.setText("-")
            self.ur_monitor_range_label.setText("-")
            self.ur_monitor_directions_label.setText("-")
            self.ur_monitor_edges_label.setText("-")
            return

        latest = self.ur_monitor_samples[-1]
        speeds = [sample["speed"] for sample in self.ur_monitor_samples]
        statistics = calculate_speed_statistics(speeds, self.ur_target_speed.value())
        self.ur_monitor_latest_label.setText(
            f"{latest['speed']:.3f} mm/s, {latest['direction']}, {latest['edge']}"
        )
        self.ur_monitor_mean_label.setText(
            f"{statistics['mean']:.3f} +/- {statistics['standard_deviation']:.3f} mm/s, "
            f"error {statistics['mean_error_percent']:+.2f}%"
        )
        self.ur_monitor_range_label.setText(
            f"{statistics['minimum']:.3f} to {statistics['maximum']:.3f} mm/s"
        )
        direction_results = []
        for direction in dict.fromkeys(
            sample["direction"] for sample in self.ur_monitor_samples
        ):
            direction_speeds = [
                sample["speed"]
                for sample in self.ur_monitor_samples
                if sample["direction"] == direction
            ]
            direction_results.append(
                f"{direction}: {sum(direction_speeds) / len(direction_speeds):.3f}"
            )
        self.ur_monitor_directions_label.setText("; ".join(direction_results))
        edge_results = []
        for edge in ("ON", "OFF"):
            edge_speeds = [
                sample["speed"]
                for sample in self.ur_monitor_samples
                if sample["edge"] == edge
            ]
            if edge_speeds:
                edge_results.append(
                    f"{edge}: {sum(edge_speeds) / len(edge_speeds):.3f}"
                )
        self.ur_monitor_edges_label.setText("; ".join(edge_results) or "-")

    def _append_ur_speed_log(self, sample: dict) -> None:
        target_speed = self.ur_target_speed.value()
        first_sensor, second_sensor = self._selected_ur_sensors()
        row = [
            datetime.now().isoformat(timespec="milliseconds"),
            target_speed,
            first_sensor,
            second_sensor,
            sample["distance_mm"],
            sample["edge"],
            sample["direction"],
            sample["start_time_ms"],
            sample["end_time_ms"],
            sample["elapsed_ms"],
            sample["speed"],
            (sample["speed"] - target_speed) / target_speed * 100.0,
        ]
        try:
            write_header = not UR_SPEED_LOG_FILE.exists()
            with UR_SPEED_LOG_FILE.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if write_header:
                    writer.writerow(UR_SPEED_LOG_HEADER)
                writer.writerow(row)
        except OSError as exc:
            self.statusBar().showMessage(f"UR speed log failed: {exc}")

    def _consistency_spacings(self) -> tuple[float, ...]:
        return tuple(spin_box.value() for spin_box in self.consistency_spacing_inputs)

    def _reload_consistency_distances(self) -> None:
        if not self.latest_status:
            return
        plc_spacings = self.latest_status.get("sensor_spacings")
        if plc_spacings is None:
            return
        for plc_index, pair in enumerate(LIGHT_BARRIER_PAIRS):
            adjacent_index = ADJACENT_BARRIER_PAIRS.index(pair)
            self.consistency_spacing_inputs[adjacent_index].setValue(
                float(plc_spacings[plc_index])
            )
        self.consistency_spacings_initialized = True
        self.consistency_state_label.setText(
            "Loaded paired distances from PLC; intermediate distances remain assumptions"
        )

    def _start_consistency_monitor(self) -> None:
        if not self.latest_status:
            return
        event_counts = self.latest_status.get("light_barrier_event_counts")
        event_times = self.latest_status.get("light_barrier_event_times_ms")
        if event_counts is None or event_times is None:
            self.consistency_state_label.setText("PLC event timestamps unavailable")
            return
        spacings = self._consistency_spacings()
        if (
            self.consistency_samples
            and self.consistency_session_spacings is not None
            and spacings != self.consistency_session_spacings
        ):
            self._clear_consistency_samples()
        self.consistency_session_spacings = spacings
        self.consistency_collector = BarrierRunCollector(
            bool(self.consistency_edge.currentData())
        )
        self.consistency_collector.start(event_counts)
        self.consistency_monitor_active = True
        self.consistency_state_label.setText(
            "Monitoring forward traversals from LB 1 through LB 8"
        )
        self._update_consistency_controls()

    def _stop_consistency_monitor(self, message: str = "Stopped") -> None:
        self.consistency_monitor_active = False
        self.consistency_collector = None
        self.consistency_state_label.setText(message)
        self._update_consistency_controls()

    def _process_consistency_monitor(self, status: dict) -> None:
        if (
            not self.consistency_monitor_active
            or self.consistency_collector is None
        ):
            return
        completed_runs, missed_event = self.consistency_collector.process(status)
        if missed_event:
            self.consistency_state_label.setText(
                "A PLC event was missed between polls; incomplete traversals were discarded"
            )
        for event_times in completed_runs:
            try:
                result = analyze_barrier_run(
                    event_times,
                    self.consistency_session_spacings,
                    self.consistency_tolerance.value(),
                )
            except ValueError as exc:
                self.consistency_state_label.setText(f"Rejected traversal: {exc}")
                continue
            sample = {
                **result,
                "event_times_ms": tuple(event_times),
                "spacings_mm": tuple(self.consistency_session_spacings),
                "part_number": len(self.consistency_samples) + 1,
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "edge": "ON" if self.consistency_collector.edge_state else "OFF",
            }
            self.consistency_samples.append(sample)
            self._append_consistency_log(sample)
            self._append_consistency_table_row(sample)
            self.consistency_state_label.setText(
                f"Captured part {sample['part_number']}: "
                f"{'consistent' if sample['consistent'] else '; '.join(sample['diagnoses'])}"
            )
        if completed_runs:
            self._update_consistency_summary()
            if self.consistency_plot_dialog is not None:
                self.consistency_plot_dialog.set_samples(self.consistency_samples)

    def _append_consistency_table_row(self, sample: dict) -> None:
        row = self.consistency_table.rowCount()
        self.consistency_table.insertRow(row)
        values = [
            str(sample["part_number"]),
            sample["timestamp"].split("T")[-1],
            *[f"{speed:.1f} mm/s" for speed in sample["speeds"]],
            f"{sample['maximum_speed_change_percent']:.1f}%",
            "OK" if sample["consistent"] else "CHECK",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if not sample["consistent"]:
                item.setBackground(QColor("#fee2e2"))
                item.setToolTip("; ".join(sample["diagnoses"]))
            self.consistency_table.setItem(row, column, item)

    def _append_consistency_log(self, sample: dict) -> None:
        row = [
            sample["timestamp"],
            sample["part_number"],
            sample["edge"],
            *sample["spacings_mm"],
            *sample["event_times_ms"],
            *sample["speeds"],
            *sample["accelerations"],
            sample["maximum_speed_change_percent"],
            sample["consistent"],
            "; ".join(sample["diagnoses"]),
        ]
        try:
            write_header = not CONSISTENCY_LOG_FILE.exists()
            with CONSISTENCY_LOG_FILE.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if write_header:
                    writer.writerow(CONSISTENCY_LOG_HEADER)
                writer.writerow(row)
        except OSError as exc:
            self.statusBar().showMessage(f"Consistency log failed: {exc}")

    def _clear_consistency_samples(self) -> None:
        self.consistency_samples = []
        self.consistency_table.setRowCount(0)
        self.consistency_session_spacings = None
        self._update_consistency_summary()
        if self.consistency_plot_dialog is not None:
            self.consistency_plot_dialog.set_samples([])

    def _update_consistency_summary(self) -> None:
        if not self.consistency_samples:
            self.consistency_summary_label.setText("No completed parts")
            self.consistency_plot_button.setEnabled(False)
            return
        inconsistent = sum(
            not sample["consistent"] for sample in self.consistency_samples
        )
        section_means = [
            sum(sample["speeds"][index] for sample in self.consistency_samples)
            / len(self.consistency_samples)
            for index in range(LIGHT_BARRIER_COUNT - 1)
        ]
        means_text = ", ".join(
            f"{pair[0]}-{pair[1]} {mean:.1f}"
            for pair, mean in zip(ADJACENT_BARRIER_PAIRS, section_means)
        )
        self.consistency_summary_label.setText(
            f"{len(self.consistency_samples)} parts, {inconsistent} flagged; "
            f"section means [mm/s]: {means_text}"
        )
        self.consistency_plot_button.setEnabled(True)

    def _open_consistency_plot(self) -> None:
        if not self.consistency_samples:
            return
        if self.consistency_plot_dialog is None:
            self.consistency_plot_dialog = SpeedPlotDialog(
                self.consistency_samples, self
            )
            self.consistency_plot_dialog.finished.connect(
                lambda _result: setattr(self, "consistency_plot_dialog", None)
            )
        self.consistency_plot_dialog.show()
        self.consistency_plot_dialog.raise_()
        self.consistency_plot_dialog.activateWindow()

    def _update_consistency_controls(self) -> None:
        ready = self.connected and self.have_setup_status
        self.consistency_start_button.setEnabled(
            ready and not self.consistency_monitor_active
        )
        self.consistency_stop_button.setEnabled(self.consistency_monitor_active)
        settings_enabled = not self.consistency_monitor_active
        self.consistency_edge.setEnabled(settings_enabled)
        self.consistency_tolerance.setEnabled(settings_enabled)
        self.consistency_reload_distances_button.setEnabled(
            ready and settings_enabled
        )
        for distance_input in self.consistency_spacing_inputs:
            distance_input.setEnabled(settings_enabled)
        self.consistency_clear_button.setEnabled(
            bool(self.consistency_samples) and not self.consistency_monitor_active
        )
        self.consistency_plot_button.setEnabled(bool(self.consistency_samples))

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.calibrate_button.setEnabled(enabled)
        self.jog_button.setEnabled(enabled and self.mm_per_full_step > 0.0)
        self.plausibility_button.setEnabled(enabled and self.mm_per_full_step > 0.0)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(enabled)
        self.apply_button.setEnabled(False)
        self.ur_start_button.setEnabled(False)
        self.ur_cancel_button.setEnabled(False)
        self.ur_apply_button.setEnabled(False)
        self.ur_monitor_start_button.setEnabled(False)
        self.ur_monitor_stop_button.setEnabled(False)
        self.consistency_start_button.setEnabled(False)
        self.consistency_stop_button.setEnabled(False)
        self.consistency_reload_distances_button.setEnabled(False)
        self.consistency_clear_button.setEnabled(False)
        self.consistency_plot_button.setEnabled(False)
        for checkbox in self.barrier_inverted:
            checkbox.setEnabled(enabled)

    def _set_barrier_indicator(self, index: int, state: bool | None) -> None:
        if state is None:
            color = "#9ca3af"
            text = "Unknown"
        elif state:
            color = "#16a34a"
            text = "ON"
        else:
            color = "#4b5563"
            text = "OFF"
        self.barrier_lamps[index].setStyleSheet(
            f"background-color: {color}; border-radius: 9px;"
        )
        self.barrier_labels[index].setText(text)

    def _selected_sensors(self) -> tuple[int, int]:
        return int(self.first_sensor.currentData()), int(self.second_sensor.currentData())

    @pyqtSlot(bool, str)
    def _on_connection_changed(self, connected: bool, message: str) -> None:
        self.connected = connected
        self.have_setup_status = False
        self.debounce_initialized = False
        self._set_controls_enabled(connected)
        if connected:
            self.statusBar().showMessage(f"ADS online: {AMS_NET_ID} / {PLC_IP}")
        else:
            self.pending_measurement = None
            self.ur_capture_active = False
            if self.ur_monitor_active:
                self._stop_ur_speed_monitor("Stopped: ADS connection lost")
            if self.consistency_monitor_active:
                self._stop_consistency_monitor("Stopped: ADS connection lost")
            self.statusBar().showMessage(f"ADS offline: {message or 'reconnecting'}")
            for index in range(LIGHT_BARRIER_COUNT):
                self._set_barrier_indicator(index, None)
        self._update_ur_controls()
        self._update_consistency_controls()

    @pyqtSlot(object)
    def _on_initial_snapshot(self, snapshot: dict) -> None:
        calibration = snapshot["calibration"]
        self.mm_per_full_step = (
            float(calibration["mm_per_full_step"]) if calibration["valid"] else 0.0
        )
        self._update_calibration_display()

    @pyqtSlot(object)
    def _on_setup_status(self, status: dict) -> None:
        self.have_setup_status = True
        self.latest_status = status
        if not self.debounce_initialized:
            blockers = [
                QSignalBlocker(self.debounce_time),
                *(QSignalBlocker(checkbox) for checkbox in self.barrier_inverted),
            ]
            try:
                self.debounce_time.setValue(status["debounce_ms"])
                for checkbox, inverted in zip(
                    self.barrier_inverted,
                    status.get(
                        "light_barrier_inverted", LIGHT_BARRIER_INVERT_DEFAULTS
                    ),
                ):
                    checkbox.setChecked(bool(inverted))
            finally:
                del blockers
            self.debounce_initialized = True
        for index, barrier_state in enumerate(status["light_barriers"]):
            self._set_barrier_indicator(index, barrier_state)
        self._process_ur_capture(status["light_barriers"])
        self._process_ur_speed_monitor(status)
        if not self.consistency_spacings_initialized and status.get("sensor_spacings"):
            self._reload_consistency_distances()
        self._process_consistency_monitor(status)

        if status["conveyor_calibration_valid"]:
            self.mm_per_full_step = float(status["mm_per_full_step"])
        else:
            self.mm_per_full_step = 0.0
        self._update_calibration_display()
        self.speed_conversion.setText(
            f'{status["full_steps_per_sec"]:.3f} full steps/s, '
            f'{status["velocity_raw"]} / 10000'
        )
        self.internal_position.setText(f'{status["internal_position"]} increments')
        self.measured_pair.setText(
            f'LB {status["first_sensor"]} -> LB {status["second_sensor"]}'
            if status["first_captured"] or status["active"] or status["valid"]
            else "-"
        )
        self.first_position.setText(
            f'{status["first_position"]} increments'
            if status["first_captured"]
            else "Not captured"
        )
        self.second_position.setText(
            f'{status["second_position"]} increments'
            if status["second_captured"]
            else "Not captured"
        )
        self.position_difference.setText(
            f'{status["difference_increments"]} increments'
            if status["first_captured"]
            else "-"
        )
        self.measured_distance.setText(
            f'{status["distance_mm"]:.3f} mm' if status["valid"] else "-"
        )
        if self.pending_measurement and not status["ready_to_execute"]:
            self.measurement_state.setText("Enabling conveyor drive")
        else:
            self.measurement_state.setText(
                BARRIER_STATUS_TEXT.get(status["status_code"], "Unknown state")
            )
        for label, spacing in zip(self.spacing_labels, status["sensor_spacings"]):
            label.setText(f"{spacing:.3f} mm")

        active = bool(status["active"])
        ready = bool(status["ready_to_execute"])
        error = bool(status["drive_error"])
        can_start = (
            self.connected
            and not active
            and not status["drive_busy"]
            and not error
            and self.pending_measurement is None
            and self.mm_per_full_step > 0.0
            and self.first_sensor.currentData() != self.second_sensor.currentData()
        )
        self.start_button.setEnabled(can_start)
        self.stop_button.setEnabled(
            self.connected
            and (active or status["drive_busy"] or error or self.pending_measurement is not None)
        )
        settings_enabled = not active and self.pending_measurement is None
        self.first_sensor.setEnabled(settings_enabled)
        self.second_sensor.setEnabled(settings_enabled)
        self.measurement_speed.setEnabled(settings_enabled)
        self.maximum_travel.setEnabled(settings_enabled)
        self.debounce_time.setEnabled(settings_enabled)
        for checkbox in self.barrier_inverted:
            checkbox.setEnabled(self.connected and settings_enabled)
        self._update_apply_state()
        self._update_ur_controls()
        self._update_consistency_controls()

        if (
            self.pending_measurement
            and ready
            and not status["drive_busy"]
            and not error
        ):
            command = self.pending_measurement
            self.pending_measurement = None
            self.ads.start_barrier_calibration(*command)
            self.start_button.setEnabled(False)
            self.measurement_state.setText("Starting rightward measurement")

    def _update_calibration_display(self) -> None:
        if self.mm_per_full_step > 0.0:
            self.calibration_factor.setText(
                f"{self.mm_per_full_step:.6f} mm/full step "
                f"({1.0 / self.mm_per_full_step:.3f} full steps/mm)"
            )
            with QSignalBlocker(self.measurement_speed):
                current_speed = self.measurement_speed.value()
                self.measurement_speed.setRange(
                    self.mm_per_full_step, 500.0 * self.mm_per_full_step
                )
                self.measurement_speed.setValue(current_speed)
        else:
            self.calibration_factor.setText("Not calibrated")
        self.jog_button.setEnabled(self.connected and self.mm_per_full_step > 0.0)
        self.plausibility_button.setEnabled(
            self.connected and self.mm_per_full_step > 0.0
        )

    def _update_apply_state(self) -> None:
        valid = bool(self.latest_status and self.latest_status["valid"])
        active = bool(self.latest_status and self.latest_status["active"])
        pair = (
            tuple(
                sorted(
                    (
                        int(self.latest_status["first_sensor"]),
                        int(self.latest_status["second_sensor"]),
                    )
                )
            )
            if self.latest_status
            else ()
        )
        self.apply_button.setEnabled(
            self.connected and valid and not active and pair in SENSOR_SPACING_SYMBOLS
        )

    def _open_conveyor_calibration(self) -> None:
        if not self.connected:
            return
        self.ads.set_setup_polling(False)
        dialog = ConveyorCalibrationDialog(self.ads, self)
        dialog.exec()
        self.ads.set_setup_polling(True)

    def _open_conveyor_jogging(self) -> None:
        if not self.connected or self.mm_per_full_step <= 0.0:
            return
        self.ads.set_setup_polling(False)
        dialog = ConveyorJogDialog(self.ads, self)
        dialog.exec()
        self.ads.set_setup_polling(True)

    def _open_plausibility_check(self) -> None:
        if not self.connected or self.mm_per_full_step <= 0.0:
            return
        dialog = VelocityPlausibilityDialog(self.ads, self)
        if self.latest_status:
            dialog._on_status(self.latest_status)
        dialog.exec()

    def _start_measurement(self) -> None:
        first_sensor, second_sensor = self._selected_sensors()
        if first_sensor == second_sensor or self.mm_per_full_step <= 0.0:
            return
        max_steps, _actual_distance, speed_full_steps = calculate_conveyor_jog(
            self.maximum_travel.value(),
            self.measurement_speed.value(),
            self.mm_per_full_step,
        )
        command = (
            first_sensor,
            second_sensor,
            max_steps,
            speed_full_steps,
            self.debounce_time.value(),
        )
        if self.latest_status and self.latest_status["ready_to_execute"]:
            self.ads.start_barrier_calibration(*command)
            self.measurement_state.setText("Starting rightward measurement")
        else:
            self.pending_measurement = command
            self.ads.write_now(
                {
                    "MAIN.GuiConveyorEnabled": False,
                    "MAIN.GuiConveyorCalibrationMode": True,
                },
                "setup_enable_drive",
            )
            self.measurement_state.setText("Enabling conveyor drive")
        self.start_button.setEnabled(False)
        self.apply_button.setEnabled(False)

    def _stop_measurement(self) -> None:
        self.pending_measurement = None
        self.ads.stop_setup_motion()
        self.measurement_state.setText("Stopping")

    def _apply_measurement(self) -> None:
        if not self.latest_status or not self.latest_status["valid"]:
            return
        pair = tuple(
            sorted(
                (
                    int(self.latest_status["first_sensor"]),
                    int(self.latest_status["second_sensor"]),
                )
            )
        )
        symbol = SENSOR_SPACING_SYMBOLS.get(pair)
        if symbol is None:
            return
        distance_mm = float(self.latest_status["distance_mm"])
        self.ads.write_now({symbol: distance_mm}, f"sensor_spacing_{pair[0]}{pair[1]}")

    @pyqtSlot(str)
    def _on_write_finished(self, context: str) -> None:
        self.statusBar().showMessage(f"ADS write complete: {context}")

    @pyqtSlot(str, str)
    def _on_ads_error(self, context: str, message: str) -> None:
        self.statusBar().showMessage(f"{context}: {message}")

    def closeEvent(self, event) -> None:
        if hasattr(self, "ur_thread") and self.ur_thread.isRunning():
            self.ur_stop_requested.emit()
            if not self.ur_thread.wait(1500):
                self.ur_thread.quit()
                self.ur_thread.wait(500)
        self.ads.set_setup_polling(False)
        if self.ads.is_connected:
            self.ads.stop_setup_motion()
        self.ads.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    lease = PlcControlLease.acquire()
    if lease is None:
        QMessageBox.critical(
            None,
            "SPS bereits belegt",
            "BiBaZu Reorientation Control, Pressure Control oder eine weitere "
            "Conveyor-Setup-Instanz steuert bereits die SPS. Bitte zuerst die "
            "andere Steueranwendung schließen.",
        )
        return 2
    window = ConveyorSetupWindow()
    window.show()
    try:
        return app.exec()
    finally:
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
