from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bibazu_reorientation import app
from bibazu_reorientation.settings import AppSettings


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in self.callbacks:
            callback()


@pytest.mark.asyncio
async def test_window_close_finishes_hardware_cleanup_without_waiting_for_qt_quit(monkeypatch):
    events = []

    class Window:
        def __init__(self, _settings):
            self.shutdown_requested = Signal()

        def show(self):
            events.append("window closed")
            self.shutdown_requested.emit()

        async def shutdown_async(self):
            await asyncio.sleep(0)
            events.append("hardware cleanup finished")

    monkeypatch.setattr(app, "MainWindow", Window)
    monkeypatch.setattr(AppSettings, "load", lambda: AppSettings())
    result = await asyncio.wait_for(app._run(SimpleNamespace(aboutToQuit=Signal())), timeout=1)
    assert result == 0
    assert events == ["window closed", "hardware cleanup finished"]
