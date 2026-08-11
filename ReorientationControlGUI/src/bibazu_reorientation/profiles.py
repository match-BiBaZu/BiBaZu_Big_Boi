from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from bibazu_reorientation.models import (
    ArrayProfile,
    ConveyorCalibration,
    PressureBaseline,
    PressureProfile,
    ProfileWritePlan,
)

ARRAY_COUNT = 4
NOZZLES_PER_ARRAY = 6
PROFILE_VERSION_MAX = 9


@dataclass(slots=True, frozen=True)
class MachineParameterComparison:
    """Global machine values found in a set of transition profiles."""

    ur_angles_deg: tuple[float | None, ...]
    conveyor_speeds_mm_per_sec: tuple[float, ...]
    common_ur_angle_deg: float | None
    common_conveyor_speed_mm_per_sec: float | None
    ur_angle_conflict: bool
    conveyor_speed_conflict: bool


def compare_machine_parameters(
    profiles: tuple[PressureProfile, ...],
) -> MachineParameterComparison:
    if not profiles:
        raise ValueError("At least one pressure profile is required")
    angles = tuple(profile.ur_ry_angle_deg for profile in profiles)
    speeds = tuple(profile.conveyor_speed_mm_per_sec for profile in profiles)
    unique_angles = set(angles)
    unique_speeds = set(speeds)
    return MachineParameterComparison(
        ur_angles_deg=angles,
        conveyor_speeds_mm_per_sec=speeds,
        common_ur_angle_deg=angles[0] if len(unique_angles) == 1 else None,
        common_conveyor_speed_mm_per_sec=speeds[0] if len(unique_speeds) == 1 else None,
        ur_angle_conflict=len(unique_angles) != 1,
        conveyor_speed_conflict=len(unique_speeds) != 1,
    )


def compose_pressure_profiles(
    profiles: tuple[PressureProfile, ...],
    *,
    conveyor_speed_mm_per_sec: float,
    ur_ry_angle_deg: float | None,
) -> PressureProfile:
    """Merge active, non-overlapping arrays for one planned roadmap path."""

    if not profiles:
        raise ValueError("At least one pressure profile is required")
    if not math.isfinite(conveyor_speed_mm_per_sec) or not 0 < conveyor_speed_mm_per_sec <= 5000:
        raise ValueError("Conveyor speed must be between 0 and 5000 mm/s")
    if ur_ry_angle_deg is not None and (
        not math.isfinite(ur_ry_angle_deg) or not 15.5 <= ur_ry_angle_deg <= 21.0
    ):
        raise ValueError("UR Ry angle must be between 15.5 and 21.0 degrees")
    if any(profile.conveyor_reverse for profile in profiles):
        raise ValueError("Roadmap execution does not support reverse conveyor motion")
    if any(
        conveyor_speed_mm_per_sec > profile.conveyor_max_speed_mm_per_sec
        for profile in profiles
    ):
        raise ValueError("Selected conveyor speed exceeds a profile's maximum speed")

    base = profiles[0]
    shared_fields = (
        "light_barrier_debounce_ms",
        "light_barrier_inverted",
        "light_barrier_debounce_enabled",
        "conveyor_calibration",
    )
    for field in shared_fields:
        values = {getattr(profile, field) for profile in profiles}
        if len(values) != 1:
            raise ValueError(
                f"Profiles use different global {field.replace('_', ' ')} values; "
                "these cannot be combined in one PLC cycle"
            )

    arrays = [
        replace(row, enabled=False, nozzles_enabled=(False, False, False, False, False, False))
        for row in base.arrays
    ]
    force_delays = list(base.force_response_delays_ms)
    single_delays = list(base.force_single_nozzle_response_delays_ms)
    owners: dict[int, Path] = {}
    for profile in profiles:
        for row in profile.arrays:
            if not row.active:
                continue
            previous = owners.get(row.index)
            if previous is not None:
                raise ValueError(
                    f"Array {row.index} is active in both {previous.name} and "
                    f"{profile.source_path.name}; one path may configure each array only once"
                )
            owners[row.index] = profile.source_path
            arrays[row.index - 1] = row
            force_delays[row.index - 1] = profile.force_response_delays_ms[row.index - 1]
            single_delays[row.index - 1] = profile.force_single_nozzle_response_delays_ms[
                row.index - 1
            ]
    if not owners:
        raise ValueError("The planned transition profiles do not activate any nozzle array")

    signature = "|".join(
        [*(profile.sha256 for profile in profiles), f"speed={conveyor_speed_mm_per_sec}",
         f"ur={ur_ry_angle_deg}"]
    ).encode("utf-8")
    return replace(
        base,
        source_version=max(profile.source_version for profile in profiles),
        created_at=None,
        ur_ry_angle_deg=ur_ry_angle_deg,
        conveyor_enabled=True,
        conveyor_reverse=False,
        conveyor_speed_mm_per_sec=float(conveyor_speed_mm_per_sec),
        conveyor_max_speed_mm_per_sec=min(
            profile.conveyor_max_speed_mm_per_sec for profile in profiles
        ),
        force_response_delays_ms=tuple(force_delays),
        force_single_nozzle_response_delays_ms=tuple(single_delays),
        arrays=tuple(arrays),
        sha256=hashlib.sha256(signature).hexdigest(),
    )


def _boolean(value: Any, field: str, fallback: bool) -> bool:
    if value is None:
        return fallback
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    return result


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    result = _number(value, field, minimum, maximum)
    if result != int(result):
        raise ValueError(f"{field} must be an integer")
    return int(result)


def _bool_list(value: Any, field: str, length: int, fallback: tuple[bool, ...]) -> tuple[bool, ...]:
    if value is None:
        return tuple(fallback)
    if (
        not isinstance(value, list)
        or len(value) != length
        or not all(isinstance(item, bool) for item in value)
    ):
        raise ValueError(f"{field} must contain {length} boolean values")
    return tuple(value)


def _barrier_bool_list(
    value: Any, field: str, fallback: tuple[bool, ...]
) -> tuple[bool, ...]:
    if isinstance(value, list) and len(value) == 6:
        value = [*value, *fallback[6:]]
    return _bool_list(value, field, 8, fallback)


def _float_list(
    value: Any,
    field: str,
    length: int,
    fallback: tuple[float, ...],
) -> tuple[float, ...]:
    if value is None:
        return tuple(fallback)
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{field} must contain {length} values")
    return tuple(
        _number(item, f"{field}[{index}]", 0.0, 1000.0) for index, item in enumerate(value)
    )


def _default_array(index: int) -> ArrayProfile:
    return ArrayProfile(index, False, (False,) * 6, 3000, 0, 100, 0.0)


def _array_from_item(item: dict[str, Any], index_override: int | None = None) -> ArrayProfile:
    index = index_override or _integer(item.get("index"), "array.index", 1, ARRAY_COUNT)
    enabled = item.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"Array {index}: enabled must be boolean")
    nozzles_raw = item.get("nozzles_enabled")
    if nozzles_raw is not None:
        if not isinstance(nozzles_raw, list) or not all(
            isinstance(value, bool) for value in nozzles_raw
        ):
            raise ValueError(f"Array {index}: nozzles_enabled must be a boolean list")
        if len(nozzles_raw) > NOZZLES_PER_ARRAY:
            raise ValueError(f"Array {index}: no more than six nozzles are allowed")
        nozzles = tuple([*nozzles_raw, *([False] * (NOZZLES_PER_ARRAY - len(nozzles_raw)))])
    else:
        specific = [item.get(f"nozzle_{number}_enabled") for number in range(1, 7)]
        present = [value for value in specific if value is not None]
        if present and not all(isinstance(value, bool) for value in present):
            raise ValueError(f"Array {index}: nozzle flags must be boolean")
        nozzles = tuple(bool(value) if value is not None else False for value in specific)
        if not present:
            nozzles = (enabled, enabled, False, False, False, False)
    return ArrayProfile(
        index=index,
        enabled=enabled,
        nozzles_enabled=nozzles,  # type: ignore[arg-type]
        pressure_mbar=_integer(item.get("pressure_mbar", 3000), f"Array {index} pressure", 0, 6000),
        delay_ms=_integer(item.get("delay_ms", 0), f"Array {index} delay", 0, 1000),
        pulse_duration_ms=_integer(
            item.get("pulse_duration_ms", 100), f"Array {index} pulse", 1, 500
        ),
        offset_mm=_number(item.get("offset_mm", 0.0), f"Array {index} offset", 0.0, 5000.0),
    )


def _normalize_arrays(value: Any) -> tuple[ArrayProfile, ArrayProfile, ArrayProfile, ArrayProfile]:
    if not isinstance(value, list):
        raise ValueError("arrays must be a list")
    normalized: dict[int, ArrayProfile] = {}
    if len(value) > ARRAY_COUNT:
        legacy: dict[int, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("Every array entry must be an object")
            legacy[_integer(item.get("index"), "legacy array.index", 1, 100)] = item
        for row_index in range(1, ARRAY_COUNT + 1):
            first = (row_index - 1) * 2 + 1
            pair = [legacy.get(first, {}), legacy.get(first + 1, {})]
            source = next((item for item in pair if item.get("enabled", False)), pair[0])
            migrated = dict(source)
            migrated["index"] = row_index
            migrated["enabled"] = any(bool(item.get("enabled", False)) for item in pair)
            migrated["nozzles_enabled"] = [
                bool(pair[0].get("enabled", False)),
                bool(pair[1].get("enabled", False)),
                False,
                False,
                False,
                False,
            ]
            normalized[row_index] = _array_from_item(migrated)
    else:
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("Every array entry must be an object")
            array = _array_from_item(item)
            if array.index in normalized:
                raise ValueError(f"Duplicate array index: {array.index}")
            normalized[array.index] = array
    return tuple(normalized.get(index, _default_array(index)) for index in range(1, 5))  # type: ignore[return-value]


def load_pressure_profile(
    path: Path,
    baseline: PressureBaseline | None = None,
    *,
    require_transition: bool = True,
) -> PressureProfile:
    source = Path(path).expanduser().resolve()
    raw = source.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid profile JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("The pressure profile must contain a JSON object")
    version = _integer(payload.get("version", 1), "version", 1, PROFILE_VERSION_MAX)
    baseline = baseline or PressureBaseline()

    calibration_raw = payload.get("conveyor_calibration")
    if calibration_raw is None:
        calibration = baseline.conveyor_calibration
    elif not isinstance(calibration_raw, dict):
        raise ValueError("conveyor_calibration must be an object")
    else:
        calibration = ConveyorCalibration(
            marker_distance_mm=_number(
                calibration_raw.get(
                    "marker_distance_mm", baseline.conveyor_calibration.marker_distance_mm
                ),
                "calibration.marker_distance_mm",
                1.0,
                5000.0,
            ),
            mm_per_full_step=_number(
                calibration_raw.get(
                    "mm_per_full_step", baseline.conveyor_calibration.mm_per_full_step
                ),
                "calibration.mm_per_full_step",
                0.000001,
                1000.0,
            ),
            valid=_boolean(calibration_raw.get("valid"), "calibration.valid", False),
        )
    ur_angle = payload.get("ur_ry_angle_deg")
    ur_angle_value = None if ur_angle is None else _number(ur_angle, "ur_ry_angle_deg", 15.5, 21.0)
    profile = PressureProfile(
        source_path=source,
        source_version=version,
        created_at=str(payload["created_at"]) if "created_at" in payload else None,
        ur_ry_angle_deg=ur_angle_value,
        light_barrier_debounce_ms=_integer(
            payload.get("light_barrier_debounce_ms", baseline.light_barrier_debounce_ms),
            "light_barrier_debounce_ms",
            1,
            200,
        ),
        light_barrier_inverted=_barrier_bool_list(
            payload.get("light_barrier_inverted"),
            "light_barrier_inverted",
            baseline.light_barrier_inverted,
        ),  # type: ignore[arg-type]
        light_barrier_debounce_enabled=_barrier_bool_list(
            payload.get("light_barrier_debounce_enabled"),
            "light_barrier_debounce_enabled",
            baseline.light_barrier_debounce_enabled,
        ),  # type: ignore[arg-type]
        conveyor_enabled=_boolean(
            payload.get("conveyor_enabled"), "conveyor_enabled", baseline.conveyor_enabled
        ),
        conveyor_reverse=_boolean(
            payload.get("conveyor_reverse"), "conveyor_reverse", baseline.conveyor_reverse
        ),
        conveyor_speed_mm_per_sec=_number(
            payload.get("conveyor_speed_mm_per_sec", baseline.conveyor_speed_mm_per_sec),
            "conveyor_speed_mm_per_sec",
            0.0,
            5000.0,
        ),
        conveyor_max_speed_mm_per_sec=_number(
            payload.get("conveyor_max_speed_mm_per_sec", baseline.conveyor_max_speed_mm_per_sec),
            "conveyor_max_speed_mm_per_sec",
            1.0,
            5000.0,
        ),
        conveyor_calibration=calibration,
        force_response_delays_ms=_float_list(
            payload.get("force_response_delays_ms"),
            "force_response_delays_ms",
            4,
            baseline.force_response_delays_ms,
        ),  # type: ignore[arg-type]
        force_single_nozzle_response_delays_ms=_float_list(
            payload.get("force_single_nozzle_response_delays_ms"),
            "force_single_nozzle_response_delays_ms",
            4,
            baseline.force_single_nozzle_response_delays_ms,
        ),  # type: ignore[arg-type]
        arrays=_normalize_arrays(payload.get("arrays", [])),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if require_transition:
        if not profile.conveyor_enabled:
            raise ValueError("The transition profile must enable the conveyor")
        if profile.conveyor_reverse:
            raise ValueError("V1 does not support reverse conveyor motion")
        if profile.conveyor_speed_mm_per_sec <= 0:
            raise ValueError("The transition profile requires a positive conveyor speed")
        if not profile.conveyor_calibration.valid:
            raise ValueError("A valid conveyor calibration is required")
        if profile.active_array_mask == 0:
            raise ValueError("The transition profile must activate at least one nozzle array")
    return profile


SAFE_STOP_VALUES: dict[str, bool | int | float] = {
    "MAIN.GuiReorientationStart": False,
    "MAIN.GuiReorientationAbort": False,
    "MAIN.GuiConveyorEnabled": False,
    "MAIN.GuiArrayEnabled1": False,
    "MAIN.GuiArrayEnabled2": False,
    "MAIN.GuiArrayEnabled3": False,
    "MAIN.GuiArrayEnabled4": False,
    "MAIN.GuiConveyorCalibrationMode": False,
    "MAIN.GuiVelocityCheckMode": False,
    "MAIN.GuiForceDelayMeasurementEnabled": False,
}


def build_write_plan(profile: PressureProfile, *, actuate: bool) -> ProfileWritePlan:
    configuration: dict[str, bool | int | float] = {
        "MAIN.GuiBarrierCalibrationDebounceMs": profile.light_barrier_debounce_ms,
        "MAIN.GuiConveyorReverse": False,
        "MAIN.GuiConveyorSpeedMmPerSec": profile.conveyor_speed_mm_per_sec,
        "MAIN.GuiConveyorMaxSpeedMmPerSec": profile.conveyor_max_speed_mm_per_sec,
        "MAIN.GuiCalibrationMarkerDistanceMm": profile.conveyor_calibration.marker_distance_mm,
        "MAIN.GuiConveyorMmPerFullStep": profile.conveyor_calibration.mm_per_full_step,
        "MAIN.GuiConveyorCalibrationValid": profile.conveyor_calibration.valid,
    }
    configuration.update(
        {
            f"MAIN.GuiLightBarrierInvert{index}": value
            for index, value in enumerate(profile.light_barrier_inverted, start=1)
        }
    )
    configuration.update(
        {
            f"MAIN.GuiLightBarrierDebounceEnabled{index}": value
            for index, value in enumerate(profile.light_barrier_debounce_enabled, start=1)
        }
    )
    for row in profile.arrays:
        configuration.update(
            {
                f"MAIN.GuiPressureMbar{row.index}": row.pressure_mbar,
                f"MAIN.GuiDelayMs{row.index}": row.delay_ms,
                f"MAIN.GuiPulseDurationMs{row.index}": row.pulse_duration_ms,
                f"MAIN.GuiOffsetMm{row.index}": row.offset_mm,
                f"MAIN.GuiForceResponseDelayMs{row.index}": profile.force_response_delays_ms[
                    row.index - 1
                ],
                f"MAIN.GuiForceSingleNozzleResponseDelayMs{row.index}": (
                    profile.force_single_nozzle_response_delays_ms[row.index - 1]
                ),
            }
        )
        configuration.update(
            {
                f"MAIN.GuiNozzleEnabled{(row.index - 1) * 6 + nozzle}": value
                for nozzle, value in enumerate(row.nozzles_enabled, start=1)
            }
        )
    mask = profile.active_array_mask if actuate else 0
    enables: dict[str, bool | int | float] = {
        f"MAIN.GuiArrayEnabled{row.index}": bool(actuate and row.active) for row in profile.arrays
    }
    enables.update(
        {
            "MAIN.GuiConveyorEnabled": True,
            "MAIN.GuiReorientationExpectedArrayMask": mask,
        }
    )
    return ProfileWritePlan(dict(SAFE_STOP_VALUES), configuration, enables, mask)
