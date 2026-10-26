from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from bibazu_reorientation.batch import PartQueuePlanner, queue_record_values
from bibazu_reorientation.conveyor import MAX_SPEED_MM_PER_SEC
from bibazu_reorientation.inference import filter_input_detections
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import (
    BatchState,
    InferenceFrame,
    PartDecision,
    PartDefinition,
    PartResult,
    PlcSnapshot,
    PressureProfile,
    ProductionMode,
    QueuedPartProfile,
)
from bibazu_reorientation.profiles import build_write_plan, compose_pressure_profiles
from bibazu_reorientation.tracking import (
    MultiPartTracker,
    TrackerUpdate,
    snapshot_queue,
)


class BatchController(QObject):
    state_changed = pyqtSignal(object, str)
    warning_raised = pyqtSignal(str, str)
    preflight_changed = pyqtSignal(object)
    tracks_changed = pyqtSignal(object)
    counters_changed = pyqtSignal(object)
    part_queued = pyqtSignal(object)
    part_result = pyqtSignal(object)

    def __init__(
        self,
        pressure: QObject,
        journal: RunJournal | None = None,
        *,
        handoff_line_ratio: float = 0.30,
        production_mode: ProductionMode = ProductionMode.SNAPSHOT,
        pose_cutoff_ratio: float | None = None,
        input_line_ratio: float = 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.pressure = pressure
        self.journal = journal or RunJournal()
        self.state = BatchState.NO_CONFIG
        self.part: PartDefinition | None = None
        self.profile: PressureProfile | None = None
        self.snapshot = PlcSnapshot()
        self.model_ready = False
        self.camera_fresh = False
        self.lights_ready = (False, False)
        self.production_mode = ProductionMode(production_mode)
        if not handoff_line_ratio < input_line_ratio <= 1.0:
            raise ValueError("Input boundary must be right of the handoff line and at most 100%")
        self.input_line_ratio = input_line_ratio
        self.pose_cutoff_ratio = (
            handoff_line_ratio
            if self.continuous and pose_cutoff_ratio is None
            else 0.10
            if pose_cutoff_ratio is None
            else pose_cutoff_ratio
        )
        self.tracker = self._make_tracker(handoff_line_ratio)
        self._roadmap_mode = False
        self._roadmap_profiles: dict[str, PressureProfile] = {}
        self._transport_profile: PressureProfile | None = None
        self._planner: PartQueuePlanner | None = None
        self._last_preflight: dict[str, bool] | None = None
        self._heartbeat = 0
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(250)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)
        self._handshake_phase: str | None = None
        self._handshake_timer = QTimer(self)
        self._handshake_timer.setSingleShot(True)
        # ADS polling reads a large PLC snapshot and can occasionally take more
        # than three seconds on the laboratory network. Only the final PLC
        # running state is acknowledged, so this is a fault timeout rather than
        # part of the normal command sequencing.
        self._handshake_timer.setInterval(10_000)
        self._handshake_timer.timeout.connect(self._handshake_timeout)
        self._preparation_name: str | None = None
        self._run_id = ""
        self._session = None
        self._sequence = 0
        self._handoff_queue: deque[tuple[QueuedPartProfile, PartDecision, np.ndarray, str]] = (
            deque()
        )
        self._pending_enqueue: tuple[QueuedPartProfile, PartDecision, np.ndarray, str] | None = None
        self._pending_parts: dict[int, tuple[QueuedPartProfile, PartDecision, str]] = {}
        self._logged_sequences: set[int] = set()
        self._enqueue_ack_times: dict[int, float] = {}
        self._last_result_sequence = 0
        self._last_tracks = ()
        self._latest_frame: InferenceFrame | None = None
        self._initial_decisions: tuple[PartDecision, ...] = ()
        self._initial_snapshot_image: np.ndarray | None = None
        self._initial_queue_staged = False
        self._conveyor_start_pending = False
        self._plc_reset_acknowledged = False
        self._last_plc_warning_counter: int | None = None
        self._release_pending = False
        self._operator_stop_requested = False
        self._operator_stop_phase: str | None = None
        self._light_addresses = ("", "")
        self._config_hash = ""
        self._model_hash = ""
        self._last_tracking_timestamp = 0.0
        self._enqueue_requested_at: float | None = None
        self._enqueue_written_at: float | None = None
        self._finishing_track_ids: set[int] = set()
        self._finish_allowed_ids: set[int] = set()
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(100)
        self._health_timer.timeout.connect(self._check_continuous_health)
        pressure.snapshot_changed.connect(self._on_snapshot)
        pressure.connection_changed.connect(self._connection_changed)
        pressure.operation_finished.connect(self._operation_finished)
        pressure.operation_failed.connect(self._operation_failed)

    @property
    def continuous(self) -> bool:
        return self.production_mode == ProductionMode.CONTINUOUS

    def _make_tracker(self, handoff_line_ratio: float) -> MultiPartTracker:
        return MultiPartTracker(
            handoff_line_ratio=handoff_line_ratio,
            strict_order=self.continuous,
            pose_cutoff_ratio=self.pose_cutoff_ratio,
            pose_at_handoff=self.continuous,
        )

    def set_production_mode(self, mode: ProductionMode) -> None:
        self._assert_configurable()
        self.production_mode = ProductionMode(mode)
        self.pose_cutoff_ratio = self.handoff_line_ratio
        self.tracker = self._make_tracker(self.handoff_line_ratio)
        self._clear_detection_snapshot()
        self._refresh_preflight()

    @property
    def handoff_line_ratio(self) -> float:
        return self.tracker.handoff_line_ratio

    def set_handoff_line_ratio(self, value: float) -> None:
        if self.state not in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
            BatchState.COMPLETE,
            BatchState.FAULT,
        }:
            raise RuntimeError("The handoff line cannot change during a run")
        if not 0.05 <= value <= 0.80:
            raise ValueError("Profile handoff line must be between 5 and 80 percent")
        if value >= self.input_line_ratio:
            raise ValueError("Handoff line must be left of the input boundary")
        self.pose_cutoff_ratio = value
        self.tracker = self._make_tracker(value)
        self._refresh_preflight()

    def set_input_line_ratio(self, value: float) -> None:
        self._assert_configurable()
        if not self.handoff_line_ratio < value <= 1.0:
            raise ValueError("Input boundary must be right of the handoff line and at most 100%")
        self.input_line_ratio = value
        self.tracker.reset()
        self._clear_detection_snapshot()
        self._refresh_preflight()

    def set_configuration(self, part: PartDefinition, profile: PressureProfile) -> None:
        self._assert_configurable()
        self._clear_detection_snapshot()
        profile = replace(profile, ur_ry_angle_deg=None)
        self.part = part
        self.profile = profile
        self._transport_profile = profile
        self._roadmap_mode = False
        self._roadmap_profiles = {}
        self._planner = PartQueuePlanner(part, profile)
        self._refresh_preflight()

    def set_roadmap_configuration(
        self,
        part: PartDefinition,
        profiles_by_edge: dict[str, PressureProfile],
        *,
        conveyor_speed_mm_per_sec: float,
    ) -> None:
        self._assert_configurable()
        self._clear_detection_snapshot()
        if not profiles_by_edge:
            raise ValueError("Assign at least one pressure profile before execution")
        first = next(iter(profiles_by_edge.values()))
        transport = compose_pressure_profiles(
            (first,),
            conveyor_speed_mm_per_sec=conveyor_speed_mm_per_sec,
        )
        self.part = part
        self.profile = transport
        self._transport_profile = transport
        self._roadmap_mode = True
        self._roadmap_profiles = dict(profiles_by_edge)
        self._planner = PartQueuePlanner(part, transport, profiles_by_edge)
        self._refresh_preflight()

    def set_manual_configuration(
        self,
        part: PartDefinition,
        profiles_by_edge: dict[str, PressureProfile],
        *,
        conveyor_speed_mm_per_sec: float,
    ) -> None:
        if not part.is_manual_configuration:
            raise ValueError("A manual start-pose configuration is required")
        if not profiles_by_edge:
            raise ValueError("Assign a pressure profile to at least one non-target start pose")
        for profile in profiles_by_edge.values():
            if conveyor_speed_mm_per_sec > profile.conveyor_max_speed_mm_per_sec:
                raise ValueError("Selected conveyor speed exceeds a profile's maximum speed")
        for field in (
            "light_barrier_debounce_ms",
            "light_barrier_inverted",
            "light_barrier_debounce_enabled",
        ):
            if len({getattr(profile, field) for profile in profiles_by_edge.values()}) != 1:
                raise ValueError(f"Manual profiles must share the same {field.replace('_', ' ')}")
        # Reuse the same snapshot queue and profile storage, with direct manual
        # selection in PartQueuePlanner instead of the roadmap resolver.
        self.set_roadmap_configuration(
            part, profiles_by_edge, conveyor_speed_mm_per_sec=conveyor_speed_mm_per_sec
        )
        self._roadmap_mode = False

    def clear_configuration(self) -> None:
        self._assert_configurable()
        self._clear_detection_snapshot()
        self.part = None
        self.profile = None
        self._transport_profile = None
        self._roadmap_profiles = {}
        self._planner = None
        self._set_state(BatchState.NO_CONFIG)
        self._refresh_preflight()

    def _clear_detection_snapshot(self) -> None:
        self._latest_frame = None
        self._initial_decisions = ()
        self._initial_snapshot_image = None
        self._last_tracks = ()

    def _assert_configurable(self) -> None:
        if self.state not in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
            BatchState.COMPLETE,
            BatchState.FAULT,
        }:
            raise RuntimeError("The configuration cannot change during a production run")

    def set_model_ready(self, ready: bool) -> None:
        changed = self.model_ready != ready
        self.model_ready = ready
        if (
            changed
            and not ready
            and self.state
            in {
                BatchState.STARTING,
                BatchState.RUNNING,
                BatchState.FINISHING,
                BatchState.DRAINING,
            }
        ):
            if self.continuous and self.state != BatchState.DRAINING:
                self._fault("yolo_lost", "YOLO unavailable during continuous feeding")
                return
            self._warn(
                "yolo_lost",
                "YOLO became unavailable after the production snapshot was frozen",
            )
        elif changed:
            self._refresh_preflight()

    def set_camera_fresh(self, ready: bool) -> None:
        changed = self.camera_fresh != ready
        self.camera_fresh = ready
        if (
            changed
            and not ready
            and self.state
            in {
                BatchState.STARTING,
                BatchState.RUNNING,
                BatchState.FINISHING,
                BatchState.DRAINING,
            }
        ):
            if self.continuous and self.state != BatchState.DRAINING:
                self._fault("camera_lost", "Camera frames stale during continuous feeding")
                return
            self._warn(
                "camera_lost",
                "Camera frames became stale after the production snapshot was frozen",
            )
        elif changed:
            self._refresh_preflight()

    def set_lights_ready(self, first: bool, second: bool) -> None:
        self.lights_ready = (first, second)

    def set_light_addresses(self, first: str, second: str) -> None:
        self._light_addresses = (first, second)

    def preflight(self) -> dict[str, bool]:
        snapshot = self.snapshot
        profile = self.profile
        frame = self._latest_frame
        checks = {
            "Configuration and at least one profile loaded": (
                self.part is not None and profile is not None and self._planner is not None
            ),
            "YOLO model warmed up": self.model_ready,
            "Fresh camera frame": self.camera_fresh,
            "PLC batch contract connected": snapshot.connected,
            "Conveyor speed valid": profile is not None
            and (
                0
                < profile.conveyor_speed_mm_per_sec
                <= min(MAX_SPEED_MM_PER_SEC, profile.conveyor_max_speed_mm_per_sec)
            ),
            "Beckhoff velocity-only PLC": snapshot.conveyor_velocity_only,
            "Calibration valid": snapshot.calibration_valid,
            "Arrays idle / valves closed": snapshot.arrays_idle
            and snapshot.pending_mask == 0
            and snapshot.open_valve_mask == 0,
            "Drive and VTEM fault-free": (
                not snapshot.stepper_error and snapshot.vtem_error_codes == (0, 0)
            ),
        }
        if self.continuous:
            checks["Fresh YOLO frame"] = (
                frame is not None and time.time() - frame.timestamp <= 1.0
            )
            checks["PLC queue available"] = snapshot.batch_queue_capacity > 0
            checks["All light barriers clear"] = all(snapshot.light_barriers_stable)
        return checks

    def _refresh_preflight(self) -> None:
        checks = self.preflight()
        if checks != self._last_preflight:
            self._last_preflight = checks.copy()
            self.preflight_changed.emit(checks)
        if self.state in {BatchState.NO_CONFIG, BatchState.OFFLINE, BatchState.READY}:
            self._set_state(BatchState.READY if all(checks.values()) else BatchState.OFFLINE)

    def start_cycle(self) -> None:
        if (
            self.state != BatchState.READY
            or not all(self.preflight().values())
            or self.part is None
            or self.profile is None
        ):
            raise RuntimeError("Production-run preflight is incomplete")
        frame = self._latest_frame
        if frame is None or time.time() - frame.timestamp > 1.0:
            raise RuntimeError("No fresh YOLO snapshot is available")
        mapping = self.part.recognition_mapping
        frozen = snapshot_queue(frame, mapping, pose_cutoff_ratio=self.pose_cutoff_ratio)
        if len(frozen.handoffs) > self.snapshot.batch_queue_capacity:
            raise RuntimeError("The production snapshot exceeds the PLC queue capacity")
        self._run_id = f"run-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        profiles = tuple(self._roadmap_profiles.values()) or (self.profile,)
        self._session = self.journal.begin_batch(
            self._run_id, self.part, profiles, production_mode=self.production_mode.value
        )
        self._config_hash = (
            self.journal.sha256(self.part.source_path) if self.part.source_path else ""
        )
        self._model_hash = self.journal.sha256(self.part.model_path)
        self.tracker.reset()
        self._sequence = 0
        self._handoff_queue.clear()
        self._pending_enqueue = None
        self._pending_parts.clear()
        self._logged_sequences.clear()
        self._enqueue_ack_times.clear()
        self._last_result_sequence = 0
        self._last_tracks = frozen.tracks
        self._initial_decisions = frozen.handoffs
        self._initial_snapshot_image = (
            None if self.continuous else np.ascontiguousarray(frame.image).copy()
        )
        self._initial_queue_staged = False
        self._conveyor_start_pending = False
        self._plc_reset_acknowledged = False
        self._release_pending = False
        self._operator_stop_requested = False
        self._operator_stop_phase = None
        # Continuous startup processes the latest frame when transport becomes
        # enabled, including parts already present in the initial frame.
        self._last_tracking_timestamp = 0.0 if self.continuous else frame.timestamp
        self._enqueue_requested_at = None
        self._enqueue_written_at = None
        self._finishing_track_ids.clear()
        self._finish_allowed_ids.clear()
        self._last_plc_warning_counter = self.snapshot.reorientation_warning_counter
        if self.continuous:
            self._health_timer.start()
        plan = build_write_plan(self.profile, actuate=False)
        self._set_state(BatchState.STARTING)
        self.tracks_changed.emit(self._last_tracks)
        self._emit_counters()
        self.pressure.write("batch_safe_stop", plan.safe_stop, True)

    def start_run(self) -> None:
        self.start_cycle()

    def prepare_next_cycle(self) -> None:
        if self.state not in {BatchState.COMPLETE, BatchState.FAULT}:
            raise RuntimeError("The production run has not finished")
        self._run_id = ""
        self._session = None
        self._set_state(BatchState.OFFLINE)
        self._refresh_preflight()

    def accept_inference(self, frame: InferenceFrame) -> TrackerUpdate | None:
        frame = replace(frame, detections=filter_input_detections(
            frame.detections, frame.image.shape[1], self.input_line_ratio
        ))
        if (
            self.continuous
            and self.state in {BatchState.STARTING, BatchState.RUNNING, BatchState.FINISHING}
            and self._latest_frame is not None
            and frame.timestamp <= self._latest_frame.timestamp
        ):
            return None
        self._latest_frame = frame
        self.set_camera_fresh(time.time() - frame.timestamp <= 1.0)
        if self.continuous:
            if self.state == BatchState.STARTING:
                # Preparation owns motion until its completion callback. Keep
                # the latest detections for handoff once the belt is enabled.
                return None
            if self.state in {BatchState.RUNNING, BatchState.FINISHING}:
                return self._accept_continuous_frame(frame)
        if (
            self.state
            not in {
                BatchState.NO_CONFIG,
                BatchState.OFFLINE,
                BatchState.READY,
                BatchState.COMPLETE,
                BatchState.FAULT,
            }
            or self.part is None
        ):
            return None
        mapping = self.part.recognition_mapping
        update = snapshot_queue(frame, mapping, pose_cutoff_ratio=self.pose_cutoff_ratio)
        self._last_tracks = update.tracks
        self.tracks_changed.emit(update.tracks)
        self._emit_counters()
        self._refresh_preflight()
        return update

    def _accept_continuous_frame(self, frame: InferenceFrame) -> TrackerUpdate | None:
        if frame.timestamp <= self._last_tracking_timestamp or self.part is None:
            return None
        self._last_tracking_timestamp = frame.timestamp
        update = self.tracker.update(frame, self.part.recognition_mapping)
        self._last_tracks = update.tracks
        self.tracks_changed.emit(update.tracks)
        self._emit_counters()
        if update.fault:
            self._fault("tracking", update.fault)
            return update
        if self.state == BatchState.FINISHING and any(
            track.track_id not in self._finish_allowed_ids for track in update.tracks
        ):
            self._fault("feed_after_stop", "A new workpiece was added after Stop production")
            return update
        for decision in update.handoffs:
            if self.state not in {BatchState.RUNNING, BatchState.FINISHING}:
                break
            if decision.pose_id is None:
                self._warn(
                    "pose_uncertain",
                    "YOLO class at the handoff line is unmapped; "
                    "this part will pass without firing",
                )
            self._queue_decision(decision, frame.image)
            self._finishing_track_ids.discard(decision.track_id)
        self._finish_continuous_when_ready()
        return update

    def _finish_continuous_when_ready(self) -> None:
        if (
            self.state == BatchState.FINISHING
            and not self._finishing_track_ids
            and self._pending_enqueue is None
            and not self._handoff_queue
        ):
            self._health_timer.stop()
            self._set_state(
                BatchState.DRAINING, "Finishing accepted parts; do not add more workpieces"
            )
            self.pressure.write("batch_finish", {"MAIN.GuiReorientationFinish": True}, True)

    def _check_continuous_health(self) -> None:
        if self.state not in {BatchState.STARTING, BatchState.RUNNING, BatchState.FINISHING}:
            return
        if (
            not self.model_ready
            or not self.camera_fresh
            or self._latest_frame is None
            or time.time() - self._latest_frame.timestamp > 1.0
        ):
            self._fault(
                "vision_stale", "Fresh camera and YOLO frames are required during continuous feed"
            )
            return
        # The ADS worker may be finishing a status poll when a commit is queued.
        # Give the write and its subsequent PLC acknowledgement separate bounds,
        # matching the ten-second startup handshake allowance.
        started_at = (
            self._enqueue_written_at
            if self._enqueue_written_at is not None
            else self._enqueue_requested_at
        )
        if started_at is not None and time.monotonic() - started_at > 10.0:
            pending_sequence = (
                self._pending_enqueue[0].sequence_id if self._pending_enqueue is not None else 0
            )
            self._fault(
                "queue_ack_timeout",
                f"PLC did not acknowledge part {pending_sequence} within ten seconds "
                f"(last acknowledgement: {self.snapshot.batch_enqueue_ack})",
            )

    def _queue_decision(self, decision: PartDecision, image: np.ndarray) -> None:
        if self._planner is None or self._session is None:
            self._fault("no_planner", "No queue planner is configured")
            return
        occupied = (
            self.snapshot.batch_queue_depth
            + len(self._handoff_queue)
            + (1 if self._pending_enqueue else 0)
        )
        if occupied >= self.snapshot.batch_queue_capacity:
            self._fault("queue_full", "PLC part queue is full")
            return
        self._sequence += 1
        record = self._planner.build(self._sequence, decision)
        try:
            image_path = self.journal.save_part_image(self._session, record.sequence_id, image)
        except Exception as exc:
            self._fault("part_image", f"Part image could not be saved: {exc}")
            return
        relative_image = str(image_path.relative_to(self._session))
        self._handoff_queue.append((record, decision, image, relative_image))
        self._send_next_enqueue()

    def _send_next_enqueue(self) -> None:
        if (
            self.state == BatchState.FAULT
            or self._pending_enqueue is not None
            or not self._handoff_queue
        ):
            return
        self._pending_enqueue = self._handoff_queue.popleft()
        record = self._pending_enqueue[0]
        self._enqueue_requested_at = time.monotonic()
        self._enqueue_written_at = None
        self.pressure.write(
            f"batch_enqueue:{record.sequence_id}",
            queue_record_values(record),
            False,
        )

    def _stage_initial_queue(self) -> None:
        image = self._initial_snapshot_image
        if image is None:
            self._fault("snapshot_missing", "The frozen production snapshot is unavailable")
            return
        for decision in self._initial_decisions:
            if decision.pose_id is None:
                self._warn(
                    "snapshot_uncertain",
                    f"Workpiece {decision.track_id} has no usable initial pose; "
                    "it remains in the fixed queue and will pass without actuation",
                )
            self._queue_decision(decision, image)
            if self.state == BatchState.FAULT:
                return
        self._initial_queue_staged = True
        self._start_conveyor_when_queue_is_acknowledged()

    def _start_conveyor_when_queue_is_acknowledged(self) -> None:
        if (
            self.state != BatchState.STARTING
            or not self._initial_queue_staged
            or self._pending_enqueue is not None
            or self._handoff_queue
            or self._conveyor_start_pending
        ):
            return
        self._conveyor_start_pending = True
        self.pressure.write(
            "batch_conveyor_start",
            {"MAIN.GuiConveyorEnabled": True},
            True,
        )

    def stop(self) -> None:
        if self.state == BatchState.STARTING:
            self._fault("operator_stop", "Production start was cancelled by the operator")
            return
        if self.state not in {BatchState.RUNNING, BatchState.FINISHING, BatchState.DRAINING}:
            return
        self._operator_stop_requested = True
        self._operator_stop_phase = "safe"
        self._preparation_name = None
        self.pressure.cancel_preparation()
        self._clear_handshake()
        self._health_timer.stop()
        self._heartbeat_timer.stop()
        self._handoff_queue.clear()
        self._pending_enqueue = None
        self._pending_parts.clear()
        self._enqueue_requested_at = None
        self._enqueue_written_at = None
        self._conveyor_start_pending = False
        self._release_pending = False
        self._finishing_track_ids.clear()
        self._finish_allowed_ids.clear()
        self._set_state(BatchState.STOPPING, "Stopping conveyor and nozzle arrays")
        self.pressure.write(
            f"batch_stop:{self._run_id}",
            {
                "MAIN.GuiReorientationAbort": True,
                "MAIN.GuiReorientationStart": False,
                "MAIN.GuiReorientationFinish": False,
                "MAIN.GuiConveyorEnabled": False,
                **{f"MAIN.GuiArrayEnabled{i}": False for i in range(1, 5)},
            },
            True,
        )

    def abort_run(self) -> None:
        if self.state in {
            BatchState.STARTING,
            BatchState.RUNNING,
            BatchState.FINISHING,
            BatchState.DRAINING,
            BatchState.STOPPING,
        }:
            self._fault("application_stop", "Production stopped because the application is closing")

    def finish_run(self) -> None:
        self.stop()

    def _operation_finished(self, name: str) -> None:
        if self._operator_stop_requested:
            if self.state == BatchState.STOPPING:
                if name == f"batch_stop:{self._run_id}" and self._operator_stop_phase == "safe":
                    self._operator_stop_phase = "owner"
                    self.pressure.write(
                        f"batch_stop_owner:{self._run_id}",
                        {"MAIN.GuiReorientationControlActive": False},
                        True,
                    )
                elif (
                    name == f"batch_stop_owner:{self._run_id}"
                    and self._operator_stop_phase == "owner"
                ):
                    self._operator_stop_phase = None
                    if self._session is not None:
                        try:
                            self.journal.finish_batch(
                                self._session, state="stopped", detail="Stopped by operator"
                            )
                        except Exception as exc:
                            self._set_state(BatchState.FAULT, f"run_logging: {exc}")
                            return
                    self._set_state(BatchState.COMPLETE, "Stopped by operator")
            return
        if name.startswith("batch_prepare:"):
            if name != self._preparation_name or self.state != BatchState.STARTING:
                return
            self._preparation_name = None
            plan = build_write_plan(self.profile, actuate=False)
            self.pressure.write("batch_configuration", dict(plan.configuration), True)
            return
        if (
            name
            in {
                "batch_safe_stop",
                "batch_configuration",
                "batch_reset",
                "batch_start",
                "batch_start_end",
                "batch_conveyor_start",
            }
            and self.state != BatchState.STARTING
        ):
            return
        if name == "batch_safe_stop" and self.profile is not None:
            self._set_state(BatchState.STARTING, "Preparing both Beckhoff conveyor motors")
            self._preparation_name = f"batch_prepare:{self._run_id}"
            self.pressure.prepare_conveyor(self._preparation_name)
        elif name == "batch_configuration":
            self._heartbeat = (int(self.snapshot.heartbeat_ack) + 1) & 0xFFFFFFFF
            self.pressure.write(
                "batch_reset",
                {
                    "MAIN.GuiReorientationHeartbeat": self._heartbeat,
                    "MAIN.GuiReorientationControlActive": True,
                    "MAIN.GuiReorientationReset": True,
                    "MAIN.GuiReorientationAbort": False,
                    "MAIN.GuiReorientationStart": False,
                    "MAIN.GuiReorientationFinish": False,
                },
                False,
            )
        elif name == "batch_reset":
            self._heartbeat_timer.start()
            self.pressure.write(
                "batch_start",
                {
                    "MAIN.GuiReorientationStart": True,
                    # The frozen queue is committed before physical motion.
                    "MAIN.GuiConveyorEnabled": False,
                },
                True,
            )
        elif name == "batch_start":
            self._begin_handshake("batch_start_ack")
        elif name == "batch_start_end":
            if self.continuous:
                self._initial_queue_staged = True
                self._start_conveyor_when_queue_is_acknowledged()
            else:
                self._stage_initial_queue()
        elif name.startswith("batch_enqueue:"):
            # The PLC acknowledgement is authoritative; a successful ADS write
            # alone must never release the next staging record.
            if (
                self._pending_enqueue is not None
                and name == f"batch_enqueue:{self._pending_enqueue[0].sequence_id}"
                and self.state in {BatchState.STARTING, BatchState.RUNNING, BatchState.FINISHING}
                and self._enqueue_written_at is None
            ):
                self._enqueue_written_at = time.monotonic()
        elif name.startswith("batch_result_ack:"):
            pass
        elif name == "batch_conveyor_start":
            self._conveyor_start_pending = False
            if self.continuous:
                self._set_state(
                    BatchState.RUNNING, "Belt running - add workpieces one at a time from the right"
                )
                if self._latest_frame is not None:
                    self._accept_continuous_frame(self._latest_frame)
                return
            self._set_state(
                BatchState.DRAINING,
                "Frozen queue running; conveyor stops after every part passes LB8",
            )
            self.pressure.write("batch_finish", {"MAIN.GuiReorientationFinish": True}, True)
        elif name == "batch_finish":
            pass
        elif name == "batch_release_safe":
            self.pressure.write(
                "batch_release_owner",
                {"MAIN.GuiReorientationControlActive": False},
                True,
            )
        elif name == "batch_release_owner":
            self._heartbeat_timer.stop()
            self._health_timer.stop()
            self._release_pending = False
            if self._session is not None:
                try:
                    self.journal.finish_batch(self._session, state="complete")
                except Exception as exc:
                    self._set_state(BatchState.FAULT, f"run_logging: {exc}")
                    return
            self._set_state(BatchState.COMPLETE)

    def _operation_failed(self, name: str, error: str) -> None:
        if self._operator_stop_requested and name not in {
            f"batch_stop:{self._run_id}", f"batch_stop_owner:{self._run_id}"
        }:
            return
        if name.startswith("manual_conveyor"):
            return
        if name.startswith("batch_prepare:") and name != self._preparation_name:
            return
        self._fault(f"ads_{name}", error)

    def _on_snapshot(self, snapshot: PlcSnapshot) -> None:
        self.snapshot = snapshot
        self._emit_counters()
        if self._operator_stop_requested and self.state in {
            BatchState.STOPPING, BatchState.COMPLETE
        }:
            return
        self._consume_plc_warning(snapshot)
        if self.state == BatchState.FAULT:
            return
        if (
            self.continuous
            and self.state in {BatchState.RUNNING, BatchState.FINISHING}
            and (snapshot.stepper_error or snapshot.vtem_error_codes != (0, 0))
        ):
            self._fault("drive", "Beckhoff conveyor or valve drive fault")
            return
        if self._handshake_phase == "batch_start_ack":
            if snapshot.reorientation_state == 20 and snapshot.reorientation_fault_code == 0:
                self._plc_reset_acknowledged = True
                self._clear_handshake()
                self.pressure.write("batch_start_end", {"MAIN.GuiReorientationStart": False}, True)
            return
        if (
            self.continuous
            and self._pending_enqueue is not None
            and snapshot.batch_enqueue_ack > self._pending_enqueue[0].sequence_id
        ):
            self._fault("queue_ack_sequence", "PLC acknowledged a different part sequence")
            return
        if self._pending_enqueue and (
            snapshot.batch_enqueue_ack >= self._pending_enqueue[0].sequence_id
        ):
            record, decision, _image, image_path = self._pending_enqueue
            self._pending_parts[record.sequence_id] = (record, decision, image_path)
            self._enqueue_ack_times[record.sequence_id] = time.time()
            self._pending_enqueue = None
            self._enqueue_requested_at = None
            self._enqueue_written_at = None
            self.part_queued.emit(record)
            self._send_next_enqueue()
            self._start_conveyor_when_queue_is_acknowledged()
            self._finish_continuous_when_ready()
            self._emit_counters()
        if (
            snapshot.batch_result_available
            and snapshot.batch_result_sequence != self._last_result_sequence
        ):
            self._consume_result(
                PartResult(
                    snapshot.batch_result_sequence,
                    snapshot.batch_result_triggered_mask,
                    snapshot.batch_result_fault_code,
                )
            )
        if self.state in {BatchState.NO_CONFIG, BatchState.OFFLINE, BatchState.READY}:
            self._refresh_preflight()
            return
        if snapshot.reorientation_fault_code and (
            self.state != BatchState.STARTING or self._plc_reset_acknowledged
        ):
            self._fault(
                f"plc_{snapshot.reorientation_fault_code}",
                self._plc_fault_text(snapshot.reorientation_fault_code),
            )
        elif (
            self.state == BatchState.DRAINING
            and snapshot.reorientation_state == 40
            and not self._release_pending
        ):
            self._release_pending = True
            safe_values = {
                "MAIN.GuiConveyorEnabled": False,
                "MAIN.GuiArrayEnabled1": False,
                "MAIN.GuiArrayEnabled2": False,
                "MAIN.GuiArrayEnabled3": False,
                "MAIN.GuiArrayEnabled4": False,
                "MAIN.GuiReorientationFinish": False,
            }
            self.pressure.write("batch_release_safe", safe_values, True)

    def _consume_plc_warning(self, snapshot: PlcSnapshot) -> None:
        counter = snapshot.reorientation_warning_counter
        if self._last_plc_warning_counter is None:
            self._last_plc_warning_counter = counter
            return
        if counter == self._last_plc_warning_counter:
            return
        self._last_plc_warning_counter = counter
        if snapshot.reorientation_warning_code == 1:
            skipped = [
                str(index)
                for index in range(1, 9)
                if snapshot.reorientation_warning_skipped_barrier_mask & (1 << (index - 1))
            ]
            skipped_text = ", ".join(f"LB{index}" for index in skipped) or "unknown"
            self._warn(
                "plc_airborne_sensor_skip",
                f"Part {snapshot.reorientation_warning_sequence} skipped {skipped_text} "
                "after its final active nozzle array; "
                f"tracking resumed at LB{snapshot.reorientation_warning_sensor}",
            )
        elif snapshot.reorientation_warning_code == 2:
            self._warn(
                "plc_airborne_duplicate_edge",
                f"Ignored an extra edge from airborne part "
                f"{snapshot.reorientation_warning_previous_sequence} at "
                f"LB{snapshot.reorientation_warning_sensor}; still waiting for part "
                f"{snapshot.reorientation_warning_sequence}",
            )
        elif snapshot.reorientation_warning_code == 3:
            self._warn(
                "plc_light_barrier_no_clear_ignored",
                f"Ignored another edge at LB{snapshot.reorientation_warning_sensor} "
                "because the sensor had not cleared; production continues",
            )
        elif snapshot.reorientation_warning_code == 4:
            self._warn(
                "plc_light_barrier_queue_edge_ignored",
                f"Ignored LB{snapshot.reorientation_warning_sensor} edge for part "
                f"{snapshot.reorientation_warning_sequence} because no matching queue "
                "entry exists; production continues",
            )
        elif snapshot.reorientation_warning_code == 5:
            self._warn(
                "plc_light_barrier_order_edge_ignored",
                f"Ignored out-of-order LB{snapshot.reorientation_warning_sensor} edge for "
                f"part {snapshot.reorientation_warning_sequence} "
                f"(previous sensor at part "
                f"{snapshot.reorientation_warning_previous_sequence}); production continues",
            )

    def _consume_result(self, result: PartResult) -> None:
        self._last_result_sequence = result.sequence_id
        pending = self._pending_parts.pop(result.sequence_id, None)
        if pending is None:
            self._fault(
                "result_sequence",
                f"PLC returned unknown part sequence {result.sequence_id}",
            )
            return
        record, decision, image_path = pending
        source_profiles = tuple(
            self._roadmap_profiles[edge_id]
            for edge_id in record.transition_edge_ids
            if edge_id in self._roadmap_profiles
        )
        if (
            not source_profiles
            and self.profile is not None
            and not self.part.is_manual_configuration
        ):
            source_profiles = (self.profile,)
        if self._session is not None:
            self.journal.append_part(
                self._session,
                {
                    "run_id": self._run_id,
                    "sequence_id": record.sequence_id,
                    "track_id": record.track_id,
                    "part_name": self.part.part_name if self.part else "",
                    "config_path": str(self.part.source_path if self.part else ""),
                    "config_sha256": self._config_hash,
                    "model_path": str(self.part.model_path if self.part else ""),
                    "model_sha256": self._model_hash,
                    "handoff_timestamp": datetime.fromtimestamp(record.timestamp, UTC).isoformat(),
                    "queue_ack_timestamp": datetime.fromtimestamp(
                        self._enqueue_ack_times.pop(record.sequence_id, time.time()), UTC
                    ).isoformat(),
                    "completed_timestamp": datetime.now(UTC).isoformat(),
                    "pose_id": record.pose_id,
                    "target_pose": record.target_pose,
                    "decision": record.decision_code.name.lower(),
                    "reason": record.reason,
                    "confidence": decision.confidence,
                    "observations": [
                        {
                            "pose_id": observation.pose_id,
                            "class_id": observation.class_id,
                            "confidence": observation.confidence,
                            "timestamp": observation.timestamp,
                        }
                        for observation in decision.observations
                    ],
                    "bbox": decision.bbox,
                    "transition_edge_ids": record.transition_edge_ids,
                    "profile_paths": [str(profile.source_path) for profile in source_profiles],
                    "profile_sha256": [profile.sha256 for profile in source_profiles],
                    "array_profiles": [
                        {
                            "index": array.index,
                            "enabled": array.enabled,
                            "nozzle_mask": array.nozzle_mask,
                            "pressure_mbar": array.pressure_mbar,
                            "delay_ms": array.delay_ms,
                            "pulse_duration_ms": array.pulse_duration_ms,
                            "offset_mm": array.offset_mm,
                        }
                        for array in record.arrays
                    ],
                    "conveyor_speed_mm_per_sec": (
                        self.profile.conveyor_speed_mm_per_sec if self.profile else None
                    ),
                    "ur_ry_angle_deg": (self.profile.ur_ry_angle_deg if self.profile else None),
                    "light_addresses": self._light_addresses,
                    "expected_array_mask": record.expected_array_mask,
                    "triggered_array_mask": result.triggered_array_mask,
                    "sensor_sequences": self.snapshot.batch_sensor_sequences,
                    "plc_fault_code": result.fault_code,
                    "image": image_path,
                    "status": "completed" if result.fault_code == 0 else "fault",
                },
            )
        self._logged_sequences.add(result.sequence_id)
        if self.continuous:
            self._logged_sequences.discard(result.sequence_id - self.snapshot.batch_queue_capacity)
        self.part_result.emit(result)
        self.pressure.write(
            f"batch_result_ack:{result.sequence_id}",
            {"MAIN.GuiReorientationResultAck": result.sequence_id},
            False,
        )
        if result.fault_code:
            self._fault(
                f"part_result_{result.fault_code}",
                f"PLC reported a fault for part {result.sequence_id}",
            )

    def _emit_counters(self) -> None:
        next_record = (
            self._pending_enqueue[0]
            if self._pending_enqueue is not None
            else (self._handoff_queue[0][0] if self._handoff_queue else None)
        )
        self.counters_changed.emit(
            {
                "production_mode": self.production_mode.value,
                "visible": len(self._last_tracks),
                "confirmed": sum(
                    track.confirmed_pose_id is not None for track in self._last_tracks
                ),
                "queued": self._sequence,
                "entered": self.snapshot.batch_entered_count,
                "completed": self.snapshot.batch_completed_count,
                "bypass": self.snapshot.batch_bypass_count,
                "queue_depth": self.snapshot.batch_queue_depth,
                "queue_capacity": self.snapshot.batch_queue_capacity,
                "sensor_sequences": self.snapshot.batch_sensor_sequences,
                "barrier_states": self.snapshot.light_barriers_stable,
                "next": (
                    f"#{next_record.sequence_id} Pose "
                    f"{next_record.pose_id if next_record.pose_id is not None else '?'} · "
                    f"mask 0x{next_record.expected_array_mask:X}"
                    if next_record is not None
                    else "–"
                ),
            }
        )

    def _send_heartbeat(self) -> None:
        self._heartbeat = (self._heartbeat + 1) & 0xFFFFFFFF
        self.pressure.write("batch_heartbeat", {"MAIN.GuiReorientationHeartbeat": self._heartbeat})

    def _begin_handshake(self, phase: str) -> None:
        self._handshake_phase = phase
        self._handshake_timer.start()

    def _clear_handshake(self) -> None:
        self._handshake_phase = None
        self._handshake_timer.stop()

    def _handshake_timeout(self) -> None:
        phase = self._handshake_phase or "unknown"
        self._clear_handshake()
        self._fault(
            "plc_handshake",
            "PLC start timed out: "
            f"{phase}; state={self.snapshot.reorientation_state}, "
            f"fault={self.snapshot.reorientation_fault_code}, "
            f"heartbeat_alive={self.snapshot.heartbeat_alive}, "
            f"heartbeat_ack={self.snapshot.heartbeat_ack}",
        )

    def _connection_changed(self, connected: bool, detail: str) -> None:
        if not connected:
            self.snapshot = replace(self.snapshot, connected=False)
        if not connected and self.state in {
            BatchState.STARTING,
            BatchState.RUNNING,
            BatchState.FINISHING,
            BatchState.DRAINING,
            BatchState.STOPPING,
        }:
            self._fault("ads_lost", detail or "ADS connection lost")
        elif self.state in {BatchState.NO_CONFIG, BatchState.OFFLINE, BatchState.READY}:
            self._refresh_preflight()

    def _plc_fault_text(self, code: int) -> str:
        if code == 96:
            detail = {
                1: "sensor did not clear",
                2: "queue entry missing/mismatched",
                3: "previous sensor has not seen this part",
            }.get(self.snapshot.reorientation_fault_detail, "unknown legacy detail")
            sequences = "/".join(str(value) for value in self.snapshot.batch_sensor_sequences)
            return (
                f"PLC LB{self.snapshot.reorientation_fault_sensor} fault: {detail}; "
                f"expected part {self.snapshot.reorientation_fault_expected_sequence}, "
                f"previous={self.snapshot.reorientation_fault_previous_sequence}, "
                f"queue_ack={self.snapshot.reorientation_fault_queue_ack}, "
                f"slot={self.snapshot.reorientation_fault_queue_slot_sequence}, "
                f"LB1→8={sequences}, "
                "stable_mask="
                f"0x{self.snapshot.reorientation_fault_barrier_stable_mask:02X}"
            )
        return {
            90: "PLC latched an operator/GUI abort",
            91: "PLC heartbeat watchdog expired",
            92: "PLC rejected an invalid queue or result handshake",
            93: "PLC part queue capacity was exhausted",
            94: "PLC reported a Beckhoff conveyor or VTEM drive fault",
            95: "PLC per-array job FIFO overflowed",
        }.get(code, "PLC batch execution fault")

    def _warn(self, code: str, text: str) -> None:
        self.warning_raised.emit(code, text)

    def _fault(self, code: str, text: str) -> None:
        if self.state == BatchState.FAULT:
            return
        self._preparation_name = None
        self._health_timer.stop()
        self.pressure.cancel_preparation()
        self._clear_handshake()
        self._heartbeat_timer.stop()
        self.pressure.write(
            "batch_abort",
            {
                "MAIN.GuiReorientationAbort": True,
                "MAIN.GuiConveyorEnabled": False,
                "MAIN.GuiArrayEnabled1": False,
                "MAIN.GuiArrayEnabled2": False,
                "MAIN.GuiArrayEnabled3": False,
                "MAIN.GuiArrayEnabled4": False,
            },
            False,
        )
        if self._session is not None:
            try:
                self.journal.finish_batch(
                    self._session,
                    state="fault",
                    detail=f"{code}: {text}",
                )
            except Exception as exc:
                text = f"{text}; fault log could not be written: {exc}"
        self._set_state(BatchState.FAULT, f"{code}: {text}")

    def _set_state(self, state: BatchState, detail: str = "") -> None:
        if self.state == state and not detail:
            return
        self.state = state
        self.state_changed.emit(state, detail)
