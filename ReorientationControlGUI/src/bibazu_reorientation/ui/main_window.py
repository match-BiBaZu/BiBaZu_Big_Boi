from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.config import load_part_definition
from bibazu_reorientation.controller import ReorientationController
from bibazu_reorientation.hardware.camera import CameraAdapter
from bibazu_reorientation.hardware.light import LightAdapter
from bibazu_reorientation.hardware.pressure import PressureAdapter
from bibazu_reorientation.hardware.robot import UrAngleWorker
from bibazu_reorientation.inference import InferenceConfig, InferenceWorker
from bibazu_reorientation.models import CameraFrame, CycleState, InferenceFrame, PressureBaseline
from bibazu_reorientation.profiles import load_pressure_profile
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.setup_dialog import SetupDialog


class LightPanel(QGroupBox):
    def __init__(self, adapter: LightAdapter) -> None:
        super().__init__(adapter.name)
        self.adapter = adapter
        self.status = QLabel("Getrennt")
        self.confirm = QCheckBox("Einstellungen für diesen Zyklus bestätigt")
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
        apply = QPushButton("CCT anwenden")
        apply.clicked.connect(lambda: adapter.set_cct(self.brightness.value(), self.cct.value()))
        power_on = QPushButton("Licht an")
        power_on.clicked.connect(lambda: adapter.set_power(True))
        power_off = QPushButton("Licht aus")
        power_off.clicked.connect(lambda: adapter.set_power(False))
        hsi = QPushButton("HSI anwenden")
        hsi.clicked.connect(
            lambda: adapter.set_hsi(
                self.brightness.value(), self.hue.value(), self.saturation.value()
            )
        )
        form = QFormLayout(self)
        form.addRow("Status", self.status)
        form.addRow("Helligkeit", self.brightness)
        form.addRow("CCT", self.cct)
        form.addRow("Farbton", self.hue)
        form.addRow("Sättigung", self.saturation)
        form.addRow(apply, power_on)
        form.addRow(power_off)
        form.addRow(hsi)
        form.addRow(self.confirm)
        adapter.state_changed.connect(
            lambda state, detail: self.status.setText(f"{state}: {detail}")
        )


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("BiBaZu Reorientation Control")
        self.resize(1450, 900)
        self.pressure = PressureAdapter(settings)
        self.camera = CameraAdapter(settings)
        self.light1 = LightAdapter("Neewer-Leuchte 1", settings.light_1_address)
        self.light2 = LightAdapter("Neewer-Leuchte 2", settings.light_2_address)
        self.controller = ReorientationController(self.pressure)
        self.inference: InferenceWorker | None = None
        self.part = None
        self.profile = None
        self.ur_worker: UrAngleWorker | None = None
        self._last_camera_frame = 0.0
        self._preflight_ok = False
        self._build_ui()
        self._wire()
        self.freshness_timer = QTimer(self)
        self.freshness_timer.setInterval(250)
        self.freshness_timer.timeout.connect(
            lambda: self.controller.set_camera_fresh(time.time() - self._last_camera_frame <= 1.0)
        )
        self.freshness_timer.start()

    def _build_ui(self) -> None:
        menu = self.menuBar().addMenu("Konfiguration")
        new = QAction("Neu …", self)
        new.triggered.connect(self.new_configuration)
        menu.addAction(new)
        load = QAction("Öffnen …", self)
        load.triggered.connect(self.open_configuration)
        menu.addAction(load)
        self.part_label = QLabel("Keine Konfiguration")
        self.transition_label = QLabel("Pflichtprofil: Pose 2 → Pose 1")
        self.video = QLabel("Kamera nicht verbunden")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(760, 520)
        self.video.setStyleSheet("background:#111827;color:#94a3b8;border-radius:8px")
        self.pose_label = QLabel("Erkannte Pose: –    Zielpose: 1    Konfidenz: –")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.part_label)
        left_layout.addWidget(self.transition_label)
        left_layout.addWidget(self.video, 1)
        left_layout.addWidget(self.pose_label)
        self.plc_status = QLabel("SPS: getrennt")
        self.camera_status = QLabel("Kamera: getrennt")
        connect = QPushButton("Alle Komponenten verbinden")
        connect.clicked.connect(self.connect_all)
        hardware = QGroupBox("Hardware")
        hardware_layout = QVBoxLayout(hardware)
        hardware_layout.addWidget(self.plc_status)
        hardware_layout.addWidget(self.camera_status)
        hardware_layout.addWidget(connect)
        self.light_panel1 = LightPanel(self.light1)
        self.light_panel2 = LightPanel(self.light2)
        self.ur_button = QPushButton("UR-Winkel anwenden")
        self.ur_button.clicked.connect(self.apply_ur)
        self.ur_button.setEnabled(False)
        self.preflight = QVBoxLayout()
        preflight_box = QGroupBox("Preflight")
        preflight_box.setLayout(self.preflight)
        self.start_button = QPushButton("Zyklus starten")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("STOPP")
        self.stop_button.setStyleSheet(
            "font-weight:bold;background:#dc2626;color:white;padding:12px"
        )
        self.stop_button.clicked.connect(self.controller.stop)
        self.cycle_status = QLabel("Keine Konfiguration")
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(hardware)
        right_layout.addWidget(self.light_panel1)
        right_layout.addWidget(self.light_panel2)
        right_layout.addWidget(self.ur_button)
        right_layout.addWidget(preflight_box)
        right_layout.addWidget(self.cycle_status)
        right_layout.addWidget(self.start_button)
        right_layout.addWidget(self.stop_button)
        right_layout.addStretch()
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    def _wire(self) -> None:
        self.pressure.connection_changed.connect(
            lambda ok, detail: self.plc_status.setText(
                f"SPS: {'verbunden' if ok else 'getrennt'} – {detail}"
            )
        )
        self.pressure.baseline_ready.connect(self._baseline_ready)
        self.camera.state_changed.connect(
            lambda state, detail: self.camera_status.setText(f"Kamera: {state} {detail}")
        )
        self.camera.frame_ready.connect(self._camera_frame)
        self.controller.preflight_changed.connect(self._preflight)
        self.controller.state_changed.connect(self._cycle_state_changed)
        self.light_panel1.confirm.toggled.connect(self._lights_changed)
        self.light_panel2.confirm.toggled.connect(self._lights_changed)
        self.light1.status_changed.connect(lambda _: self._lights_changed())
        self.light2.status_changed.connect(lambda _: self._lights_changed())

    def new_configuration(self) -> None:
        try:
            part = SetupDialog(self).create()
            if part:
                self._load(part)
        except Exception as exc:
            QMessageBox.critical(self, "Konfiguration", str(exc))

    def open_configuration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Konfiguration", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            self._load(load_part_definition(Path(path)))
        except Exception as exc:
            QMessageBox.critical(self, "Konfiguration", str(exc))

    def _load(self, part) -> None:
        self.part = part
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
        self.part_label.setText(f"Bauteil: {part.part_name}")
        self.transition_label.setText(
            f"Pflichtprofil Pose 2 → Pose 1: {self.profile.source_path.name}"
        )
        self.ur_button.setEnabled(self.profile.ur_ry_angle_deg is not None)
        if self.inference:
            self.inference.stop()
        self.inference = InferenceWorker(InferenceConfig(part.model_path))
        self.inference.frame_ready.connect(self._inference_frame)
        self.inference.model_ready.connect(lambda _: self.controller.set_model_ready(True))
        self.inference.error.connect(lambda error: QMessageBox.critical(self, "YOLO", error))
        self.inference.start()

    def _baseline_ready(self, baseline: PressureBaseline) -> None:
        if self.part is None:
            return
        try:
            self.profile = load_pressure_profile(
                self.part.transitions[0].pressure_profile, baseline=baseline
            )
            self.controller.set_configuration(self.part, self.profile)
        except Exception as exc:
            QMessageBox.critical(self, "Pressure-Profil", str(exc))

    def connect_all(self) -> None:
        self.pressure.connect_device()
        self.camera.connect_device()
        self.light1.connect_device()
        self.light2.connect_device()

    def _camera_frame(self, frame: CameraFrame) -> None:
        self._last_camera_frame = frame.timestamp
        self.controller.set_camera_fresh(time.time() - frame.timestamp <= 1.0)
        if self.inference:
            self.inference.submit(frame.image, frame.timestamp)

    def _inference_frame(self, frame: InferenceFrame) -> None:
        self._show_image(frame.image)
        if len(frame.detections) == 1:
            detection = frame.detections[0]
            self.pose_label.setText(
                f"Erkannte Pose: {detection.class_id + 1}    Zielpose: 1    "
                f"Konfidenz: {detection.confidence:.1%}"
            )
        self.controller.accept_inference(frame)

    def _show_image(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        qimage = QImage(image.data, width, height, image.strides[0], QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimage.copy())
        self.video.setPixmap(
            pixmap.scaled(
                self.video.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _lights_changed(self) -> None:
        self.controller.set_light_addresses(self.light1.address, self.light2.address)
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
        if not self.profile or self.profile.ur_ry_angle_deg is None:
            return
        self.ur_worker = UrAngleWorker(self.profile.ur_ry_angle_deg, self)
        self.ur_worker.applied.connect(lambda angle, _: self.controller.set_ur_applied(angle))
        self.ur_worker.failed.connect(lambda error: QMessageBox.critical(self, "UR", error))
        self.ur_worker.start()

    def _preflight(self, checks: dict[str, bool]) -> None:
        while self.preflight.count():
            item = self.preflight.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for label, passed in checks.items():
            self.preflight.addWidget(QLabel(f"{'✓' if passed else '✗'} {label}"))
        self._preflight_ok = all(checks.values())
        self.start_button.setEnabled(
            self._preflight_ok
            or self.controller.state in {CycleState.COMPLETE, CycleState.ABORTED, CycleState.FAULT}
        )

    def _cycle_state_changed(self, state, detail: str) -> None:
        self.cycle_status.setText(f"{state}: {detail}")
        terminal = state in {CycleState.COMPLETE, CycleState.ABORTED, CycleState.FAULT}
        self.start_button.setText("Neuen Zyklus vorbereiten" if terminal else "Zyklus starten")
        self.start_button.setEnabled(terminal or (state is CycleState.READY and self._preflight_ok))

    def _start(self) -> None:
        try:
            if self.controller.state in {CycleState.COMPLETE, CycleState.ABORTED, CycleState.FAULT}:
                self.controller.prepare_next_cycle()
                return
            self.controller.start_cycle()
        except Exception as exc:
            QMessageBox.warning(self, "Zyklus", str(exc))

    async def shutdown_async(self) -> None:
        if self.inference:
            self.inference.stop()
        self.camera.shutdown()
        await self.light1.shutdown()
        await self.light2.shutdown()
        self.pressure.shutdown()
