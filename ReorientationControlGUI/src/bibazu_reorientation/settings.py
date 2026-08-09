from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from PyQt6.QtCore import QSettings


@dataclass(slots=True)
class AppSettings:
    camera_ip: str = "169.254.117.70"
    camera_serial: str = ""
    cti_path: str = r"C:\Program Files\Baumer Camera Explorer\bgapi2_gige.cti"
    preview_fps: float = 15.0
    light_1_address: str = ""
    light_2_address: str = ""
    plc_ip: str = "192.168.10.23"
    plc_ams_net_id: str = "10.145.4.14.1.1"
    plc_port: int = 851
    cycle_timeout_s: float = 60.0
    drain_timeout_s: float = 35.0

    @classmethod
    def load(cls) -> AppSettings:
        q = QSettings("LeibnizUniversitaetHannover", "BiBaZuReorientationControl")
        result = cls()
        for name, default in asdict(result).items():
            value = q.value(name, default)
            setattr(result, name, type(default)(value))
        return result

    def save(self) -> None:
        q = QSettings("LeibnizUniversitaetHannover", "BiBaZuReorientationControl")
        for name, value in asdict(self).items():
            q.setValue(name, value)


def default_run_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "BiBaZuReorientationControl" / "runs"
