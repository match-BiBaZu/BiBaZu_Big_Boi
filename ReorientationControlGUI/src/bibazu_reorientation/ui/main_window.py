from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QAction, QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.batch_controller import BatchController
from bibazu_reorientation.config import (
    RoadmapHashMismatchError,
    TransitionResolver,
    load_part_definition,
    roadmap_readiness,
)
from bibazu_reorientation.hardware.camera import CameraAdapter
from bibazu_reorientation.hardware.light import LightAdapter
from bibazu_reorientation.hardware.pressure import PressureAdapter
from bibazu_reorientation.hardware.robot import UrAngleWorker
from bibazu_reorientation.inference import InferenceConfig, InferenceWorker
from bibazu_reorientation.mesh_preview import render_mesh_preview
from bibazu_reorientation.models import (
    BatchState,
    CameraFrame,
    CameraStatus,
    ConnectionState,
    InferenceFrame,
    PressureBaseline,
    PressureProfile,
)
from bibazu_reorientation.profiles import (
    compare_machine_parameters,
    compose_pressure_profiles,
    load_pressure_profile,
)
from bibazu_reorientation.settings import (
    CAMERA_EXPOSURE_MAX_US,
    CAMERA_EXPOSURE_MIN_US,
    AppSettings,
)
from bibazu_reorientation.ui.hardware_settings_dialog import HardwareSettingsDialog
from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog
from bibazu_reorientation.ui.setup_dialog import SetupDialog

LOGGER = logging.getLogger(__name__)
EXPOSURE_SLIDER_STEPS = 1000
WARNING_DISPLAY_MS = 15_000


class LightPanel(QGroupBox):
    def __init__(self, adapter: LightAdapter) -> None:
        super().__init__(adapter.name)
        self.adapter = adapter
        self.status = QLabel("Disconnected")
        self.confirm = QCheckBox("Settings confirmed for this cycle")
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(50)
        self.cct = QSlider(Qt.Orientation.Horizontal)
        self.cct.setRange(3200, 5600)
        self.cct.setValue(5600)
        self.hue = QSlider(Qt.Orientation.Horizontal)
        self.hue.setRange(0, 360)
        self.saturation = QSlider(Qt.Orientation.Horizontal)
        self.saturation.setRange(0, 100)
        self.saturation.setValue(100)
        apply = QPushButton("Apply CCT")
        apply.clicked.connect(lambda: adapter.set_cct(self.brightness.value(), self.cct.value()))
        power_on = QPushButton("Light on")
        power_on.clicked.connect(lambda: adapter.set_power(True))
        power_off = QPushButton("Light off")
        power_off.clicked.connect(lambda: adapter.set_power(False))
        hsi = QPushButton("Apply HSI")
        hsi.clicked.connect(
            lambda: adapter.set_hsi(
                self.brightness.value(), self.hue.value(), self.saturation.value()
            )
        )
        form = QFormLayout(self)
        form.addRow("Status", self.status)
        form.addRow("Brightness", self.brightness)
        form.addRow("CCT", self.cct)
        form.addRow("Hue", self.hue)
        form.addRow("Saturation", self.saturation)
        form.addRow(apply, power_on)
        form.addRow(power_off)
        form.addRow(hsi)
        form.addRow(self.confirm)
        adapter.state_changed.connect(self._adapter_state_changed)

    @pyqtSlot(object, str)
    def _adapter_state_changed(self, state: object, detail: str) -> None:
        self.status.setText(f"{state}: {detail}")


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("BiBaZu Reorientation Control")
        self.resize(1450, 900)
        self.pressure = PressureAdapter(settings)
        self.camera = CameraAdapter(settings)
        self.light1 = LightAdapter(
            "Neewer light 1",
            settings.light_1_address,
            excluded_addresses=lambda: {self.light2.address},
            auto_reconnect=False,
        )
        self.light2 = LightAdapter(
            "Neewer light 2",
            settings.light_2_address,
            excluded_addresses=lambda: {self.light1.address},
            auto_reconnect=False,
        )
        self.controller = BatchController(
            self.pressure,
            handoff_line_ratio=settings.handoff_line_percent / 100.0,
        )
        self.inference: InferenceWorker | None = None
        self._yolo_reload_pending = False
        self._shutting_down = False
        self.part = None
        self.profile = None
        self.ur_worker: UrAngleWorker | None = None
        self._last_camera_frame = 0.0
        self._camera_status_data = CameraStatus()
        self._exposure_min_us = CAMERA_EXPOSURE_MIN_US
        self._exposure_max_us = CAMERA_EXPOSURE_MAX_US
        self._restore_camera_exposure = True
        self._updating_exposure_ui = False
        self._preflight_ok = False
        self._displayed_preflight_checks: dict[str, bool] | None = None
        self._roadmap_mode = False
        self._roadmap_profiles: dict[str, PressureProfile] = {}
        self._pressure_baseline: PressureBaseline | None = None
        self._machine_parameters_confirmed = False
        self._updating_machine_parameters = False
        self._profile_parameter_details = ""
        self._light_connect_task: asyncio.Task[None] | None = None
        self._manual_conveyor_command_pending = False
        self._build_ui()
        self._wire()
        self.freshness_timer = QTimer(self)
        self.freshness_timer.setInterval(250)
        self.freshness_timer.timeout.connect(self._update_camera_freshness)
        self.freshness_timer.start()
        self.exposure_apply_timer = QTimer(self)
        self.exposure_apply_timer.setSingleShot(True)
        self.exposure_apply_timer.setInterval(250)
        self.exposure_apply_timer.timeout.connect(self._apply_camera_exposure)
        self.warning_display_timer = QTimer(self)
        self.warning_display_timer.setSingleShot(True)
        self.warning_display_timer.setInterval(WARNING_DISPLAY_MS)
        self.warning_display_timer.timeout.connect(self._clear_warning_banner)

    def _build_ui(self) -> None:
        menu = self.menuBar().addMenu("Configuration")
        self.new_config_action = QAction("New part configuration (roadmap) …", self)
        self.new_config_action.triggered.connect(self.new_configuration)
        menu.addAction(self.new_config_action)
        self.open_config_action = QAction("Open part configuration …", self)
        self.open_config_action.triggered.connect(self.open_configuration)
        menu.addAction(self.open_config_action)
        self.edit_config_action = QAction("Edit loaded part configuration …", self)
        self.edit_config_action.triggered.connect(self.edit_configuration)
        self.edit_config_action.setEnabled(False)
        menu.addAction(self.edit_config_action)
        menu.addSeparator()
        hardware_settings = QAction("Hardware settings …", self)
        hardware_settings.triggered.connect(self.open_hardware_settings)
        menu.addAction(hardware_settings)
        self.part_label = QLabel("No configuration")
        self.transition_label = QLabel("Actuation profile: –")
        self.model_label = QLabel("No 3D model selected")
        self.model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_label.setFixedSize(250, 175)
        self.model_label.setStyleSheet(
            "background:#111827;color:#94a3b8;border:1px solid #334155;border-radius:8px"
        )
        config_buttons = QVBoxLayout()
        self.new_config_button = QPushButton("New configuration")
        self.new_config_button.clicked.connect(self.new_configuration)
        self.open_config_button = QPushButton("Open configuration")
        self.open_config_button.clicked.connect(self.open_configuration)
        self.edit_config_button = QPushButton("Edit configuration")
        self.edit_config_button.clicked.connect(self.edit_configuration)
        self.edit_config_button.setEnabled(False)
        config_buttons.addWidget(self.new_config_button)
        config_buttons.addWidget(self.open_config_button)
        config_buttons.addWidget(self.edit_config_button)
        self.video = QLabel("Camera not connected")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(760, 520)
        self.video.setStyleSheet("background:#111827;color:#94a3b8;border-radius:8px")
        self.pose_label = QLabel("Detected pose: –    Target pose: 1    Confidence: –")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        configuration_details = QVBoxLayout()
        configuration_details.addWidget(self.part_label)
        configuration_details.addWidget(self.transition_label)
        configuration_details.addLayout(config_buttons)
        configuration_details.addStretch()
        configuration_header = QHBoxLayout()
        configuration_header.addWidget(self.model_label)
        configuration_header.addLayout(configuration_details, 1)
        left_layout.addLayout(configuration_header)
        left_layout.addWidget(self.video, 1)
        left_layout.addWidget(self.pose_label)
        self.plc_status = QLabel("PLC: disconnected")
        self.camera_status = QLabel("Camera: disconnected")
        self.exposure_slider = QSlider(Qt.Orientation.Horizontal)
        self.exposure_slider.setRange(0, EXPOSURE_SLIDER_STEPS)
        self.exposure_slider.setEnabled(False)
        self.exposure_slider.setMinimumWidth(110)
        self.exposure_slider.setToolTip(
            "Logarithmic exposure-time control from 1 000 to 20 000 µs; the selected "
            "value is applied after 250 ms without another change and restored next time."
        )
        self.exposure_slider.valueChanged.connect(self._exposure_slider_changed)
        self.exposure_value = QLabel("– µs")
        self.exposure_value.setMinimumWidth(75)
        self.camera_fps = QLabel("FPS: –")
        self.camera_fps.setToolTip(
            "cam = camera-reported rate, raw = measured buffer rate, view = displayed preview rate"
        )
        camera_controls = QHBoxLayout()
        camera_controls.addWidget(QLabel("Exposure"))
        camera_controls.addWidget(self.exposure_slider, 1)
        camera_controls.addWidget(self.exposure_value)
        camera_controls.addWidget(self.camera_fps)
        self.handoff_slider = QSlider(Qt.Orientation.Horizontal)
        self.handoff_slider.setRange(5, 80)
        self.handoff_slider.setValue(self.settings.handoff_line_percent)
        self.handoff_slider.setToolTip(
            "Vertical image line at which a tracked part is committed to the PLC queue."
        )
        self.handoff_value = QLabel(f"{self.settings.handoff_line_percent} %")
        self.handoff_slider.valueChanged.connect(self._handoff_line_changed)
        self.connect_button = QPushButton("Connect all components")
        self.connect_button.clicked.connect(self.connect_all)
        self.disconnect_button = QPushButton("Disconnect all components")
        self.disconnect_button.clicked.connect(self.disconnect_all)
        hardware = QGroupBox("Hardware")
        hardware_layout = QVBoxLayout(hardware)
        hardware_layout.addWidget(self.plc_status)
        hardware_layout.addWidget(self.camera_status)
        hardware_layout.addLayout(camera_controls)
        # Snapshot-queue production does not present the legacy moving handoff
        # line. Its widgets/settings remain for backward-compatible settings.
        self.yolo_status = QLabel("YOLO: no model loaded")
        self.yolo_status.setWordWrap(True)
        self.load_yolo_button = QPushButton("Load YOLO model")
        self.load_yolo_button.setEnabled(False)
        self.load_yolo_button.clicked.connect(self.load_yolo_model)
        yolo_row = QHBoxLayout()
        yolo_row.addWidget(self.yolo_status, 1)
        yolo_row.addWidget(self.load_yolo_button)
        hardware_layout.addLayout(yolo_row)
        self.use_ur_angle = QCheckBox("Apply")
        self.ur_angle_input = QDoubleSpinBox()
        self.ur_angle_input.setRange(15.5, 21.0)
        self.ur_angle_input.setDecimals(1)
        self.ur_angle_input.setSingleStep(0.1)
        self.ur_angle_input.setSuffix(" °")
        self.ur_angle_input.setValue(18.0)
        ur_row = QHBoxLayout()
        ur_row.addWidget(self.use_ur_angle)
        ur_row.addWidget(self.ur_angle_input, 1)
        self.conveyor_speed_input = QDoubleSpinBox()
        self.conveyor_speed_input.setRange(0.1, 5000.0)
        self.conveyor_speed_input.setDecimals(1)
        self.conveyor_speed_input.setSuffix(" mm/s")
        self.conveyor_speed_input.setValue(100.0)
        machine_form = QFormLayout()
        machine_form.addRow("UR Ry angle", ur_row)
        machine_form.addRow("Conveyor speed", self.conveyor_speed_input)
        hardware_layout.addLayout(machine_form)
        self.machine_parameter_status = QLabel("Load a configuration to compare profiles.")
        self.machine_parameter_status.setWordWrap(True)
        hardware_layout.addWidget(self.machine_parameter_status)
        self.apply_machine_parameters_button = QPushButton("Use machine parameters")
        self.apply_machine_parameters_button.setEnabled(False)
        self.apply_machine_parameters_button.clicked.connect(self.apply_machine_parameters)
        self.ur_button = QPushButton("Apply UR angle")
        self.ur_button.clicked.connect(self.apply_ur)
        self.ur_button.setEnabled(False)
        machine_buttons = QHBoxLayout()
        machine_buttons.addWidget(self.apply_machine_parameters_button)
        machine_buttons.addWidget(self.ur_button)
        hardware_layout.addLayout(machine_buttons)
        self.manual_conveyor_start_button = QPushButton("Start conveyor")
        self.manual_conveyor_start_button.setToolTip(
            "Manual conveyor operation without YOLO or light preflight. Arrays are disabled."
        )
        self.manual_conveyor_start_button.setEnabled(False)
        self.manual_conveyor_start_button.clicked.connect(self._manual_conveyor_start)
        self.manual_conveyor_stop_button = QPushButton("Stop conveyor")
        self.manual_conveyor_stop_button.setToolTip(
            "Stops manual transport; during a production run this requests controlled draining."
        )
        self.manual_conveyor_stop_button.setEnabled(False)
        self.manual_conveyor_stop_button.clicked.connect(self._manual_conveyor_stop)
        manual_conveyor_buttons = QHBoxLayout()
        manual_conveyor_buttons.addWidget(self.manual_conveyor_start_button)
        manual_conveyor_buttons.addWidget(self.manual_conveyor_stop_button)
        hardware_layout.addLayout(manual_conveyor_buttons)
        self.manual_conveyor_status = QLabel("Manual conveyor: PLC disconnected")
        self.manual_conveyor_status.setWordWrap(True)
        hardware_layout.addWidget(self.manual_conveyor_status)
        hardware_layout.addWidget(self.connect_button)
        hardware_layout.addWidget(self.disconnect_button)
        self.light_panel1 = LightPanel(self.light1)
        self.light_panel2 = LightPanel(self.light2)
        self.preflight = QVBoxLayout()
        preflight_box = QGroupBox("Preflight")
        preflight_box.setLayout(self.preflight)
        self.batch_counters = QLabel(
            "Snapshot 0 · poses 0 · queued 0 · LB1 0 · completed 0 · bypass 0 · PLC queue 0/128"
        )
        self.batch_counters.setWordWrap(True)
        self.start_button = QPushButton("Start production run")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("Cancel production start")
        self.stop_button.setStyleSheet("font-weight:bold;background:#d97706;color:white;padding:12px")
        self.stop_button.clicked.connect(self.controller.finish_run)
        self.cycle_status = QLabel("No configuration")
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(hardware)
        right_layout.addWidget(self.light_panel1)
        right_layout.addWidget(self.light_panel2)
        right_layout.addWidget(preflight_box)
        right_layout.addWidget(self.batch_counters)
        right_layout.addWidget(self.cycle_status)
        right_layout.addWidget(self.start_button)
        right_layout.addWidget(self.stop_button)
        right_layout.addStretch()
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.warning_banner = QLabel()
        self.warning_banner.setWordWrap(True)
        self.warning_banner.setMinimumHeight(58)
        self.warning_banner.setStyleSheet(
            "background:#fef3c7;color:#7c2d12;border:2px solid #f59e0b;"
            "border-radius:6px;padding:10px;font-weight:bold;font-size:13px;"
        )
        self.warning_banner.hide()
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(splitter, 1)
        central_layout.addWidget(self.warning_banner)
        self.setCentralWidget(central)

    def _wire(self) -> None:
        self.pressure.connection_changed.connect(self._plc_connection_changed)
        self.pressure.baseline_ready.connect(self._baseline_ready)
        self.pressure.snapshot_changed.connect(self._manual_conveyor_snapshot)
        self.pressure.operation_finished.connect(self._pressure_operation_finished)
        self.pressure.operation_failed.connect(self._pressure_operation_failed)
        self.camera.state_changed.connect(self._camera_state_changed)
        self.camera.status_changed.connect(self._camera_status_changed)
        self.camera.exposure_applied.connect(self._camera_exposure_applied)
        self.camera.exposure_failed.connect(self._camera_exposure_error)
        self.camera.frame_ready.connect(self._camera_frame)
        self.camera.error.connect(self._camera_error)
        self.controller.preflight_changed.connect(self._preflight)
        self.controller.state_changed.connect(self._cycle_state_changed)
        self.controller.warning_raised.connect(self._production_warning)
        self.controller.counters_changed.connect(self._batch_counters_changed)
        self.light_panel1.confirm.toggled.connect(self._lights_changed)
        self.light_panel2.confirm.toggled.connect(self._lights_changed)
        self.light1.status_changed.connect(self._light_status_changed)
        self.light2.status_changed.connect(self._light_status_changed)
        self.light1.error.connect(self._light1_error)
        self.light2.error.connect(self._light2_error)
        self.use_ur_angle.toggled.connect(self._machine_parameters_edited)
        self.ur_angle_input.valueChanged.connect(self._machine_parameters_edited)
        self.conveyor_speed_input.valueChanged.connect(self._machine_parameters_edited)

    def _plc_connection_changed(self, connected: bool, detail: str) -> None:
        self.plc_status.setText(f"PLC: {'connected' if connected else 'disconnected'} – {detail}")
        if not connected:
            self._manual_conveyor_command_pending = False
            self.manual_conveyor_status.setText("Manual conveyor: PLC disconnected")
        self._update_manual_conveyor_buttons()
        if connected:
            LOGGER.info(
                "ADS connected: %s / %s", self.settings.plc_ams_net_id, self.settings.plc_ip
            )
        else:
            LOGGER.error("ADS connection failed/lost: %s", detail)

    @staticmethod
    def _manual_conveyor_idle_state(state: BatchState) -> bool:
        return state in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
            BatchState.COMPLETE,
            BatchState.FAULT,
        }

    def _manual_conveyor_can_start(self) -> bool:
        snapshot = self.controller.snapshot
        return (
            not self._manual_conveyor_command_pending
            and self._manual_conveyor_idle_state(self.controller.state)
            and snapshot.connected
            and self.conveyor_speed_input.value() > 0.0
        )

    def _update_manual_conveyor_buttons(self) -> None:
        snapshot = self.controller.snapshot
        self.manual_conveyor_start_button.setEnabled(self._manual_conveyor_can_start())
        self.manual_conveyor_stop_button.setEnabled(
            snapshot.connected and not self._manual_conveyor_command_pending
        )

    @pyqtSlot(object)
    def _manual_conveyor_snapshot(self, snapshot: object) -> None:
        self._update_manual_conveyor_buttons()
        if self._manual_conveyor_command_pending:
            return
        if getattr(snapshot, "conveyor_motion_state", 0) != 0:
            self.manual_conveyor_status.setText("Manual conveyor: running / drive active")
        elif getattr(snapshot, "connected", False):
            self.manual_conveyor_status.setText("Manual conveyor: stopped")

    def _manual_conveyor_start(self) -> None:
        if not self._manual_conveyor_can_start():
            QMessageBox.warning(
                self,
                "Manual conveyor",
                "Manual start requires only an ADS connection, an idle automatic cycle, "
                "and a positive conveyor speed.",
            )
            return
        self._manual_conveyor_command_pending = True
        self._update_manual_conveyor_buttons()
        snapshot = self.controller.snapshot
        if snapshot.reorientation_state != 0 or snapshot.reorientation_fault_code != 0:
            self.manual_conveyor_status.setText(
                "Manual conveyor: clearing previous cycle latch …"
            )
            values: dict[str, bool | float] = {
                "MAIN.GuiReorientationControlActive": True,
                "MAIN.GuiReorientationReset": True,
                "MAIN.GuiReorientationAbort": False,
                "MAIN.GuiReorientationStart": False,
                "MAIN.GuiConveyorEnabled": False,
            }
            values.update({f"MAIN.GuiArrayEnabled{index}": False for index in range(1, 5)})
            self.pressure.write("manual_conveyor_reset", values, True)
            return
        self._send_manual_conveyor_start()

    def _send_manual_conveyor_start(self) -> None:
        if (
            self._shutting_down
            or not self._manual_conveyor_command_pending
            or not self.controller.snapshot.connected
        ):
            self._manual_conveyor_command_pending = False
            self._update_manual_conveyor_buttons()
            return
        self.manual_conveyor_status.setText("Manual conveyor: sending start …")
        values: dict[str, bool | float] = {
            "MAIN.GuiReorientationControlActive": False,
            "MAIN.GuiReorientationReset": False,
            "MAIN.GuiReorientationAbort": False,
            "MAIN.GuiReorientationStart": False,
            "MAIN.GuiConveyorCalibrationMode": False,
            "MAIN.GuiVelocityCheckMode": False,
            "MAIN.GuiForceDelayMeasurementEnabled": False,
            "MAIN.GuiConveyorReverse": False,
            "MAIN.GuiConveyorSpeedMmPerSec": self.conveyor_speed_input.value(),
            "MAIN.GuiConveyorEnabled": True,
        }
        values.update({f"MAIN.GuiArrayEnabled{index}": False for index in range(1, 5)})
        self.pressure.write("manual_conveyor_start", values, True)

    def _manual_conveyor_stop(self) -> None:
        if not self._manual_conveyor_idle_state(self.controller.state):
            self.manual_conveyor_status.setText(
                "Manual conveyor: controlled production-run draining requested …"
            )
            self.controller.stop()
            return
        if not self.controller.snapshot.connected:
            return
        self._manual_conveyor_command_pending = True
        self._update_manual_conveyor_buttons()
        self.manual_conveyor_status.setText("Manual conveyor: sending stop …")
        values = {"MAIN.GuiConveyorEnabled": False}
        values.update({f"MAIN.GuiArrayEnabled{index}": False for index in range(1, 5)})
        self.pressure.write("manual_conveyor_stop", values, True)

    @pyqtSlot(str)
    def _pressure_operation_finished(self, name: str) -> None:
        if name == "manual_conveyor_reset":
            self.manual_conveyor_status.setText(
                "Manual conveyor: previous cycle latch cleared …"
            )
            # Keep Reset asserted long enough for at least one PLC scan before
            # releasing the automation owner and starting in legacy/manual mode.
            QTimer.singleShot(100, self._send_manual_conveyor_start)
            return
        if name not in {"manual_conveyor_start", "manual_conveyor_stop"}:
            return
        self._manual_conveyor_command_pending = False
        self.manual_conveyor_status.setStyleSheet("")
        self.manual_conveyor_status.setText(
            "Manual conveyor: start accepted – waiting for drive"
            if name == "manual_conveyor_start"
            else "Manual conveyor: stop accepted – waiting for standstill"
        )
        self._update_manual_conveyor_buttons()

    @pyqtSlot(str, str)
    def _pressure_operation_failed(self, name: str, detail: str) -> None:
        if name not in {
            "manual_conveyor_reset",
            "manual_conveyor_start",
            "manual_conveyor_stop",
        }:
            return
        self._manual_conveyor_command_pending = False
        self.manual_conveyor_status.setStyleSheet("color:#b91c1c;font-weight:bold")
        self.manual_conveyor_status.setText(f"Manual conveyor failed: {detail}")
        self._update_manual_conveyor_buttons()

    @staticmethod
    def _hardware_error(component: str, detail: str) -> None:
        LOGGER.error("%s: %s", component, detail)

    @pyqtSlot(object)
    def _light_status_changed(self, _status: object) -> None:
        self._lights_changed()

    @pyqtSlot(str)
    def _light1_error(self, detail: str) -> None:
        self._hardware_error("Light 1", detail)

    @pyqtSlot(str)
    def _light2_error(self, detail: str) -> None:
        self._hardware_error("Light 2", detail)

    @pyqtSlot(str)
    def _camera_exposure_error(self, detail: str) -> None:
        self._hardware_error("Camera exposure", detail)

    @pyqtSlot(str)
    def _camera_error(self, detail: str) -> None:
        self._hardware_error("Camera", detail)

    def _camera_state_changed(self, state: ConnectionState, detail: str) -> None:
        self.camera_status.setText(f"Camera: {state} {detail}")
        if state is not ConnectionState.CONNECTED:
            self.exposure_apply_timer.stop()
            self.exposure_slider.setEnabled(False)
            self._restore_camera_exposure = True
            if state in {ConnectionState.DISCONNECTED, ConnectionState.ERROR}:
                self.camera_fps.setText("FPS: –")

    @staticmethod
    def _slider_to_exposure(position: int, minimum: float, maximum: float) -> float:
        minimum = max(1.0, float(minimum))
        maximum = max(minimum, float(maximum))
        if maximum == minimum:
            return minimum
        fraction = max(0.0, min(1.0, position / EXPOSURE_SLIDER_STEPS))
        return minimum * math.pow(maximum / minimum, fraction)

    @staticmethod
    def _exposure_to_slider(exposure: float, minimum: float, maximum: float) -> int:
        minimum = max(1.0, float(minimum))
        maximum = max(minimum, float(maximum))
        exposure = max(minimum, min(maximum, float(exposure)))
        if maximum == minimum:
            return 0
        fraction = math.log(exposure / minimum) / math.log(maximum / minimum)
        return round(fraction * EXPOSURE_SLIDER_STEPS)

    @staticmethod
    def _format_exposure(exposure_time_us: float | None) -> str:
        if exposure_time_us is None or not math.isfinite(exposure_time_us):
            return "– µs"
        return f"{exposure_time_us:,.0f} µs".replace(",", " ")

    def _camera_status_changed(self, status: CameraStatus) -> None:
        self._camera_status_data = status
        camera_minimum = max(1.0, float(status.exposure_min_us or 1.0))
        camera_maximum = max(
            camera_minimum, float(status.exposure_max_us or camera_minimum)
        )
        minimum = max(CAMERA_EXPOSURE_MIN_US, camera_minimum)
        maximum = min(CAMERA_EXPOSURE_MAX_US, camera_maximum)
        range_supported = minimum <= maximum
        if range_supported:
            self._exposure_min_us = minimum
            self._exposure_max_us = maximum
        can_control = (
            range_supported
            and self.camera.state is ConnectionState.CONNECTED
            and status.exposure_writable
        )
        self.exposure_slider.setEnabled(can_control)

        restoring = can_control and self._restore_camera_exposure
        if restoring:
            desired = max(
                minimum,
                min(maximum, float(self.settings.camera_exposure_time_us)),
            )
            self._updating_exposure_ui = True
            self.exposure_slider.setValue(
                self._exposure_to_slider(desired, minimum, maximum)
            )
            self._updating_exposure_ui = False
            self.exposure_value.setText(self._format_exposure(desired))
            self._restore_camera_exposure = False
            if status.exposure_time_us is None or not math.isclose(
                status.exposure_time_us, desired, abs_tol=0.5
            ):
                self.camera.set_exposure_time(desired)
        elif (
            status.exposure_time_us is not None
            and not self.exposure_slider.isSliderDown()
            and not self.exposure_apply_timer.isActive()
        ):
            self._updating_exposure_ui = True
            self.exposure_slider.setValue(
                self._exposure_to_slider(status.exposure_time_us, minimum, maximum)
            )
            self._updating_exposure_ui = False
            self.exposure_value.setText(self._format_exposure(status.exposure_time_us))
        elif not restoring:
            self.exposure_value.setText(self._format_exposure(status.exposure_time_us))
        camera_fps = "–" if status.camera_fps is None else f"{status.camera_fps:.1f}"
        self.camera_fps.setText(
            f"FPS: {camera_fps} cam · {status.stream_fps:.1f} raw · {status.preview_fps:.1f} view"
        )

    def _exposure_slider_changed(self, position: int) -> None:
        exposure = self._slider_to_exposure(position, self._exposure_min_us, self._exposure_max_us)
        self.exposure_value.setText(self._format_exposure(exposure))
        if not self._updating_exposure_ui and self.exposure_slider.isEnabled():
            self.exposure_apply_timer.start()

    def _apply_camera_exposure(self) -> None:
        if not self.exposure_slider.isEnabled():
            return
        exposure = self._slider_to_exposure(
            self.exposure_slider.value(),
            self._exposure_min_us,
            self._exposure_max_us,
        )
        self.camera.set_exposure_time(exposure)

    def _camera_exposure_applied(self, exposure_time_us: float) -> None:
        self.exposure_value.setText(self._format_exposure(exposure_time_us))
        self.settings.camera_exposure_time_us = max(
            CAMERA_EXPOSURE_MIN_US,
            min(CAMERA_EXPOSURE_MAX_US, float(exposure_time_us)),
        )
        try:
            self.settings.save()
        except ValueError as exc:
            LOGGER.error("Camera exposure setting could not be saved: %s", exc)

    def open_hardware_settings(self) -> None:
        dialog = HardwareSettingsDialog(self.settings, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            selected = dialog.selected_settings()
            selected.save()
            self.settings = selected
        except Exception as exc:
            QMessageBox.critical(self, "Hardware settings", str(exc))
            return
        QMessageBox.information(
            self,
            "Hardware settings saved",
            "The settings were saved. Please close the application and reopen it from "
            "the desktop shortcut so all hardware adapters use the new values.",
        )

    def new_configuration(self) -> None:
        try:
            part = RoadmapSetupDialog(self).create()
            if part:
                self._load(part)
        except Exception as exc:
            QMessageBox.critical(self, "Configuration", str(exc))

    def open_configuration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Configuration", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            self._load(load_part_definition(Path(path)))
        except RoadmapHashMismatchError as exc:
            answer = QMessageBox.question(
                self,
                "Roadmap changed",
                f"{exc}\n\nDo you want to explicitly re-import the changed roadmap and "
                "open the configuration for review?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    draft = load_part_definition(Path(path), accept_roadmap_change=True)
                    part = RoadmapSetupDialog(self, draft).create()
                    if part:
                        self._load(part)
                except Exception as reimport_error:
                    QMessageBox.critical(self, "Re-import roadmap", str(reimport_error))
        except Exception as exc:
            QMessageBox.critical(self, "Configuration", str(exc))

    def edit_configuration(self) -> None:
        if self.part is None:
            QMessageBox.information(
                self, "Edit configuration", "Please load a configuration first."
            )
            return
        try:
            dialog = (
                RoadmapSetupDialog(self, self.part)
                if self.part.schema_version == 2
                else SetupDialog(self, self.part)
            )
            part = dialog.create()
            if part:
                self._load(part)
        except Exception as exc:
            QMessageBox.critical(self, "Configuration", str(exc))

    def _load(self, part) -> None:
        self.controller.clear_configuration()
        self.part = part
        self._displayed_preflight_checks = None
        self.edit_config_action.setEnabled(True)
        self.edit_config_button.setEnabled(True)
        self._roadmap_mode = part.schema_version == 2
        self.load_yolo_button.setEnabled(True)
        if self._roadmap_mode:
            self._load_roadmap_configuration(part)
            return
        self._roadmap_profiles = {}
        self._machine_parameters_confirmed = True
        self.apply_machine_parameters_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.profile = load_pressure_profile(
            part.transitions[0].pressure_profile, require_transition=False
        )
        try:
            strict_profile = load_pressure_profile(part.transitions[0].pressure_profile)
            self.profile = strict_profile
            self.controller.set_configuration(part, strict_profile)
        except ValueError:
            # Legacy profiles resolve omitted machine fields from the PLC baseline
            # after the first successful ADS connection.
            pass
        self.part_label.setText(
            f"Part: {part.part_name}\n"
            f"YOLO: {part.model_path.name}\n"
            f"Target pose: Pose {part.target_pose}"
            + (
                f" · physical roadmap pose {part.target_roadmap_pose_id}"
                if part.target_roadmap_pose_id is not None
                else ""
            )
        )
        transition = part.transitions[0]
        self.transition_label.setText(
            f"Actuation profile Pose {transition.from_pose} → Pose {transition.to_pose}: "
            f"{self.profile.source_path.name}"
        )
        if part.mesh_path is None:
            self.model_label.setPixmap(QPixmap())
            self.model_label.setText("No 3D model in this YAML")
        else:
            try:
                self.model_label.setText("")
                self.model_label.setPixmap(render_mesh_preview(part.mesh_path))
                self.model_label.setToolTip(str(part.mesh_path))
            except Exception as exc:
                self.model_label.setPixmap(QPixmap())
                self.model_label.setText(f"3D preview unavailable:\n{exc}")
        self.ur_button.setEnabled(self.profile.ur_ry_angle_deg is not None)
        self._set_machine_inputs(
            self.profile.conveyor_speed_mm_per_sec, self.profile.ur_ry_angle_deg
        )
        self.machine_parameter_status.setText("Machine values loaded from the pressure profile.")
        self.load_yolo_model()

    def _load_roadmap_configuration(self, part) -> None:
        """Load a roadmap project and prepare its bounded v1 multi-pose executor."""
        readiness = roadmap_readiness(part)
        self.part_label.setText(
            f"Part: {part.part_name}\n"
            f"Roadmap: {part.roadmap_path.name if part.roadmap_path else '–'}\n"
            f"YOLO: {part.model_path.name}\n"
            f"Target pose: Roadmap pose {part.target_pose}"
        )
        self.transition_label.setText(
            f"Roadmap: {len(part.transitions)} profile-eligible transitions, "
            f"{len(readiness.missing_profile_edge_ids)} profiles missing. "
            f"Reachable: {', '.join(map(str, readiness.reachable_pose_ids))}."
        )
        if part.mesh_path is not None:
            try:
                self.model_label.setText("")
                self.model_label.setPixmap(render_mesh_preview(part.mesh_path))
                self.model_label.setToolTip(str(part.mesh_path))
            except Exception as exc:
                self.model_label.setPixmap(QPixmap())
                self.model_label.setText(f"3D preview unavailable:\n{exc}")
        self.pose_label.setText(
            f"Detected pose: –    Target roadmap pose: {part.target_pose}    Confidence: –"
        )
        self.cycle_status.setText("Roadmap loaded – waiting for hardware preflight")
        self.start_button.setText("Start production run")
        self.stop_button.setEnabled(False)
        self.apply_machine_parameters_button.setEnabled(True)
        self._load_roadmap_profiles(self._pressure_baseline)
        self.load_yolo_model()

    def _set_machine_inputs(self, speed: float, angle: float | None) -> None:
        self._updating_machine_parameters = True
        self.conveyor_speed_input.setValue(speed)
        self.use_ur_angle.setChecked(angle is not None)
        if angle is not None:
            self.ur_angle_input.setValue(angle)
        self.ur_angle_input.setEnabled(self._roadmap_mode and angle is not None)
        self.conveyor_speed_input.setEnabled(self._roadmap_mode)
        self.use_ur_angle.setEnabled(self._roadmap_mode)
        self._updating_machine_parameters = False

    def _load_roadmap_profiles(self, baseline: PressureBaseline | None) -> None:
        if self.part is None or not self._roadmap_mode:
            return
        profiles: dict[str, PressureProfile] = {}
        rows: list[str] = []
        errors: list[str] = []
        for transition in self.part.transitions:
            if transition.pressure_profile is None:
                continue
            try:
                profile = load_pressure_profile(transition.pressure_profile, baseline=baseline)
                profiles[transition.edge_id] = profile
                angle = (
                    "–"
                    if profile.ur_ry_angle_deg is None
                    else f"{profile.ur_ry_angle_deg:.1f}°"
                )
                rows.append(
                    f"{transition.from_pose} → {transition.to_pose}: "
                    f"UR {angle}, conveyor {profile.conveyor_speed_mm_per_sec:g} mm/s "
                    f"({profile.source_path.name})"
                )
            except Exception as exc:
                errors.append(
                    f"{transition.from_pose} → {transition.to_pose} "
                    f"({transition.pressure_profile.name}): {exc}"
                )
        self._roadmap_profiles = profiles
        self._machine_parameters_confirmed = False
        if errors:
            self._profile_parameter_details = ""
            self.profile = None
            self.machine_parameter_status.setStyleSheet("color:#b91c1c;font-weight:bold")
            self.machine_parameter_status.setText(
                "Invalid transition profile:\n" + "\n".join(errors)
            )
            return
        if not profiles:
            self._profile_parameter_details = ""
            self.profile = None
            self.machine_parameter_status.setStyleSheet("color:#b91c1c;font-weight:bold")
            self.machine_parameter_status.setText(
                "No pressure profile is assigned. At least one reachable transition is required."
            )
            return
        comparison = compare_machine_parameters(tuple(profiles.values()))
        speed = comparison.common_conveyor_speed_mm_per_sec
        angle = comparison.common_ur_angle_deg
        fallback_speed = comparison.conveyor_speeds_mm_per_sec[0]
        fallback_angle = next(
            (value for value in comparison.ur_angles_deg if value is not None), None
        )
        selected_angle = angle if not comparison.ur_angle_conflict else fallback_angle
        self._set_machine_inputs(speed or fallback_speed, selected_angle)
        profile_text = "\n".join(rows)
        self._profile_parameter_details = profile_text
        if comparison.ur_angle_conflict or comparison.conveyor_speed_conflict:
            conflicts = []
            if comparison.ur_angle_conflict:
                conflicts.append("UR Ry angles")
            if comparison.conveyor_speed_conflict:
                conflicts.append("conveyor speeds")
            self.machine_parameter_status.setStyleSheet("color:#b45309;font-weight:bold")
            self.machine_parameter_status.setText(
                f"Profiles disagree on {' and '.join(conflicts)}. Select the values above and "
                f"press 'Use machine parameters'.\n{profile_text}"
            )
            self.controller.clear_configuration()
            return
        self.machine_parameter_status.setStyleSheet("color:#166534")
        self.machine_parameter_status.setText(
            "All assigned profiles use the same machine parameters; values were loaded "
            f"automatically.\n{profile_text}"
        )
        self.apply_machine_parameters(show_error=False)

    def _machine_parameters_edited(self, _value=None) -> None:
        self.ur_angle_input.setEnabled(self._roadmap_mode and self.use_ur_angle.isChecked())
        if self._updating_machine_parameters or not self._roadmap_mode:
            return
        if self.controller.state not in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
            BatchState.COMPLETE,
            BatchState.FAULT,
        }:
            return
        self._machine_parameters_confirmed = False
        self.machine_parameter_status.setStyleSheet("color:#b45309;font-weight:bold")
        self.machine_parameter_status.setText(
            "Machine values changed. Press 'Use machine parameters' before starting."
        )

    def apply_machine_parameters(self, *, show_error: bool = True) -> None:
        if self.part is None or not self._roadmap_mode:
            return
        try:
            angle = self.ur_angle_input.value() if self.use_ur_angle.isChecked() else None
            speed = self.conveyor_speed_input.value()
            self._machine_parameters_confirmed = True
            self.controller.set_roadmap_configuration(
                self.part,
                self._roadmap_profiles,
                conveyor_speed_mm_per_sec=speed,
                ur_ry_angle_deg=angle,
            )
            self.profile = self.controller.profile
            self.ur_button.setEnabled(angle is not None)
            self.machine_parameter_status.setStyleSheet("color:#166534;font-weight:bold")
            details = (
                f"\n{self._profile_parameter_details}"
                if self._profile_parameter_details
                else ""
            )
            self.machine_parameter_status.setText(
                f"Machine parameters confirmed: conveyor {speed:g} mm/s, "
                f"UR Ry {'not used' if angle is None else f'{angle:.1f}°'}."
                + details
            )
        except Exception as exc:
            self._machine_parameters_confirmed = False
            self.machine_parameter_status.setStyleSheet("color:#b91c1c;font-weight:bold")
            self.machine_parameter_status.setText(str(exc))
            if show_error:
                QMessageBox.warning(self, "Machine parameters", str(exc))

    def _validate_roadmap_paths(self, speed: float, angle: float | None) -> None:
        assert self.part is not None
        valid_paths = 0
        ambiguous_paths: list[str] = []
        resolver = TransitionResolver(self.part)
        for pose in self.part.poses:
            if pose.id == self.part.target_pose:
                continue
            try:
                path = resolver.plan(pose.id, max_transitions=2)
            except ValueError as exc:
                if "ambiguous" in str(exc).casefold() or "no unique" in str(exc).casefold():
                    ambiguous_paths.append(str(exc))
                continue
            profiles = tuple(self._roadmap_profiles[transition.edge_id] for transition in path)
            compose_pressure_profiles(
                profiles,
                conveyor_speed_mm_per_sec=speed,
                ur_ry_angle_deg=angle,
            )
            valid_paths += 1
        if ambiguous_paths:
            raise ValueError("\n".join(ambiguous_paths))
        if valid_paths == 0:
            raise ValueError(
                "No robust start pose has a unique profiled path to the target with at most "
                "one intermediate pose."
            )

    def _baseline_ready(self, baseline: PressureBaseline) -> None:
        self._pressure_baseline = baseline
        if self.part is None:
            return
        if self._roadmap_mode:
            self._load_roadmap_profiles(baseline)
            return
        try:
            self.profile = load_pressure_profile(
                self.part.transitions[0].pressure_profile, baseline=baseline
            )
            self.controller.set_configuration(self.part, self.profile)
        except Exception as exc:
            QMessageBox.critical(self, "Pressure profile", str(exc))

    def connect_all(self) -> None:
        self.pressure.connect_device()
        self.camera.connect_device()
        if self._light_connect_task is None or self._light_connect_task.done():
            self._light_connect_task = asyncio.get_running_loop().create_task(
                self._connect_lights_sequentially()
            )
            self._light_connect_task.add_done_callback(self._light_connection_finished)

    @staticmethod
    def _light_connection_finished(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("Unexpected error in the serial light connection task")

    def disconnect_all(self) -> None:
        if self._light_connect_task is not None and not self._light_connect_task.done():
            self._light_connect_task.cancel()
        self._light_connect_task = None
        self.light1.disconnect_device()
        self.light2.disconnect_device()
        self.camera.disconnect_device()
        self.pressure.disconnect_device()
        self.light_panel1.confirm.setChecked(False)
        self.light_panel2.confirm.setChecked(False)
        self.controller.set_camera_fresh(False)
        self.controller.set_lights_ready(False, False)

    async def _connect_lights_sequentially(self) -> None:
        # WinRT/Bleak discovery and GATT connection are not reliably re-entrant.
        # Serial connection also guarantees that panel 2 can exclude panel 1's
        # freshly discovered address, even when stored BLE addresses are stale.
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Connecting components …")
        try:
            for attempt in range(2):
                if not self.light1.status.connected:
                    await self.light1.connect_async()
                await asyncio.sleep(0.5)
                if not self.light2.status.connected:
                    await self.light2.connect_async()
                if self.light1.status.connected and self.light2.status.connected:
                    return
                if attempt == 0:
                    await asyncio.sleep(1.0)
        finally:
            self.connect_button.setText("Connect all components")
            self.connect_button.setEnabled(True)

    def _camera_frame(self, frame: CameraFrame) -> None:
        self._last_camera_frame = frame.timestamp
        if self.inference:
            self.inference.submit(frame.image, frame.timestamp)
        else:
            # A live preview is useful while setting up hardware, before a part/model
            # configuration has been selected.
            self._show_image(frame.image)

    def _update_camera_freshness(self) -> None:
        self.controller.set_camera_fresh(time.time() - self._last_camera_frame <= 1.0)

    def _handoff_line_changed(self, value: int) -> None:
        self.handoff_value.setText(f"{value} %")
        try:
            self.controller.set_handoff_line_ratio(value / 100.0)
        except RuntimeError:
            return
        self.settings.handoff_line_percent = value
        try:
            self.settings.save()
        except ValueError as exc:
            LOGGER.error("Handoff-line setting could not be saved: %s", exc)

    def _inference_frame(self, frame: InferenceFrame) -> None:
        update = self.controller.accept_inference(frame)
        tracks = () if update is None else update.tracks
        self._show_image(frame.image)
        leftmost = next((track for track in tracks if track.leftmost), None)
        if leftmost is None:
            detected = "none"
        elif leftmost.confirmed_pose_id is not None:
            detected = f"Track {leftmost.track_id}: Pose {leftmost.confirmed_pose_id} locked"
        else:
            detected = f"Track {leftmost.track_id}: consensus {leftmost.pose_streak}/5"
        self.pose_label.setText(
            f"Leftmost: {detected}    Target pose: "
            f"{self.part.target_pose if self.part else '–'}"
        )

    @pyqtSlot(object)
    def _inference_model_ready(self, details: object) -> None:
        if isinstance(details, dict):
            self.yolo_status.setText(
                f"YOLO ready · {details.get('device', '–')} · "
                f"{len(details.get('names', {}))} classes"
            )
        else:
            self.yolo_status.setText("YOLO ready")
        self.load_yolo_button.setEnabled(True)
        self.controller.set_model_ready(True)

    @pyqtSlot(str)
    def _inference_status_changed(self, status: str) -> None:
        self.yolo_status.setText(f"YOLO: {status}")

    @pyqtSlot(str)
    def _inference_error(self, error: str) -> None:
        self.yolo_status.setText(error)
        self.yolo_status.setStyleSheet("color:#b91c1c;font-weight:bold")
        self.load_yolo_button.setEnabled(True)
        self.controller.set_model_ready(False)
        LOGGER.error(error)

    def load_yolo_model(self) -> None:
        if self.part is None or self._shutting_down:
            return
        # Invalidate readiness before touching the old worker. A stopped or
        # retiring model must never leave Start enabled with a stale ready flag.
        self.controller.set_model_ready(False)
        self.load_yolo_button.setEnabled(False)
        if self.inference is not None and self.inference.isRunning():
            if not self._yolo_reload_pending:
                self._yolo_reload_pending = True
                self._disconnect_inference_output(self.inference)
                self.inference.finished.connect(self._inference_stopped_for_reload)
                self.inference.request_stop()
            self.yolo_status.setStyleSheet("")
            self.yolo_status.setText("YOLO: stopping previous model …")
            return
        if self.inference is not None:
            self.inference = None
        self._start_yolo_worker()

    def _disconnect_inference_output(self, worker: InferenceWorker) -> None:
        for signal, slot in (
            (worker.frame_ready, self._inference_frame),
            (worker.model_ready, self._inference_model_ready),
            (worker.status_changed, self._inference_status_changed),
            (worker.error, self._inference_error),
        ):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)

    @pyqtSlot()
    def _inference_stopped_for_reload(self) -> None:
        worker = self.sender()
        if worker is not self.inference:
            return
        with contextlib.suppress(TypeError, RuntimeError):
            worker.finished.disconnect(self._inference_stopped_for_reload)
        self.inference = None
        pending = self._yolo_reload_pending
        self._yolo_reload_pending = False
        if pending and not self._shutting_down:
            QTimer.singleShot(0, self._start_yolo_worker)

    def _start_yolo_worker(self) -> None:
        if self.part is None or self._shutting_down:
            self.load_yolo_button.setEnabled(self.part is not None)
            return
        self.yolo_status.setStyleSheet("")
        self.yolo_status.setText(f"YOLO: loading {self.part.model_path.name} …")
        self.load_yolo_button.setEnabled(False)
        expected = (
            tuple(pose.model_class_id for pose in self.part.poses)
            if self._roadmap_mode
            else None
        )
        class_to_pose = tuple(
            (pose.model_class_id, pose.id) for pose in self.part.poses
        )
        self.inference = InferenceWorker(
            InferenceConfig(
                self.part.model_path,
                max_fps=15.0,
                expected_class_ids=expected,
                class_to_pose=class_to_pose,
            )
        )
        self.inference.frame_ready.connect(self._inference_frame)
        self.inference.model_ready.connect(self._inference_model_ready)
        self.inference.status_changed.connect(self._inference_status_changed)
        self.inference.error.connect(self._inference_error)
        self.inference.start()

    def _show_image(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        qimage = QImage(image.data, width, height, image.strides[0], QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimage.copy())
        self.video.setPixmap(
            pixmap.scaled(
                self.video.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def _lights_changed(self) -> None:
        self.controller.set_light_addresses(self.light1.address, self.light2.address)
        if (
            self.light1.status.connected
            and self.light2.status.connected
            and self.light1.address
            and self.light2.address
            and self.light1.address.casefold() != self.light2.address.casefold()
        ):
            self.settings.light_1_address = self.light1.address
            self.settings.light_2_address = self.light2.address
            try:
                self.settings.save()
            except ValueError as exc:
                LOGGER.error("Discovered light addresses could not be saved: %s", exc)
        ready1 = (
            self.light1.status.connected
            and self.light1.status.power is True
            and self.light1.status.values_are_confirmed_commands
            and self.light_panel1.confirm.isChecked()
        )
        ready2 = (
            self.light2.status.connected
            and self.light2.status.power is True
            and self.light2.status.values_are_confirmed_commands
            and self.light_panel2.confirm.isChecked()
        )
        self.controller.set_lights_ready(ready1, ready2)

    def apply_ur(self) -> None:
        profile = self.controller.profile or self.profile
        if not profile or profile.ur_ry_angle_deg is None:
            return
        self.ur_worker = UrAngleWorker(profile.ur_ry_angle_deg, self)
        self.ur_worker.applied.connect(self._ur_applied)
        self.ur_worker.failed.connect(self._ur_failed)
        self.ur_worker.start()

    @pyqtSlot(float, int)
    def _ur_applied(self, angle: float, _command: int) -> None:
        self.controller.set_ur_applied(angle)

    @pyqtSlot(str)
    def _ur_failed(self, error: str) -> None:
        QMessageBox.critical(self, "UR", error)

    def _preflight(self, checks: dict[str, bool]) -> None:
        if checks == self._displayed_preflight_checks:
            return
        self._displayed_preflight_checks = checks.copy()
        while self.preflight.count():
            item = self.preflight.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for label, passed in checks.items():
            self.preflight.addWidget(QLabel(f"{'✓' if passed else '✗'} {label}"))
        self._preflight_ok = all(checks.values())
        self.start_button.setEnabled(
            self._preflight_ok
            or self.controller.state in {BatchState.COMPLETE, BatchState.FAULT}
        )

    def _batch_counters_changed(self, counters: dict[str, object]) -> None:
        sensor_sequences = "/".join(str(value) for value in counters["sensor_sequences"])
        barrier_states = "".join("1" if value else "0" for value in counters["barrier_states"])
        self.batch_counters.setText(
            f"Snapshot {counters['visible']} · poses {counters['confirmed']} · "
            f"queued {counters['queued']} · LB1 {counters['entered']} · "
            f"completed {counters['completed']} · bypass {counters['bypass']} · "
            f"PLC queue {counters['queue_depth']}/{counters['queue_capacity']} · "
            f"next {counters['next']}\n"
            f"LB sequences 1→8: {sensor_sequences} · normalized states: {barrier_states}"
        )

    def _cycle_state_changed(self, state, detail: str) -> None:
        LOGGER.info("Batch state: %s%s", state, f" – {detail}" if detail else "")
        self.cycle_status.setText(f"{state}: {detail}")
        configuration_editable = state in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
        }
        self.new_config_action.setEnabled(configuration_editable)
        self.open_config_action.setEnabled(configuration_editable)
        self.edit_config_action.setEnabled(configuration_editable and self.part is not None)
        self.new_config_button.setEnabled(configuration_editable)
        self.open_config_button.setEnabled(configuration_editable)
        self.edit_config_button.setEnabled(configuration_editable and self.part is not None)
        self.load_yolo_button.setEnabled(configuration_editable and self.part is not None)
        machine_editable = configuration_editable and self._roadmap_mode
        self.use_ur_angle.setEnabled(machine_editable)
        self.ur_angle_input.setEnabled(machine_editable and self.use_ur_angle.isChecked())
        self.conveyor_speed_input.setEnabled(configuration_editable)
        self.handoff_slider.setEnabled(configuration_editable)
        self.apply_machine_parameters_button.setEnabled(machine_editable)
        terminal = state in {BatchState.COMPLETE, BatchState.FAULT}
        self.start_button.setText(
            "Prepare next production run" if terminal else "Start production run"
        )
        self.start_button.setEnabled(
            terminal
            or (
                state is BatchState.READY
                and self._preflight_ok
            )
        )
        self.stop_button.setEnabled(state is BatchState.STARTING)
        self._update_manual_conveyor_buttons()

    def _production_warning(self, code: str, detail: str) -> None:
        LOGGER.warning("Production warning %s: %s", code, detail)
        self.warning_banner.setText(
            f"⚠ WARNUNG · {time.strftime('%H:%M:%S')} · {code}\n"
            f"{detail}\nDie Produktion läuft weiter."
        )
        self.warning_banner.show()
        self.warning_display_timer.start()

    def _clear_warning_banner(self) -> None:
        self.warning_banner.clear()
        self.warning_banner.hide()

    def _start(self) -> None:
        try:
            if self.controller.state in {BatchState.COMPLETE, BatchState.FAULT}:
                self.controller.prepare_next_cycle()
                return
            if self._roadmap_mode and not self._machine_parameters_confirmed:
                self.apply_machine_parameters(show_error=False)
                if not self._machine_parameters_confirmed:
                    raise RuntimeError(
                        "The currently entered conveyor/UR values could not be applied."
                    )
            self.controller.start_run()
        except Exception as exc:
            QMessageBox.warning(self, "Production run", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._shutting_down = True
        self._yolo_reload_pending = False
        self.freshness_timer.stop()
        self.exposure_apply_timer.stop()
        self.warning_display_timer.stop()
        super().closeEvent(event)

    async def shutdown_async(self) -> None:
        self._shutting_down = True
        self._yolo_reload_pending = False
        self.freshness_timer.stop()
        self.exposure_apply_timer.stop()
        self.warning_display_timer.stop()
        if self._light_connect_task and not self._light_connect_task.done():
            self._light_connect_task.cancel()
            done, pending = await asyncio.wait({self._light_connect_task}, timeout=1.0)
            if pending:
                LOGGER.error("BLE connection task did not stop within one second")
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()
        if self.inference:
            self.inference.stop()
        self.camera.shutdown()
        await self.light1.shutdown()
        await self.light2.shutdown()
        self.pressure.shutdown()
