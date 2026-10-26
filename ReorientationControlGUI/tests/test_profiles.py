from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bibazu_reorientation.models import ConveyorCalibration, PressureBaseline
from bibazu_reorientation.profiles import (
    PROFILE_VERSION_MAX,
    build_write_plan,
    compare_machine_parameters,
    compose_pressure_profiles,
    load_pressure_profile,
)


def payload(version: int = 9) -> dict:
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


@pytest.mark.parametrize("version", range(1, 14))
def test_loads_versions_1_to_13(tmp_path: Path, version: int) -> None:
    source = tmp_path / f"v{version}.json"
    source.write_text(json.dumps(payload(version)), encoding="utf-8")
    profile = load_pressure_profile(source)
    assert profile.source_version == version
    assert profile.arrays[0].nozzles_enabled == (True, False, False, False, False, False)
    assert not profile.arrays[1].enabled


def test_supported_profile_version_matches_pressure_control_exporter() -> None:
    exporter = Path(__file__).parents[2] / "CSVSaver" / "PressureControlGUI.py"
    tree = ast.parse(exporter.read_text(encoding="utf-8"))
    version = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "PROFILE_VERSION"
            for target in node.targets
        )
    )
    assert PROFILE_VERSION_MAX == version


def test_future_profile_versions_are_still_rejected(tmp_path: Path) -> None:
    source = tmp_path / "future-profile.json"
    source.write_text(json.dumps(payload(PROFILE_VERSION_MAX + 1)), encoding="utf-8")
    with pytest.raises(ValueError, match="version must be between 1 and 13"):
        load_pressure_profile(source)


def test_wago_version_13_profile_preserves_array_commands_and_global_machine_values() -> None:
    source = Path(__file__).parent / "fixtures" / "pressure_profile_v13.json"
    original = source.read_bytes()
    calibration = ConveyorCalibration(315.0, 0.0245, True)
    baseline = PressureBaseline(
        conveyor_calibration=calibration,
        force_response_delays_ms=(11.0, 12.0, 13.0, 14.0),
        force_single_nozzle_response_delays_ms=(21.0, 22.0, 23.0, 24.0),
    )
    profile = load_pressure_profile(source, baseline)
    assert profile.source_version == 13
    assert profile.active_array_mask == 0b0110
    assert profile.conveyor_speed_mm_per_sec == 100.0
    assert profile.conveyor_max_speed_mm_per_sec == 200.0
    second, third = profile.arrays[1:3]
    assert second.nozzles_enabled == (True, False, False, True, False, False)
    assert (second.pressure_mbar, second.delay_ms, second.pulse_duration_ms, second.offset_mm) == (
        4250,
        0,
        50,
        12.0,
    )
    assert third.nozzles_enabled == (False, True, False, False, False, False)
    assert (third.pressure_mbar, third.delay_ms, third.pulse_duration_ms, third.offset_mm) == (
        4000,
        0,
        30,
        20.0,
    )
    assert profile.conveyor_calibration == calibration
    assert profile.force_response_delays_ms == baseline.force_response_delays_ms
    assert (
        profile.force_single_nozzle_response_delays_ms
        == baseline.force_single_nozzle_response_delays_ms
    )
    plan = build_write_plan(profile, actuate=True)
    assert plan.configuration["MAIN.GuiNozzleEnabled10"] is True
    assert plan.configuration["MAIN.GuiPressureMbar2"] == 4250
    assert plan.configuration["MAIN.GuiPulseDurationMs3"] == 30
    assert plan.configuration["MAIN.GuiOffsetMm3"] == 20.0
    assert "MAIN.GuiConveyorMmPerFullStep" not in plan.configuration
    assert not any("SpacingMm" in name for name in plan.configuration)
    assert "MAIN.GuiConveyorEnabled" not in plan.configuration
    assert plan.safe_stop["MAIN.GuiConveyorEnabled"] is False
    assert profile.sha256 == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original


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


def test_force_response_is_global_and_profile_values_are_ignored(tmp_path: Path) -> None:
    data = payload()
    data["force_response_delays_ms"] = [15.0, 16.0, 17.0, 18.0]
    data["force_single_nozzle_response_delays_ms"] = [25.0, 26.0, 27.0, 28.0]
    source = tmp_path / "legacy-force-values.json"
    source.write_text(json.dumps(data), encoding="utf-8")

    profile = load_pressure_profile(source)
    plan = build_write_plan(profile, actuate=True)

    assert profile.force_response_delays_ms == (8.7,) * 4
    assert profile.force_single_nozzle_response_delays_ms == (8.7,) * 4
    assert plan.configuration["MAIN.GuiForceResponseDelayMs1"] == 8.7
    assert plan.configuration["MAIN.GuiForceSingleNozzleResponseDelayMs1"] == 8.7


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
    with pytest.raises(ValueError, match="reverse conveyor motion"):
        load_pressure_profile(source)


def _profile_for_array(tmp_path: Path, index: int, speed: float, angle: float):
    data = payload()
    data["conveyor_speed_mm_per_sec"] = speed
    data["ur_ry_angle_deg"] = angle
    data["arrays"][0]["index"] = index
    source = tmp_path / f"array-{index}-{speed}.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    return load_pressure_profile(source)


def test_machine_parameter_comparison_and_path_profile_composition(tmp_path: Path) -> None:
    first = _profile_for_array(tmp_path, 1, 120.0, 18.0)
    second = _profile_for_array(tmp_path, 3, 120.0, 18.0)
    comparison = compare_machine_parameters((first, second))
    assert comparison.common_conveyor_speed_mm_per_sec == 120.0
    assert not comparison.conveyor_speed_conflict

    combined = compose_pressure_profiles(
        (first, second), conveyor_speed_mm_per_sec=125.0, ur_ry_angle_deg=18.5
    )
    assert combined.active_array_mask == 0b0101
    assert combined.conveyor_speed_mm_per_sec == 125.0
    assert combined.ur_ry_angle_deg is None


def test_legacy_zero_tilt_is_ignored_when_loading_and_composing(tmp_path: Path) -> None:
    profile = _profile_for_array(tmp_path, 1, 120.0, 0.0)
    assert profile.ur_ry_angle_deg is None
    assert compare_machine_parameters((profile,)).common_conveyor_speed_mm_per_sec == 120.0
    combined = compose_pressure_profiles(
        (profile,), conveyor_speed_mm_per_sec=120.0, ur_ry_angle_deg=0.0
    )
    assert combined.ur_ry_angle_deg is None


def test_conflicting_parameters_require_override_and_duplicate_array_is_rejected(
    tmp_path: Path,
) -> None:
    first = _profile_for_array(tmp_path, 1, 100.0, 18.0)
    second = _profile_for_array(tmp_path, 2, 140.0, 19.0)
    comparison = compare_machine_parameters((first, second))
    assert comparison.conveyor_speed_conflict

    duplicate = _profile_for_array(tmp_path, 1, 100.0, 18.0)
    with pytest.raises(ValueError, match="Array 1 is active in both"):
        compose_pressure_profiles(
            (first, duplicate), conveyor_speed_mm_per_sec=100.0, ur_ry_angle_deg=18.0
        )


@pytest.mark.parametrize("angle", [None, 18.0, 300, "obsolete robot value"])
def test_old_machine_metadata_is_inert_and_source_hash_is_preserved(tmp_path, angle):
    data = payload()
    data["ur_ry_angle_deg"] = angle
    data["conveyor_calibration"] = {"valid": False, "mm_per_full_step": 0.3296}
    source = tmp_path / "old-profile.json"
    raw = json.dumps(data).encode()
    source.write_bytes(raw)
    live = ConveyorCalibration(315.0, 0.024, True)
    profile = load_pressure_profile(source, PressureBaseline(conveyor_calibration=live))
    assert profile.ur_ry_angle_deg is None
    assert profile.conveyor_calibration == live
    assert profile.sha256 == hashlib.sha256(raw).hexdigest()
    plan = build_write_plan(profile, actuate=True)
    assert plan.configuration["MAIN.GuiConveyorMaxSpeedMmPerSec"] == 200.0
    for name in (
        "GuiConveyorMmPerFullStep",
        "GuiConveyorCalibrationValid",
        "GuiCalibrationMarkerDistanceMm",
    ):
        assert f"MAIN.{name}" not in plan.configuration
    assert source.read_bytes() == raw


@pytest.mark.parametrize("speed", [0.0, -1.0, 200.1, float("nan"), float("inf")])
def test_execution_rejects_invalid_prototype_speed(tmp_path, speed):
    profile = _profile_for_array(tmp_path, 1, 100.0, 18.0)
    with pytest.raises(ValueError, match="speed"):
        compose_pressure_profiles((profile,), conveyor_speed_mm_per_sec=speed)
    with pytest.raises(ValueError, match="speed"):
        build_write_plan(replace(profile, conveyor_speed_mm_per_sec=speed), actuate=False)


def test_high_speed_profile_loads_but_requires_valid_execution_override(tmp_path):
    profile = _profile_for_array(tmp_path, 1, 500.0, 18.0)
    assert profile.conveyor_speed_mm_per_sec == 500.0
    with pytest.raises(ValueError, match="200"):
        build_write_plan(profile, actuate=False)
    effective = compose_pressure_profiles((profile,), conveyor_speed_mm_per_sec=200.0)
    assert effective.conveyor_speed_mm_per_sec == 200.0
    assert effective.conveyor_max_speed_mm_per_sec == 200.0
    limited = replace(profile, conveyor_max_speed_mm_per_sec=80.0)
    with pytest.raises(ValueError, match="maximum speed"):
        compose_pressure_profiles((limited,), conveyor_speed_mm_per_sec=80.1)
    assert (
        compose_pressure_profiles(
            (limited,), conveyor_speed_mm_per_sec=80.0
        ).conveyor_max_speed_mm_per_sec
        == 80.0
    )


def test_legacy_calibration_differences_do_not_block_composition(tmp_path):
    first = _profile_for_array(tmp_path, 1, 100.0, 18.0)
    second = replace(
        _profile_for_array(tmp_path, 2, 100.0, 19.0),
        conveyor_calibration=ConveyorCalibration(100, 0.3296, False),
    )
    assert not compare_machine_parameters((first, second)).conveyor_speed_conflict
    assert (
        compose_pressure_profiles(
            (first, second), conveyor_speed_mm_per_sec=100.0
        ).active_array_mask
        == 3
    )
