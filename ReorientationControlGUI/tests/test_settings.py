from __future__ import annotations

from dataclasses import replace

import pytest

from bibazu_reorientation.settings import AppSettings, _migrated_plc_ip
from bibazu_reorientation.ui.hardware_settings_dialog import HardwareSettingsDialog


def valid_settings(tmp_path) -> AppSettings:
    cti = tmp_path / "baumer.cti"
    cti.write_bytes(b"test")
    return AppSettings(cti_path=str(cti))


def test_hardware_settings_validation(tmp_path) -> None:
    settings = valid_settings(tmp_path).validated()
    assert settings.camera_ip == "169.254.117.70"
    assert settings.camera_exposure_time_us == 4_000.0
    with pytest.raises(ValueError, match="different addresses"):
        replace(settings, light_1_address="AA:BB", light_2_address="AA:BB").validated()
    with pytest.raises(ValueError, match="AMS Net ID"):
        replace(settings, plc_ams_net_id="invalid").validated()
    with pytest.raises(ValueError, match="Camera preview"):
        replace(settings, preview_fps=0).validated()
    with pytest.raises(ValueError, match="Camera exposure"):
        replace(settings, camera_exposure_time_us=999).validated()
    with pytest.raises(ValueError, match="Camera exposure"):
        replace(settings, camera_exposure_time_us=20_001).validated()


def test_legacy_plc_ip_is_migrated() -> None:
    assert _migrated_plc_ip("192.168.10.23") == "192.168.0.23"
    assert _migrated_plc_ip("192.168.0.42") == "192.168.0.42"


def test_production_mode_and_handoff_position_are_persisted(tmp_path, monkeypatch) -> None:
    values = {}

    class MemorySettings:
        def __init__(self, *_args):
            pass

        def allKeys(self):
            return list(values)

        def value(self, key, default):
            return values.get(key, default)

        def setValue(self, key, value):
            values[key] = value

        def sync(self):
            pass

    monkeypatch.setattr("bibazu_reorientation.settings.QSettings", MemorySettings)
    settings = replace(
        valid_settings(tmp_path), handoff_line_percent=45, input_line_percent=75,
        production_mode="continuous"
    )
    settings.save()
    loaded = AppSettings.load()
    assert loaded.handoff_line_percent == 45
    assert loaded.input_line_percent == 75
    assert loaded.production_mode == "continuous"
    with pytest.raises(ValueError, match="Production mode"):
        replace(settings, production_mode="invalid").validated()
    for invalid in (45, 101):
        with pytest.raises(ValueError, match="Input boundary"):
            replace(settings, input_line_percent=invalid).validated()


def test_hardware_dialog_exposes_all_connection_fields(qtbot, tmp_path) -> None:
    dialog = HardwareSettingsDialog(valid_settings(tmp_path))
    qtbot.addWidget(dialog)
    assert dialog.camera_ip.text() == "169.254.117.70"
    assert dialog.plc_ip.text() == "192.168.0.23"
    assert dialog.plc_port.value() == 851
    assert dialog.preview_fps.value() == 15
    assert dialog.light_1.text() == ""
    assert dialog.selected_settings().cti_path.endswith("baumer.cti")
