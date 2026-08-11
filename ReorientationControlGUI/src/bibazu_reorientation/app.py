from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import qasync
from PyQt6.QtWidgets import QApplication, QMessageBox

from bibazu_reorientation.hardware_lease import PLC_CONTROL_LEASE_NAME, HardwareLease
from bibazu_reorientation.logging_setup import configure_logging
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow

LOGGER = logging.getLogger("uncaught")


def _exception_hook(exc_type: type[BaseException], exc: BaseException, trace: Any) -> None:
    """Keep exceptions raised by Qt callbacks visible without a silent process exit."""
    LOGGER.critical("Unhandled GUI exception", exc_info=(exc_type, exc, trace))


async def _run(app: QApplication) -> int:
    window = MainWindow(AppSettings.load())
    window.show()
    done = asyncio.Event()
    app.aboutToQuit.connect(done.set)
    await done.wait()
    await window.shutdown_async()
    return 0


def main() -> int:
    configure_logging()
    sys.excepthook = _exception_hook
    app = QApplication(sys.argv)
    app.setApplicationName("BiBaZu Reorientation Control")
    plc_lease = HardwareLease.acquire(PLC_CONTROL_LEASE_NAME)
    if plc_lease is None:
        QMessageBox.critical(
            None,
            "PLC control already in use",
            "Pressure Control, Conveyor Setup, or another Reorientation Control "
            "instance is already controlling the PLC. Close the other control "
            "application before starting this one.",
        )
        return 2
    hardware_lease = HardwareLease.acquire()
    if hardware_lease is None:
        QMessageBox.critical(
            None,
            "Hardware already in use",
            "Automated Image Capture or another Reorientation Control instance is "
            "already using the Baumer camera and Neewer panels. Close the other "
            "application before starting this one.",
        )
        plc_lease.close()
        return 2
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        with loop:
            return loop.run_until_complete(_run(app))
    finally:
        hardware_lease.close()
        plc_lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
