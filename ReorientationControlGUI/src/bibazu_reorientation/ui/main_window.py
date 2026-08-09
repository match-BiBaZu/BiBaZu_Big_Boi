from __future__ import annotations

import asyncio
import contextlib
import logging
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

from bibazu_reorientation.config import load_part_definition
from bibazu_reorientation.controller import ReorientationController
from bibazu_reorientation.hardware.camera import CameraAdapter
from bibazu_reorientation.hardware.light import LightAdapter
from bibazu_reorientation.hardware.pressure import PressureAdapter
from bibazu_reorientation.hardware.robot import UrAngleWorker
from bibazu_reorientation.inference import InferenceConfig, InferenceWorker
from bibazu_reorientation.mesh_preview import render_mesh_preview
from bibazu_reorientation.models import CameraFrame, CycleState, InferenceFrame, PressureBaseline
from bibazu_reorientation.profiles import load_pressure_profile
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.hardware_settings_dialog import HardwareSettingsDialog
from bibazu_reorientation.ui.setup_dialog import SetupDialog

LOGGER = logging.getLogger(__name__)


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
        self.light2 = LightAdapter(
            "Neewer-Leuchte 2",
            settings.light_2_address,
            excluded_addresses=lambda: {self.light1.address},
        )
        self.controller = ReorientationController(self.pressure)
        self.inference: InferenceWorker | None = None
        self.part = None
        self.profile = None
        self.ur_worker: UrAngleWorker | None = None
        self._last_camera_frame = 0.0
        self._preflight_ok = False
        self._light_connect_task: asyncio.Task[None] | None = None
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
        self.new_config_action = QAction(
            "Neue Bauteilkonfiguration (Modell + Profil) …", self
        )
        self.new_config_action.triggered.connect(self.new_configuration)
        menu.addAction(self.new_config_action)
        self.open_config_action = QAction("Bauteilkonfiguration öffnen …", self)
        self.open_config_action.triggered.connect(self.open_configuration)
        menu.addAction(self.open_config_action)
        self.edit_config_action = QAction("Geladene Bauteilkonfiguration bearbeiten …", self)
        self.edit_config_action.triggered.connect(self.edit_configuration)
        self.edit_config_action.setEnabled(False)
        menu.addAction(self.edit_config_action)
        menu.addSeparator()
        hardware_settings = QAction("Hardware-Einstellungen …", self)
        hardware_settings.triggered.connect(self.open_hardware_settings)
        menu.addAction(hardware_settings)
        self.part_label = QLabel("Keine Konfiguration")
        self.transition_label = QLabel("Aktuierungsprofil: –")
        self.model_label = QLabel("Kein 3D-Modell gewählt")
        self.model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_label.setFixedSize(250, 175)
        self.model_label.setStyleSheet(
            "background:#111827;color:#94a3b8;border:1px solid #334155;border-radius:8px"
        )
        config_buttons = QHBoxLayout()
        self.new_config_button = QPushButton("Neue Konfiguration")
        self.new_config_button.clicked.connect(self.new_configuration)
        self.open_config_button = QPushButton("Konfiguration öffnen")
        self.open_config_button.clicked.connect(self.open_configuration)
        self.edit_config_button = QPushButton("Konfiguration bearbeiten")
        self.edit_config_button.clicked.connect(self.edit_configuration)
        self.edit_config_button.setEnabled(False)
        config_buttons.addWidget(self.new_config_button)
        config_buttons.addWidget(self.open_config_button)
        config_buttons.addWidget(self.edit_config_button)
        self.video = QLabel("Kamera nicht verbunden")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(760, 520)
        self.video.setStyleSheet("background:#111827;color:#94a3b8;border-radius:8px")
        self.pose_label = QLabel("Erkannte Pose: –    Zielpose: 1    Konfidenz: –")
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
        self.pressure.connection_changed.connect(self._plc_connection_changed)
        self.pressure.baseline_ready.connect(self._baseline_ready)
        self.camera.state_changed.connect(
            lambda state, detail: self.camera_status.setText(f"Kamera: {state} {detail}")
        )
        self.camera.frame_ready.connect(self._camera_frame)
        self.camera.error.connect(lambda detail: self._hardware_error("Kamera", detail))
        self.controller.preflight_changed.connect(self._preflight)
        self.controller.state_changed.connect(self._cycle_state_changed)
        self.light_panel1.confirm.toggled.connect(self._lights_changed)
        self.light_panel2.confirm.toggled.connect(self._lights_changed)
        self.light1.status_changed.connect(lambda _: self._lights_changed())
        self.light2.status_changed.connect(lambda _: self._lights_changed())
        self.light1.error.connect(lambda detail: self._hardware_error("Leuchte 1", detail))
        self.light2.error.connect(lambda detail: self._hardware_error("Leuchte 2", detail))

    def _plc_connection_changed(self, connected: bool, detail: str) -> None:
        self.plc_status.setText(f"SPS: {'verbunden' if connected else 'getrennt'} – {detail}")
        if connected:
            LOGGER.info(
                "ADS connected: %s / %s", self.settings.plc_ams_net_id, self.settings.plc_ip
            )
        else:
            LOGGER.error("ADS connection failed/lost: %s", detail)

    @staticmethod
    def _hardware_error(component: str, detail: str) -> None:
        LOGGER.error("%s: %s", component, detail)

    def open_hardware_settings(self) -> None:
        dialog = HardwareSettingsDialog(self.settings, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            selected = dialog.selected_settings()
            selected.save()
            self.settings = selected
        except Exception as exc:
            QMessageBox.critical(self, "Hardware-Einstellungen", str(exc))
            return
        QMessageBox.information(
            self,
            "Hardware-Einstellungen gespeichert",
            "Die Einstellungen wurden gespeichert. Bitte die Anwendung einmal schließen "
            "und über das Desktop-Symbol neu öffnen, damit alle Hardware-Adapter mit den "
            "neuen Werten erzeugt werden.",
        )

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

    def edit_configuration(self) -> None:
        if self.part is None:
            QMessageBox.information(
                self, "Konfiguration bearbeiten", "Bitte zuerst eine Konfiguration laden."
            )
            return
        try:
            part = SetupDialog(self, self.part).create()
            if part:
                self._load(part)
        except Exception as exc:
            QMessageBox.critical(self, "Konfiguration", str(exc))

    def _load(self, part) -> None:
        self.part = part
        self.edit_config_action.setEnabled(True)
        self.edit_config_button.setEnabled(True)
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
            f"Bauteil: {part.part_name}\n"
            f"YOLO: {part.model_path.name}\n"
            f"Zielpose: Pose {part.target_pose}"
        )
        transition = part.transitions[0]
        self.transition_label.setText(
            f"Aktuierungsprofil Pose {transition.from_pose} → Pose {transition.to_pose}: "
            f"{self.profile.source_path.name}"
        )
        if part.mesh_path is None:
            self.model_label.setPixmap(QPixmap())
            self.model_label.setText("Kein 3D-Modell in dieser YAML")
        else:
            try:
                self.model_label.setText("")
                self.model_label.setPixmap(render_mesh_preview(part.mesh_path))
                self.model_label.setToolTip(str(part.mesh_path))
            except Exception as exc:
                self.model_label.setPixmap(QPixmap())
                self.model_label.setText(f"3D-Vorschau nicht möglich:\n{exc}")
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
        if self._light_connect_task is None or self._light_connect_task.done():
            self._light_connect_task = asyncio.get_running_loop().create_task(
                self._connect_lights_sequentially()
            )

    async def _connect_lights_sequentially(self) -> None:
        first = self.settings.light_1_address.strip()
        second = self.settings.light_2_address.strip()
        if first and second and first.casefold() != second.casefold():
            await asyncio.gather(self.light1.connect_async(), self.light2.connect_async())
        else:
            # During first discovery, connect one after the other so that panel 2 can
            # exclude the address selected for panel 1.
            await self.light1.connect_async()
            await self.light2.connect_async()

    def _camera_frame(self, frame: CameraFrame) -> None:
        self._last_camera_frame = frame.timestamp
        self.controller.set_camera_fresh(time.time() - frame.timestamp <= 1.0)
        if self.inference:
            self.inference.submit(frame.image, frame.timestamp)
        else:
            # A live preview is useful while setting up hardware, before a part/model
            # configuration has been selected.
            self._show_image(frame.image)

    def _inference_frame(self, frame: InferenceFrame) -> None:
        self._show_image(frame.image)
        if len(frame.detections) == 1:
            detection = frame.detections[0]
            self.pose_label.setText(
                f"Erkannte Pose: {detection.class_id + 1}    "
                f"Zielpose: {self.part.target_pose if self.part else '–'}    "
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
        configuration_editable = state in {
            CycleState.NO_CONFIG,
            CycleState.OFFLINE,
            CycleState.READY,
        }
        self.new_config_action.setEnabled(configuration_editable)
        self.open_config_action.setEnabled(configuration_editable)
        self.edit_config_action.setEnabled(configuration_editable and self.part is not None)
        self.new_config_button.setEnabled(configuration_editable)
        self.open_config_button.setEnabled(configuration_editable)
        self.edit_config_button.setEnabled(configuration_editable and self.part is not None)
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
        if self._light_connect_task and not self._light_connect_task.done():
            self._light_connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._light_connect_task
        if self.inference:
            self.inference.stop()
        self.camera.shutdown()
        await self.light1.shutdown()
        await self.light2.shutdown()
        self.pressure.shutdown()
