from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import UTC, datetime

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from bibazu_reorientation.batch import PartQueuePlanner, queue_record_values
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import (
    BatchState,
    InferenceFrame,
    PartDecision,
    PartDefinition,
    PartResult,
    PlcSnapshot,
    PressureProfile,
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
        self.ur_applied: float | None = None
        self.tracker = MultiPartTracker(handoff_line_ratio=handoff_line_ratio)
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
        self._run_id = ""
        self._session = None
        self._sequence = 0
        self._handoff_queue: deque[
            tuple[QueuedPartProfile, PartDecision, np.ndarray, str]
        ] = deque()
        self._pending_enqueue: tuple[
            QueuedPartProfile, PartDecision, np.ndarray, str
        ] | None = None
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
        self._release_pending = False
        self._light_addresses = ("", "")
        self._config_hash = ""
        self._model_hash = ""
        pressure.snapshot_changed.connect(self._on_snapshot)
        pressure.connection_changed.connect(self._connection_changed)
        pressure.operation_finished.connect(self._operation_finished)
        pressure.operation_failed.connect(self._operation_failed)

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
        self.tracker = MultiPartTracker(handoff_line_ratio=value)

    def set_configuration(self, part: PartDefinition, profile: PressureProfile) -> None:
        self._assert_configurable()
        self._clear_detection_snapshot()
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
        ur_ry_angle_deg: float | None,
    ) -> None:
        self._assert_configurable()
        self._clear_detection_snapshot()
        if not profiles_by_edge:
            raise ValueError("Assign at least one pressure profile before execution")
        first = next(iter(profiles_by_edge.values()))
        transport = compose_pressure_profiles(
            (first,),
            conveyor_speed_mm_per_sec=conveyor_speed_mm_per_sec,
            ur_ry_angle_deg=ur_ry_angle_deg,
        )
        self.part = part
        self.profile = transport
        self._transport_profile = transport
        self._roadmap_mode = True
        self._roadmap_profiles = dict(profiles_by_edge)
        self._planner = PartQueuePlanner(part, transport, profiles_by_edge)
        if self.ur_applied != ur_ry_angle_deg:
            self.ur_applied = None
        self._refresh_preflight()

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
        if changed and not ready and self.state in {
            BatchState.STARTING,
            BatchState.RUNNING,
            BatchState.FINISHING,
            BatchState.DRAINING,
        }:
            self._warn(
                "yolo_lost",
                "YOLO became unavailable after the production snapshot was frozen",
            )
        elif changed:
            self._refresh_preflight()

    def set_camera_fresh(self, ready: bool) -> None:
        changed = self.camera_fresh != ready
        self.camera_fresh = ready
        if changed and not ready and self.state in {
            BatchState.STARTING,
            BatchState.RUNNING,
            BatchState.FINISHING,
            BatchState.DRAINING,
        }:
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

    def set_ur_applied(self, angle: float | None) -> None:
        self.ur_applied = angle
        self._refresh_preflight()

    def preflight(self) -> dict[str, bool]:
        snapshot = self.snapshot
        profile = self.profile
        frame = self._latest_frame
        snapshot_part_count = len(frame.detections) if frame is not None else 0
        fresh_snapshot = (
            frame is not None
            and time.time() - frame.timestamp <= 1.0
            and snapshot_part_count > 0
        )
        return {
            "Configuration and at least one profile loaded": (
                self.part is not None and profile is not None and self._planner is not None
            ),
            "YOLO model warmed up": self.model_ready,
            "Fresh camera frame": self.camera_fresh,
            "Fresh production snapshot contains workpieces": fresh_snapshot,
            "Snapshot fits PLC queue": (
                snapshot_part_count > 0
                and snapshot_part_count <= snapshot.batch_queue_capacity
            ),
            "PLC batch contract connected": snapshot.connected,
            "Conveyor stopped": snapshot.conveyor_motion_state == 0 and not snapshot.stepper_busy,
            "Calibration valid": snapshot.calibration_valid,
            "Arrays idle / valves closed": snapshot.arrays_idle
            and snapshot.pending_mask == 0
            and snapshot.open_valve_mask == 0,
            "Drive and VTEM fault-free": (
                not snapshot.stepper_error and snapshot.vtem_error_codes == (0, 0)
            ),
            "UR angle acknowledged": profile is None
            or profile.ur_ry_angle_deg is None
            or self.ur_applied == profile.ur_ry_angle_deg,
        }

    def _refresh_preflight(self) -> None:
        checks = self.preflight()
        if checks != self._last_preflight:
            self._last_preflight = checks.copy()
            self.preflight_changed.emit(checks)
        if self.state in {BatchState.NO_CONFIG, BatchState.OFFLINE, BatchState.READY}:
            self._set_state(BatchState.READY if all(checks.values()) else BatchState.OFFLINE)

    def start_cycle(self) -> None:
        if self.state != BatchState.READY or self.part is None or self.profile is None:
            raise RuntimeError("Production-run preflight is incomplete")
        frame = self._latest_frame
        if frame is None or time.time() - frame.timestamp > 1.0:
            raise RuntimeError("No fresh YOLO snapshot is available")
        mapping = {pose.model_class_id: pose.id for pose in self.part.poses}
        frozen = snapshot_queue(frame, mapping)
        if not frozen.handoffs:
            raise RuntimeError("The production snapshot contains no workpieces")
        if len(frozen.handoffs) > self.snapshot.batch_queue_capacity:
            raise RuntimeError("The production snapshot exceeds the PLC queue capacity")
        self._run_id = f"run-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        profiles = tuple(self._roadmap_profiles.values()) or (self.profile,)
        self._session = self.journal.begin_batch(self._run_id, self.part, profiles)
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
        self._initial_snapshot_image = np.ascontiguousarray(frame.image).copy()
        self._initial_queue_staged = False
        self._conveyor_start_pending = False
        self._plc_reset_acknowledged = False
        self._release_pending = False
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
        self._latest_frame = frame
        self.set_camera_fresh(time.time() - frame.timestamp <= 1.0)
        if self.state not in {
            BatchState.NO_CONFIG,
            BatchState.OFFLINE,
            BatchState.READY,
            BatchState.COMPLETE,
            BatchState.FAULT,
        } or self.part is None:
            return None
        mapping = {pose.model_class_id: pose.id for pose in self.part.poses}
        update = snapshot_queue(frame, mapping)
        self._last_tracks = update.tracks
        self.tracks_changed.emit(update.tracks)
        self._emit_counters()
        self._refresh_preflight()
        return update

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
            image_path = self.journal.save_part_image(
                self._session, record.sequence_id, image
            )
        except Exception as exc:
            self._fault("part_image", f"Part image could not be saved: {exc}")
            return
        relative_image = str(image_path.relative_to(self._session))
        self._handoff_queue.append((record, decision, image, relative_image))
        self._send_next_enqueue()

    def _send_next_enqueue(self) -> None:
        if self._pending_enqueue is not None or not self._handoff_queue:
            return
        self._pending_enqueue = self._handoff_queue.popleft()
        record = self._pending_enqueue[0]
        self.pressure.write(
            f"batch_enqueue:{record.sequence_id}",
            queue_record_values(record),
            False,
        )

    def _stage_initial_queue(self) -> None:
        image = self._initial_snapshot_image
        if image is None or not self._initial_decisions:
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
        if self.state != BatchState.RUNNING:
            return
        self._set_state(BatchState.DRAINING, "Waiting for all queued parts to pass LB8")
        self.pressure.write("batch_finish", {"MAIN.GuiReorientationFinish": True}, True)

    def finish_run(self) -> None:
        self.stop()

    def _operation_finished(self, name: str) -> None:
        if name == "batch_safe_stop" and self.profile is not None:
            plan = build_write_plan(self.profile, actuate=False)
            batch_configuration = dict(plan.configuration)
            self.pressure.write("batch_configuration", batch_configuration, True)
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
            self._stage_initial_queue()
        elif name.startswith("batch_enqueue:"):
            # The PLC acknowledgement is authoritative; a successful ADS write
            # alone must never release the next staging record.
            pass
        elif name.startswith("batch_result_ack:"):
            pass
        elif name == "batch_conveyor_start":
            self._conveyor_start_pending = False
            self._set_state(
                BatchState.DRAINING,
                "Frozen queue running; conveyor stops after every part passes LB8",
            )
            self.pressure.write(
                "batch_finish", {"MAIN.GuiReorientationFinish": True}, True
            )
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
            self._release_pending = False
            if self._session is not None:
                try:
                    self.journal.finish_batch(self._session, state="complete")
                except Exception as exc:
                    self._set_state(BatchState.FAULT, f"run_logging: {exc}")
                    return
            self._set_state(BatchState.COMPLETE)

    def _operation_failed(self, name: str, error: str) -> None:
        if name.startswith("manual_conveyor"):
            return
        self._fault(f"ads_{name}", error)

    def _on_snapshot(self, snapshot: PlcSnapshot) -> None:
        self.snapshot = snapshot
        self._emit_counters()
        if self._handshake_phase == "batch_start_ack":
            if (
                snapshot.reorientation_state == 20
                and snapshot.reorientation_fault_code == 0
            ):
                self._plc_reset_acknowledged = True
                self._clear_handshake()
                self.pressure.write(
                    "batch_start_end", {"MAIN.GuiReorientationStart": False}, True
                )
            return
        if self._pending_enqueue and (
            snapshot.batch_enqueue_ack >= self._pending_enqueue[0].sequence_id
        ):
            record, decision, _image, image_path = self._pending_enqueue
            self._pending_parts[record.sequence_id] = (record, decision, image_path)
            self._enqueue_ack_times[record.sequence_id] = time.time()
            self._pending_enqueue = None
            self.part_queued.emit(record)
            self._send_next_enqueue()
            self._start_conveyor_when_queue_is_acknowledged()
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
        if not source_profiles and self.profile is not None:
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
                    "handoff_timestamp": datetime.fromtimestamp(
                        record.timestamp, UTC
                    ).isoformat(),
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
                    "profile_paths": [
                        str(profile.source_path) for profile in source_profiles
                    ],
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
                    "ur_ry_angle_deg": (
                        self.profile.ur_ry_angle_deg if self.profile else None
                    ),
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
                    f"#{next_record.sequence_id} Pose {next_record.pose_id or '?'} · "
                    f"mask 0x{next_record.expected_array_mask:X}"
                    if next_record is not None
                    else "–"
                ),
            }
        )

    def _send_heartbeat(self) -> None:
        self._heartbeat = (self._heartbeat + 1) & 0xFFFFFFFF
        self.pressure.write(
            "batch_heartbeat", {"MAIN.GuiReorientationHeartbeat": self._heartbeat}
        )

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
            self.snapshot.connected = False
        if not connected and self.state in {
            BatchState.STARTING,
            BatchState.RUNNING,
            BatchState.FINISHING,
            BatchState.DRAINING,
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
            94: "PLC reported an EL7047 or VTEM drive fault",
            95: "PLC per-array job FIFO overflowed",
        }.get(code, "PLC batch execution fault")

    def _warn(self, code: str, text: str) -> None:
        self.warning_raised.emit(code, text)

    def _fault(self, code: str, text: str) -> None:
        if self.state == BatchState.FAULT:
            return
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
