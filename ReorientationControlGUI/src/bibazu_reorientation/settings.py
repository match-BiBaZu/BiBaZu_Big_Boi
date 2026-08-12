from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from ipaddress import ip_address
from pathlib import Path

from PyQt6.QtCore import QSettings

DEFAULT_PLC_IP = "192.168.0.23"
LEGACY_PLC_IPS = {"192.168.10.23"}
CAMERA_EXPOSURE_MIN_US = 1_000.0
CAMERA_EXPOSURE_MAX_US = 20_000.0
CAMERA_EXPOSURE_DEFAULT_US = 4_000.0


def _migrated_plc_ip(value: str) -> str:
    normalized = value.strip()
    return DEFAULT_PLC_IP if normalized in LEGACY_PLC_IPS else normalized


@dataclass(slots=True)
class AppSettings:
    camera_ip: str = "169.254.117.70"
    camera_serial: str = ""
    cti_path: str = r"C:\Program Files\Baumer Camera Explorer\bgapi2_gige.cti"
    preview_fps: float = 15.0
    camera_exposure_time_us: float = CAMERA_EXPOSURE_DEFAULT_US
    handoff_line_percent: int = 30
    light_1_address: str = ""
    light_2_address: str = ""
    plc_ip: str = DEFAULT_PLC_IP
    plc_ams_net_id: str = "10.145.4.14.1.1"
    plc_port: int = 851
    cycle_timeout_s: float = 60.0
    drain_timeout_s: float = 35.0

    def validated(self) -> AppSettings:
        ip_address(self.camera_ip.strip())
        ip_address(self.plc_ip.strip())
        parts = self.plc_ams_net_id.strip().split(".")
        if len(parts) != 6 or any(
            not part.isdigit() or not 0 <= int(part) <= 255 for part in parts
        ):
            raise ValueError("The AMS Net ID must contain six numbers between 0 and 255")
        if not 1 <= self.plc_port <= 65535:
            raise ValueError("The ADS port must be between 1 and 65535")
        if not 1.0 <= float(self.preview_fps) <= 60.0:
            raise ValueError("Camera preview must be between 1 and 60 FPS")
        if not CAMERA_EXPOSURE_MIN_US <= float(
            self.camera_exposure_time_us
        ) <= CAMERA_EXPOSURE_MAX_US:
            raise ValueError("Camera exposure must be between 1000 and 20000 microseconds")
        if not 5 <= int(self.handoff_line_percent) <= 80:
            raise ValueError("Handoff line must be between 5 and 80 percent")
        cti = Path(self.cti_path.strip()).expanduser()
        if not cti.is_file():
            raise ValueError(f"Baumer CTI not found: {cti}")
        if self.light_1_address and self.light_1_address == self.light_2_address:
            raise ValueError("The two lights must use different addresses")
        return replace(
            self,
            camera_ip=self.camera_ip.strip(),
            camera_serial=self.camera_serial.strip(),
            cti_path=str(cti.resolve()),
            camera_exposure_time_us=float(self.camera_exposure_time_us),
            light_1_address=self.light_1_address.strip(),
            light_2_address=self.light_2_address.strip(),
            plc_ip=_migrated_plc_ip(self.plc_ip),
            plc_ams_net_id=self.plc_ams_net_id.strip(),
        )

    @classmethod
    def load(cls) -> AppSettings:
        q = QSettings("LeibnizUniversitaetHannover", "BiBaZuReorientationControl")
        result = cls()
        if not q.allKeys():
            legacy = QSettings("LeibnizUniversitaetHannover", "AutomatedImageCapture")
            result.camera_ip = str(legacy.value("camera/ip", result.camera_ip))
            result.camera_serial = str(legacy.value("camera/serial", result.camera_serial))
            result.cti_path = str(legacy.value("camera/cti_path", result.cti_path))
            result.light_1_address = str(legacy.value("light/address", result.light_1_address))
            result.light_2_address = str(legacy.value("light_2/address", result.light_2_address))
            result.plc_ip = str(legacy.value("plc/ip", result.plc_ip))
            result.plc_ams_net_id = str(legacy.value("plc/ams_net_id", result.plc_ams_net_id))
            result.plc_port = int(legacy.value("plc/port", result.plc_port))
            result.plc_ip = _migrated_plc_ip(result.plc_ip)
            return result
        for name, default in asdict(result).items():
            value = q.value(name, default)
            setattr(result, name, type(default)(value))
        result.camera_exposure_time_us = max(
            CAMERA_EXPOSURE_MIN_US,
            min(CAMERA_EXPOSURE_MAX_US, result.camera_exposure_time_us),
        )
        result.plc_ip = _migrated_plc_ip(result.plc_ip)
        return result

    def save(self) -> None:
        validated = self.validated()
        q = QSettings("LeibnizUniversitaetHannover", "BiBaZuReorientationControl")
        for name, value in asdict(validated).items():
            q.setValue(name, value)
        q.sync()


def default_run_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "BiBaZuReorientationControl" / "runs"
