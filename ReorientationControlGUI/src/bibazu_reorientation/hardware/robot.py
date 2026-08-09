from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal


class UrAngleWorker(QThread):
    applied = pyqtSignal(float, int)
    failed = pyqtSignal(str)

    def __init__(self, angle_deg: float, parent: Any = None) -> None:
        super().__init__(parent)
        self.angle_deg = angle_deg

    def run(self) -> None:
        try:
            csvsaver = Path(__file__).resolve().parents[4] / "CSVSaver"
            if str(csvsaver) not in sys.path:
                sys.path.insert(0, str(csvsaver))
            from ur_angle_control import UrAngleClient

            result = UrAngleClient().apply_angle(self.angle_deg)
            self.applied.emit(float(result["angle_deg"]), int(result["command"]))
        except Exception as exc:
            self.failed.emit(str(exc))
