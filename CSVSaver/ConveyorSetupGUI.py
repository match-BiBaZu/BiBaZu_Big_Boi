import sys

from PyQt6.QtCore import QSignalBlocker, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QStyle,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from PressureControlGUI import (
    ADS_TIMEOUT_MS,
    AMS_NET_ID,
    PLC_IP,
    AdsController,
    ConveyorCalibrationDialog,
    ConveyorJogDialog,
    calculate_conveyor_jog,
)


SENSOR_SPACING_SYMBOLS = {
    (1, 2): "MAIN.GuiSensorSpacing12Mm",
    (3, 4): "MAIN.GuiSensorSpacing34Mm",
    (5, 6): "MAIN.GuiSensorSpacing56Mm",
}

BARRIER_STATUS_TEXT = {
    0: "Ready",
    1: "Waiting for first light barrier",
    2: "Waiting for second light barrier",
    3: "Measurement complete",
    4: "Measurement cancelled or invalid",
    5: "EL7047 error",
}


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
            minimum_time, maximum_time = self.latest_status.get(
                "travel_time_bounds", (1, 30000)
            )
            if distance > 0.0 and minimum_time > 0 and maximum_time > 0:
                minimum_speed = max(0.1, distance * 1000.0 / maximum_time)
                maximum_speed = min(500.0, distance * 1000.0 / minimum_time)
                maximum_speed = max(minimum_speed, maximum_speed)
                with QSignalBlocker(self.target_speed_input):
                    current_speed = self.target_speed_input.value()
                    self.target_speed_input.setRange(minimum_speed, maximum_speed)
                    self.target_speed_input.setValue(current_speed)
                self.target_speed_input.setToolTip(
                    f"Valid for the PLC timing window: {minimum_speed:.3f} to "
                    f"{maximum_speed:.3f} mm/s"
                )

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
    def __init__(self) -> None:
        super().__init__()
        self.ads = AdsController(self)
        self.connected = False
        self.have_setup_status = False
        self.latest_status = None
        self.mm_per_full_step = 0.0
        self.pending_measurement = None
        self.debounce_initialized = False

        self.setWindowTitle("Conveyor and Light Barrier Setup")
        self.resize(850, 610)
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
        for index in range(1, 7):
            column = index - 1
            title = QLabel(f"LB {index}")
            title.setStyleSheet("font-weight: 600;")
            lamp = QLabel()
            lamp.setFixedSize(18, 18)
            state = QLabel("Unknown")
            state.setMinimumWidth(58)
            barrier_layout.addWidget(title, 0, column)
            barrier_layout.addWidget(lamp, 1, column)
            barrier_layout.addWidget(state, 2, column)
            self.barrier_lamps.append(lamp)
            self.barrier_labels.append(state)
            self._set_barrier_indicator(index - 1, None)
        barrier_layout.setColumnStretch(6, 1)
        layout.addWidget(barrier_group)

        measurement_group = QGroupBox("Light Barrier Distance Calibration")
        measurement_layout = QGridLayout(measurement_group)
        form = QFormLayout()
        self.first_sensor = QComboBox()
        self.second_sensor = QComboBox()
        for index in range(1, 7):
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
        layout.addWidget(measurement_group)

        spacing_group = QGroupBox("Velocity Sensor Spacings")
        spacing_layout = QHBoxLayout(spacing_group)
        self.spacing_labels = [QLabel("-") for _ in range(3)]
        for pair, label in zip(((1, 2), (3, 4), (5, 6)), self.spacing_labels):
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

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.calibrate_button.setEnabled(enabled)
        self.jog_button.setEnabled(enabled and self.mm_per_full_step > 0.0)
        self.plausibility_button.setEnabled(enabled and self.mm_per_full_step > 0.0)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(enabled)
        self.apply_button.setEnabled(False)

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
            self.statusBar().showMessage(f"ADS offline: {message or 'reconnecting'}")
            for index in range(6):
                self._set_barrier_indicator(index, None)

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
            with QSignalBlocker(self.debounce_time):
                self.debounce_time.setValue(status["debounce_ms"])
            self.debounce_initialized = True
        for index, barrier_state in enumerate(status["light_barriers"]):
            self._set_barrier_indicator(index, barrier_state)

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
        self._update_apply_state()

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
        self.ads.set_setup_polling(False)
        if self.ads.is_connected:
            self.ads.stop_setup_motion()
        self.ads.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = ConveyorSetupWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
