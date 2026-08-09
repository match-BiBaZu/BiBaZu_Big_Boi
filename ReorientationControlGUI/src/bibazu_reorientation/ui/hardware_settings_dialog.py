from __future__ import annotations

from dataclasses import replace

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from bibazu_reorientation.settings import AppSettings


class HardwareSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Hardware-Einstellungen")
        self.setMinimumWidth(680)
        self.camera_ip = QLineEdit(settings.camera_ip)
        self.camera_serial = QLineEdit(settings.camera_serial)
        self.cti_path = QLineEdit(settings.cti_path)
        self.plc_ip = QLineEdit(settings.plc_ip)
        self.ams_net_id = QLineEdit(settings.plc_ams_net_id)
        self.plc_port = QSpinBox()
        self.plc_port.setRange(1, 65535)
        self.plc_port.setValue(settings.plc_port)
        self.light_1 = QLineEdit(settings.light_1_address)
        self.light_2 = QLineEdit(settings.light_2_address)

        form = QFormLayout()
        form.addRow("Baumer Kamera-IP", self.camera_ip)
        form.addRow("Baumer Seriennummer (optional)", self.camera_serial)
        form.addRow("Baumer GenTL/CTI", self._cti_row())
        form.addRow("SPS-IP", self.plc_ip)
        form.addRow("SPS AMS-Net-ID", self.ams_net_id)
        form.addRow("SPS ADS-Port", self.plc_port)
        form.addRow("Neewer-Leuchte 1 Adresse", self.light_1)
        form.addRow("Neewer-Leuchte 2 Adresse", self.light_2)

        hint = QLabel(
            "Leuchtenadressen dürfen zunächst leer bleiben. Beim nächsten Verbinden "
            "werden dann zwei unterschiedliche Panels nacheinander gesucht und ihre "
            "Adressen gespeichert. Für eine feste Zuordnung können BLE-Adressen hier "
            "direkt eingetragen werden."
        )
        hint.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _cti_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(self.cti_path)
        browse = QPushButton("…")
        browse.clicked.connect(self._browse_cti)
        row.addWidget(browse)
        return row

    def _browse_cti(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Baumer GenTL-Producer auswählen", self.cti_path.text(), "GenTL (*.cti)"
        )
        if path:
            self.cti_path.setText(path)

    def selected_settings(self) -> AppSettings:
        return replace(
            self._settings,
            camera_ip=self.camera_ip.text(),
            camera_serial=self.camera_serial.text(),
            cti_path=self.cti_path.text(),
            plc_ip=self.plc_ip.text(),
            plc_ams_net_id=self.ams_net_id.text(),
            plc_port=self.plc_port.value(),
            light_1_address=self.light_1.text(),
            light_2_address=self.light_2.text(),
        ).validated()
