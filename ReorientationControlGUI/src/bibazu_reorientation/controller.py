from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from bibazu_reorientation.config import TransitionResolver
from bibazu_reorientation.hardware.pressure import PressureAdapter
from bibazu_reorientation.inference import PoseConsensus
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.models import (
    CycleResult,
    CycleState,
    InferenceFrame,
    PartDefinition,
    PlcSnapshot,
    PressureProfile,
)
from bibazu_reorientation.profiles import build_write_plan


class ReorientationController(QObject):
    state_changed = pyqtSignal(object, str)
    result_ready = pyqtSignal(object)
    preflight_changed = pyqtSignal(object)

    def __init__(
        self,
        pressure: PressureAdapter,
        journal: RunJournal | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.pressure = pressure
        self.journal = journal or RunJournal()
        self.state = CycleState.NO_CONFIG
        self.part: PartDefinition | None = None
        self.profile: PressureProfile | None = None
        self.snapshot = PlcSnapshot()
        self.consensus = PoseConsensus()
        self.model_ready = False
        self.camera_fresh = False
        self.lights_ready = (False, False)
        self.ur_applied: float | None = None
        self._decision_frame: InferenceFrame | None = None
        self._observations = ()
        self._decision_at: datetime | None = None
        self._exit_at: datetime | None = None
        self._light_addresses = ("", "")
        self._camera_lost_after_decision = False
        self._lights_lost_after_decision = False
        self._detected_pose: int | None = None
        self._started_at: datetime | None = None
        self._cycle_id = ""
        self._session = None
        self._plan = None
        self._finalizing = False
        self._terminal_state = CycleState.COMPLETE
        self._result_emitted = False
        self._heartbeat = 0
        self._barriers_clear_since: float | None = None
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(250)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)
        pressure.snapshot_changed.connect(self._on_snapshot)
        pressure.connection_changed.connect(self._connection_changed)
        pressure.operation_finished.connect(self._operation_finished)
        pressure.operation_failed.connect(self._operation_failed)

    def set_configuration(self, part: PartDefinition, profile: PressureProfile) -> None:
        if self.state not in {CycleState.NO_CONFIG, CycleState.OFFLINE, CycleState.READY}:
            raise RuntimeError("Konfiguration kann während eines Zyklus nicht gewechselt werden")
        self.part, self.profile = part, profile
        TransitionResolver(part).plan(3 - part.target_pose, part.target_pose)
        self._refresh_preflight()

    def set_model_ready(self, ready: bool) -> None:
        self.model_ready = ready
        self._refresh_preflight()

    def set_camera_fresh(self, ready: bool) -> None:
        self.camera_fresh = ready
        if not ready and self.state not in {
            CycleState.NO_CONFIG,
            CycleState.OFFLINE,
            CycleState.READY,
            CycleState.DETECTING,
            CycleState.COMPLETE,
            CycleState.ABORTED,
            CycleState.FAULT,
        }:
            self._camera_lost_after_decision = True
        self._refresh_preflight()

    def set_lights_ready(self, first: bool, second: bool) -> None:
        self.lights_ready = (first, second)
        if not (first and second) and self.state not in {
            CycleState.NO_CONFIG,
            CycleState.OFFLINE,
            CycleState.READY,
            CycleState.DETECTING,
            CycleState.COMPLETE,
            CycleState.ABORTED,
            CycleState.FAULT,
        }:
            self._lights_lost_after_decision = True
        self._refresh_preflight()

    def set_light_addresses(self, first: str, second: str) -> None:
        self._light_addresses = (first, second)

    def set_ur_applied(self, angle: float | None) -> None:
        self.ur_applied = angle
        self._refresh_preflight()

    def preflight(self) -> dict[str, bool]:
        p = self.profile
        s = self.snapshot
        return {
            "Konfiguration und Profil gültig": self.part is not None and p is not None,
            "YOLO-Modell aufgewärmt": self.model_ready,
            "Frisches Kamerabild": self.camera_fresh,
            "SPS verbunden": s.connected,
            "Förderband steht": s.conveyor_motion_state == 0 and not s.stepper_busy,
            "Kalibrierung gültig": s.calibration_valid,
            "Arrays idle / Ventile geschlossen": s.array_states == (2, 2, 2, 2)
            and s.pending_mask == 0
            and s.open_valve_mask == 0,
            "Antrieb und VTEM fehlerfrei": not s.stepper_error and s.vtem_error_codes == (0, 0),
            "Lichtschranken frei": self._barriers_clear_long_enough(),
            "Beide Leuchten bestätigt": all(self.lights_ready),
            "UR-Winkel quittiert": p is None
            or p.ur_ry_angle_deg is None
            or self.ur_applied == p.ur_ry_angle_deg,
        }

    def _refresh_preflight(self) -> None:
        checks = self.preflight()
        self.preflight_changed.emit(checks)
        if self.state in {CycleState.NO_CONFIG, CycleState.OFFLINE, CycleState.READY}:
            self._set_state(CycleState.READY if all(checks.values()) else CycleState.OFFLINE)

    def start_cycle(self) -> None:
        if self.state != CycleState.READY:
            raise RuntimeError("Preflight ist nicht vollständig")
        self._cycle_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        self._started_at = datetime.now(UTC)
        self._finalizing = False
        self._terminal_state = CycleState.COMPLETE
        self._result_emitted = False
        self._camera_lost_after_decision = False
        self._lights_lost_after_decision = False
        self.consensus.reset()
        self._set_state(CycleState.DETECTING)

    def prepare_next_cycle(self) -> None:
        if self.state not in {CycleState.COMPLETE, CycleState.ABORTED, CycleState.FAULT}:
            raise RuntimeError("Der aktuelle Zyklus ist noch nicht beendet")
        self._started_at = None
        self._cycle_id = ""
        self._session = None
        self._plan = None
        self._detected_pose = None
        self._decision_frame = None
        self._observations = ()
        self._set_state(CycleState.OFFLINE)
        self._refresh_preflight()

    def accept_inference(self, frame: InferenceFrame) -> None:
        self.set_camera_fresh(time.time() - frame.timestamp <= 1.0)
        if self.state != CycleState.DETECTING or self.part is None or self.profile is None:
            return
        mapping = {pose.model_class_id: pose.id for pose in self.part.poses}
        decision = self.consensus.add(frame, mapping)
        if decision is None:
            return
        if not all(self.preflight().values()):
            self.consensus.reset()
            return
        self._detected_pose = decision.pose_id
        self._decision_frame = frame
        self._observations = decision.observations
        self._decision_at = datetime.now(UTC)
        try:
            self._session = self.journal.begin(self._cycle_id, self.part, self.profile)
            self.journal.save_decision_image(self._session, frame.image)
        except Exception as exc:
            self._fault(
                "fault_logging", f"Entscheidungsbild konnte nicht gespeichert werden: {exc}"
            )
            return
        self._plan = build_write_plan(
            self.profile, actuate=decision.pose_id != self.part.target_pose
        )
        self._set_state(CycleState.DECIDED, f"Pose {decision.pose_id}")
        self._set_state(CycleState.STAGING)
        self.pressure.write("safe_stop", self._plan.safe_stop, True)

    def _operation_finished(self, name: str) -> None:
        if self._plan is None:
            return
        if name == "safe_stop":
            self.pressure.write("configuration", self._plan.configuration, True)
        elif name == "configuration":
            self.pressure.write(
                "ownership",
                {"MAIN.GuiReorientationControlActive": True, "MAIN.GuiReorientationReset": True},
                True,
            )
        elif name == "ownership":
            self.pressure.write("reset_end", {"MAIN.GuiReorientationReset": False}, True)
        elif name == "reset_end":
            self._heartbeat_timer.start()
            self._heartbeat = (self._heartbeat + 1) & 0xFFFFFFFF
            self.pressure.write(
                "heartbeat_initial",
                {"MAIN.GuiReorientationHeartbeat": self._heartbeat},
                True,
            )
        elif name == "heartbeat_initial":
            self.pressure.write("enables", self._plan.enables, True)
        elif name == "enables":
            self._set_state(CycleState.ARMED)
            self.pressure.write("start", {"MAIN.GuiReorientationStart": True}, True)
        elif name == "start":
            self.pressure.write("start_pulse_end", {"MAIN.GuiReorientationStart": False})
            self._set_state(CycleState.RUNNING)
        elif name == "release_safe":
            self.pressure.write(
                "release_owner", {"MAIN.GuiReorientationControlActive": False}, True
            )
        elif name == "release_owner":
            self._heartbeat_timer.stop()
            self._finish(self._terminal_state)
        elif name == "abort":
            self.pressure.write("release_safe", self._plan.safe_stop, True)

    def _operation_failed(self, name: str, error: str) -> None:
        self._fault(f"ads_{name}", error)

    def _on_snapshot(self, snapshot: PlcSnapshot) -> None:
        self.snapshot = snapshot
        if all(snapshot.light_barriers_stable):
            if self._barriers_clear_since is None:
                self._barriers_clear_since = time.monotonic()
        else:
            self._barriers_clear_since = None
        if self.state in {CycleState.NO_CONFIG, CycleState.OFFLINE, CycleState.READY}:
            self._refresh_preflight()
            return
        if snapshot.reorientation_fault_code:
            self._fault(f"plc_{snapshot.reorientation_fault_code}", "SPS-Reorientierungsfehler")
        elif self.state == CycleState.RUNNING and snapshot.exit_seen:
            self._exit_at = datetime.now(UTC)
            self._set_state(CycleState.DRAINING)
        elif self.state in {CycleState.RUNNING, CycleState.DRAINING} and snapshot.complete:
            if not self._finalizing:
                self._finalizing = True
                self._terminal_state = CycleState.COMPLETE
                self.pressure.write("release_safe", self._plan.safe_stop, True)

    def _barriers_clear_long_enough(self) -> bool:
        if self._barriers_clear_since is None:
            return False
        debounce_ms = self.profile.light_barrier_debounce_ms if self.profile else 20
        return time.monotonic() - self._barriers_clear_since >= (debounce_ms + 100) / 1000.0

    def _connection_changed(self, connected: bool, detail: str) -> None:
        if not connected and self.state not in {
            CycleState.NO_CONFIG,
            CycleState.OFFLINE,
            CycleState.READY,
            CycleState.COMPLETE,
            CycleState.ABORTED,
            CycleState.FAULT,
        }:
            self._fault("ads_disconnect", detail or "ADS-Verbindung verloren")

    def _send_heartbeat(self) -> None:
        self._heartbeat = (self._heartbeat + 1) & 0xFFFFFFFF
        self.pressure.write("heartbeat", {"MAIN.GuiReorientationHeartbeat": self._heartbeat})

    def stop(self) -> None:
        if self.state not in {CycleState.READY, CycleState.OFFLINE, CycleState.NO_CONFIG}:
            if self.state == CycleState.DETECTING:
                self._terminal_state = CycleState.ABORTED
                self._finish(CycleState.ABORTED, "manual_stop", "Vor Aktuierung abgebrochen")
                return
            if self._finalizing:
                return
            self._finalizing = True
            self._terminal_state = CycleState.ABORTED
            self._set_state(CycleState.ABORTING)
            self.pressure.write("abort", {"MAIN.GuiReorientationAbort": True}, True)

    def _fault(self, code: str, text: str) -> None:
        if self.state == CycleState.FAULT:
            return
        self._set_state(CycleState.FAULT, text)
        self._terminal_state = CycleState.FAULT
        self.pressure.write(
            "abort",
            {"MAIN.GuiReorientationAbort": True, "MAIN.GuiConveyorEnabled": False},
        )
        self._finish(CycleState.FAULT, code, text)

    def _finish(self, state: CycleState, code: str = "", text: str = "") -> None:
        if self.part is None or self._started_at is None or self._result_emitted:
            return
        self._result_emitted = True
        result = CycleResult(
            self._cycle_id,
            state,
            self.part.part_name,
            self._started_at,
            datetime.now(UTC),
            self._detected_pose,
            self.part.target_pose,
            (
                "pass_through"
                if self._detected_pose == self.part.target_pose
                else f"{self._detected_pose}_to_{self.part.target_pose}"
            ),
            0 if self._plan is None else self._plan.expected_array_mask,
            self.snapshot.triggered_array_mask,
            code,
            text,
        )
        details = {
            "decision_at": self._decision_at.isoformat() if self._decision_at else "",
            "lb6_at": self._exit_at.isoformat() if self._exit_at else "",
            "config_path": str(self.part.source_path or ""),
            "config_sha256": (
                self.journal.sha256(self.part.source_path) if self.part.source_path else ""
            ),
            "model_path": str(self.part.model_path),
            "model_sha256": self.journal.sha256(self.part.model_path),
            "profile_path": str(self.profile.source_path) if self.profile else "",
            "profile_sha256": self.profile.sha256 if self.profile else "",
            "profile_version": self.profile.source_version if self.profile else "",
            "observations": [
                {
                    "class_id": item.class_id,
                    "pose_id": item.pose_id,
                    "confidence": item.confidence,
                    "timestamp": item.timestamp,
                }
                for item in self._observations
            ],
            "ur_requested": self.profile.ur_ry_angle_deg if self.profile else None,
            "ur_applied": self.ur_applied,
            "light_addresses": self._light_addresses,
            "lights_manual_confirmed": self.lights_ready,
            "camera_lost_after_decision": self._camera_lost_after_decision,
            "lights_lost_after_decision": self._lights_lost_after_decision,
            "plc_state": self.snapshot.reorientation_state,
            "plc_fault": self.snapshot.reorientation_fault_code,
            "plc_cycle_counter": self.snapshot.cycle_counter,
            "lb6_exit_seen": self.snapshot.exit_seen,
            "array_velocities": self.snapshot.velocities,
            "array_delays": self.snapshot.delays,
            "avg_pressure_n1": self.snapshot.avg_pressure_n1,
            "avg_pressure_n2": self.snapshot.avg_pressure_n2,
            "watchdog": self.snapshot.reorientation_fault_code == 91,
            "image": f"{self._cycle_id}/decision.png" if self._session else "",
        }
        try:
            self.journal.finish(result, details)
        except Exception as exc:
            result = replace(
                result,
                state=CycleState.FAULT,
                error_code="fault_logging",
                error_text=str(exc),
            )
            state, text = CycleState.FAULT, str(exc)
        self.result_ready.emit(result)
        self._set_state(state, text)

    def _set_state(self, state: CycleState, detail: str = "") -> None:
        if self.state == state and not detail:
            return
        self.state = state
        self.state_changed.emit(state, detail)
