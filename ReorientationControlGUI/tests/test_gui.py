from __future__ import annotations

from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow


def test_main_window_offscreen_smoke(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    assert window.windowTitle() == "BiBaZu Reorientation Control"
    assert window.start_button.text() == "Zyklus starten"
    assert window.stop_button.text() == "STOPP"
    window.camera.shutdown()
    window.pressure.shutdown()
