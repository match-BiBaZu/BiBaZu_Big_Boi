from __future__ import annotations

import asyncio
import sys

import qasync
from PyQt6.QtWidgets import QApplication

from bibazu_reorientation.logging_setup import configure_logging
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow


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
    app = QApplication(sys.argv)
    app.setApplicationName("BiBaZu Reorientation Control")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        return loop.run_until_complete(_run(app))


if __name__ == "__main__":
    raise SystemExit(main())
