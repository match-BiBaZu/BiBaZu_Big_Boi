from __future__ import annotations

import json
from pathlib import Path

import pytest

from bibazu_reorientation.models import ConveyorCalibration, PressureBaseline
from bibazu_reorientation.profiles import build_write_plan, load_pressure_profile


def payload(version: int = 8) -> dict:
    return {
        "version": version,
        "conveyor_enabled": True,
        "conveyor_reverse": False,
        "conveyor_speed_mm_per_sec": 100.0,
        "conveyor_max_speed_mm_per_sec": 1000.0,
        "conveyor_calibration": {
            "marker_distance_mm": 315.0,
            "mm_per_full_step": 0.3296,
            "valid": True,
        },
        "arrays": [
            {
                "index": 1,
                "enabled": True,
                "nozzles_enabled": [True],
                "pressure_mbar": 3000,
                "delay_ms": 20,
                "pulse_duration_ms": 100,
                "offset_mm": 12.0,
            }
        ],
    }


@pytest.mark.parametrize("version", range(1, 9))
def test_loads_versions_1_to_8(tmp_path: Path, version: int) -> None:
    source = tmp_path / f"v{version}.json"
    source.write_text(json.dumps(payload(version)), encoding="utf-8")
    profile = load_pressure_profile(source)
    assert profile.source_version == version
    assert profile.arrays[0].nozzles_enabled == (True, False, False, False, False, False)
    assert not profile.arrays[1].enabled


def test_legacy_machine_values_resolve_from_baseline(tmp_path: Path) -> None:
    source = tmp_path / "v1.json"
    source.write_text(
        json.dumps({"version": 1, "arrays": [payload()["arrays"][0]]}), encoding="utf-8"
    )
    baseline = PressureBaseline(
        conveyor_enabled=True,
        conveyor_speed_mm_per_sec=80.0,
        conveyor_calibration=ConveyorCalibration(315.0, 0.3296, True),
    )
    profile = load_pressure_profile(source, baseline)
    assert profile.conveyor_speed_mm_per_sec == 80.0


def test_profile_selection_does_not_create_motion_write(tmp_path: Path) -> None:
    source = tmp_path / "profile.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    plan = build_write_plan(load_pressure_profile(source), actuate=True)
    assert plan.safe_stop["MAIN.GuiConveyorEnabled"] is False
    assert "MAIN.GuiConveyorEnabled" not in plan.configuration
    assert plan.enables["MAIN.GuiConveyorEnabled"] is True
    assert plan.expected_array_mask == 1


def test_pass_through_has_zero_array_mask(tmp_path: Path) -> None:
    source = tmp_path / "profile.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    plan = build_write_plan(load_pressure_profile(source), actuate=False)
    assert plan.expected_array_mask == 0
    assert not any(plan.enables[f"MAIN.GuiArrayEnabled{i}"] for i in range(1, 5))


def test_reverse_and_empty_profile_are_rejected(tmp_path: Path) -> None:
    data = payload()
    data["conveyor_reverse"] = True
    source = tmp_path / "bad.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Rückwärtsfahrt"):
        load_pressure_profile(source)
