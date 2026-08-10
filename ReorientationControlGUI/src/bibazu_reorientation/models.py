from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np


class ConnectionState(StrEnum):
    DISCONNECTED = "Disconnected"
    DISCOVERING = "Discovering"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    DEGRADED = "Degraded"
    ERROR = "Error"


class CycleState(StrEnum):
    NO_CONFIG = "No configuration"
    OFFLINE = "Offline"
    READY = "Ready"
    DETECTING = "Detecting pose"
    DECIDED = "Pose decided"
    STAGING = "Staging profile"
    ARMED = "PLC armed"
    RUNNING = "Part in transit"
    DRAINING = "Draining nozzle arrays"
    COMPLETE = "Complete"
    ABORTING = "Aborting"
    ABORTED = "Aborted"
    FAULT = "Fault"


@dataclass(slots=True, frozen=True)
class PoseDefinition:
    id: int
    label: str
    model_class_id: int


@dataclass(slots=True, frozen=True)
class TransitionSpec:
    from_pose: int
    to_pose: int
    pressure_profile: Path | None
    edge_id: str = ""
    transition_kind: str = "actuated"
    actuation: str = ""
    signed_angle_deg: float | None = None
    geometric_score: float | None = None
    experimental_status: str = ""


@dataclass(slots=True, frozen=True)
class PartDefinition:
    schema_version: int
    part_name: str
    model_path: Path
    poses: tuple[PoseDefinition, ...]
    target_pose: int
    transitions: tuple[TransitionSpec, ...]
    mesh_path: Path | None = None
    source_path: Path | None = None
    roadmap_path: Path | None = None
    target_roadmap_pose_id: int | None = None
    roadmap_sha256: str | None = None
    roadmap_changed: bool = False
    roadmap_added_pose_ids: tuple[int, ...] = ()
    roadmap_removed_pose_ids: tuple[int, ...] = ()
    roadmap_added_edge_ids: tuple[str, ...] = ()
    roadmap_removed_edge_ids: tuple[str, ...] = ()

    @property
    def is_roadmap_configuration(self) -> bool:
        return self.schema_version == 2


@dataclass(slots=True, frozen=True)
class RoadmapReadiness:
    missing_profile_edge_ids: tuple[str, ...]
    reachable_pose_ids: tuple[int, ...]
    unreachable_pose_ids: tuple[int, ...]
    unmapped_pose_ids: tuple[int, ...]
    roadmap_hash_matches: bool
    name_differs: bool
    mesh_differs: bool

    @property
    def is_complete(self) -> bool:
        return (
            not self.missing_profile_edge_ids
            and not self.unmapped_pose_ids
            and self.roadmap_hash_matches
        )


@dataclass(slots=True, frozen=True)
class ArrayProfile:
    index: int
    enabled: bool
    nozzles_enabled: tuple[bool, bool, bool, bool, bool, bool]
    pressure_mbar: int
    delay_ms: int
    pulse_duration_ms: int
    offset_mm: float

    @property
    def active(self) -> bool:
        return self.enabled and any(self.nozzles_enabled)


@dataclass(slots=True, frozen=True)
class ConveyorCalibration:
    marker_distance_mm: float
    mm_per_full_step: float
    valid: bool


@dataclass(slots=True, frozen=True)
class PressureBaseline:
    light_barrier_debounce_ms: int = 20
    light_barrier_inverted: tuple[bool, ...] = (False, False, True, True, False, False)
    light_barrier_debounce_enabled: tuple[bool, ...] = (
        True,
        True,
        False,
        False,
        True,
        True,
    )
    conveyor_speed_mm_per_sec: float = 0.0
    conveyor_max_speed_mm_per_sec: float = 1000.0
    conveyor_enabled: bool = False
    conveyor_reverse: bool = False
    conveyor_calibration: ConveyorCalibration = ConveyorCalibration(315.0, 0.32960026, True)
    force_response_delays_ms: tuple[float, ...] = (15.0, 15.0, 15.0, 15.0)
    force_single_nozzle_response_delays_ms: tuple[float, ...] = (15.0, 15.0, 15.0, 15.0)


@dataclass(slots=True, frozen=True)
class PressureProfile:
    source_path: Path
    source_version: int
    created_at: str | None
    ur_ry_angle_deg: float | None
    light_barrier_debounce_ms: int
    light_barrier_inverted: tuple[bool, bool, bool, bool, bool, bool]
    light_barrier_debounce_enabled: tuple[bool, bool, bool, bool, bool, bool]
    conveyor_enabled: bool
    conveyor_reverse: bool
    conveyor_speed_mm_per_sec: float
    conveyor_max_speed_mm_per_sec: float
    conveyor_calibration: ConveyorCalibration
    force_response_delays_ms: tuple[float, float, float, float]
    force_single_nozzle_response_delays_ms: tuple[float, float, float, float]
    arrays: tuple[ArrayProfile, ArrayProfile, ArrayProfile, ArrayProfile]
    sha256: str

    @property
    def active_array_mask(self) -> int:
        return sum((1 << (row.index - 1)) for row in self.arrays if row.active)


@dataclass(slots=True, frozen=True)
class ProfileWritePlan:
    safe_stop: dict[str, bool | int | float]
    configuration: dict[str, bool | int | float]
    enables: dict[str, bool | int | float]
    expected_array_mask: int


@dataclass(slots=True, frozen=True)
class Detection:
    class_id: int
    class_name: str
    confidence: float
    corners: tuple[tuple[float, float], ...]
    kind: str


@dataclass(slots=True, frozen=True)
class InferenceFrame:
    image: np.ndarray
    detections: tuple[Detection, ...]
    inference_ms: float
    timestamp: float


@dataclass(slots=True, frozen=True)
class PoseObservation:
    pose_id: int
    class_id: int
    confidence: float
    timestamp: float


@dataclass(slots=True, frozen=True)
class ConsensusDecision:
    pose_id: int
    observations: tuple[PoseObservation, PoseObservation, PoseObservation]


@dataclass(slots=True)
class CameraStatus:
    model: str = "–"
    serial_number: str = "–"
    ip_address: str = "–"
    width: int = 0
    height: int = 0
    pixel_format: str = "–"
    camera_fps: float | None = None
    stream_fps: float = 0.0
    preview_fps: float = 0.0
    exposure_time_us: float | None = None
    exposure_min_us: float | None = None
    exposure_max_us: float | None = None
    exposure_writable: bool = False
    exposure_auto: str = "–"
    gain: float | None = None


@dataclass(slots=True)
class CameraFrame:
    image: np.ndarray
    pixel_format: str
    timestamp: float


@dataclass(slots=True, frozen=True)
class LightCapabilities:
    power: bool = True
    brightness: bool = True
    cct: bool = True
    hsi: bool = True
    min_cct_kelvin: int = 3200
    max_cct_kelvin: int = 5600


@dataclass(slots=True)
class LightStatus:
    name: str = "–"
    address: str = "–"
    rssi: int | None = None
    connected: bool = False
    power: bool | None = None
    mode: str = "CCT"
    brightness: int = 50
    cct_kelvin: int = 5600
    hue: int = 0
    saturation: int = 100
    capabilities: LightCapabilities = field(default_factory=LightCapabilities)
    values_are_confirmed_commands: bool = False
    last_command_confirmed_at: float | None = None
    last_command_duration_ms: float | None = None


@dataclass(slots=True, frozen=True)
class DiscoveredLight:
    name: str
    address: str
    rssi: int | None = None
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(slots=True, frozen=True)
class PlcSnapshot:
    connected: bool = False
    conveyor_motion_state: int = 0
    stepper_busy: bool = False
    stepper_error: bool = False
    calibration_valid: bool = False
    array_states: tuple[int, int, int, int] = (2, 2, 2, 2)
    pending_mask: int = 0
    open_valve_mask: int = 0
    vtem_error_codes: tuple[int, int] = (0, 0)
    light_barriers_stable: tuple[bool, ...] = (True,) * 6
    reorientation_state: int = 0
    reorientation_fault_code: int = 0
    heartbeat_alive: bool = False
    heartbeat_ack: int = 0
    busy: bool = False
    exit_seen: bool = False
    arrays_idle: bool = True
    expected_array_mask: int = 0
    triggered_array_mask: int = 0
    complete: bool = False
    cycle_counter: int = 0
    velocities: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
    delays: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
    avg_pressure_n1: float = 0.0
    avg_pressure_n2: float = 0.0


@dataclass(slots=True, frozen=True)
class CycleResult:
    cycle_id: str
    state: CycleState
    part_name: str
    started_at: datetime
    finished_at: datetime
    detected_pose: int | None
    target_pose: int
    action: str
    expected_array_mask: int
    triggered_array_mask: int
    error_code: str = ""
    error_text: str = ""
