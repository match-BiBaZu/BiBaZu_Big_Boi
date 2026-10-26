from __future__ import annotations

from bibazu_reorientation.config import TransitionResolver
from bibazu_reorientation.models import (
    PartDecision,
    PartDecisionCode,
    PartDefinition,
    PressureProfile,
    QueuedArrayProfile,
    QueuedPartProfile,
)
from bibazu_reorientation.profiles import compose_pressure_profiles


def _queued_arrays(
    profile: PressureProfile,
    *,
    actuate: bool,
) -> tuple[QueuedArrayProfile, QueuedArrayProfile, QueuedArrayProfile, QueuedArrayProfile]:
    rows = []
    for array in profile.arrays:
        nozzle_mask = sum(
            1 << index for index, enabled in enumerate(array.nozzles_enabled) if enabled
        )
        rows.append(
            QueuedArrayProfile(
                array.index,
                bool(actuate and array.active),
                nozzle_mask if actuate and array.active else 0,
                array.pressure_mbar,
                array.delay_ms,
                array.pulse_duration_ms,
                array.offset_mm,
                profile.force_response_delays_ms[array.index - 1],
                profile.force_single_nozzle_response_delays_ms[array.index - 1],
            )
        )
    return tuple(rows)  # type: ignore[return-value]


class PartQueuePlanner:
    """Resolve a camera decision into one immutable PLC queue record."""

    def __init__(
        self,
        part: PartDefinition,
        transport_profile: PressureProfile,
        profiles_by_edge: dict[str, PressureProfile] | None = None,
    ) -> None:
        self.part = part
        self.transport_profile = transport_profile
        self.profiles_by_edge = profiles_by_edge or {}

    def build(self, sequence_id: int, decision: PartDecision) -> QueuedPartProfile:
        pose_id = decision.pose_id
        reason = decision.reason
        profile = self.transport_profile
        edge_ids: tuple[str, ...] = ()
        code = PartDecisionCode.BYPASS_UNCERTAIN
        actuate = False

        if self.part.same_target_group(pose_id, self.part.target_pose):
            code = PartDecisionCode.TARGET
            reason = "already_target"
        elif pose_id is not None and pose_id < 0:
            reason = "bypass_omitted_group: no roadmap route available"
        elif pose_id is not None:
            try:
                if self.part.is_manual_configuration:
                    choices = tuple(
                        edge
                        for edge in self.part.transitions
                        if edge.from_pose == pose_id
                        and edge.to_pose == self.part.target_pose
                        and edge.pressure_profile is not None
                    )
                    if len(choices) != 1:
                        raise ValueError("no unique manual profile assigned for this start pose")
                    edge_ids = (choices[0].edge_id,)
                    profile = compose_pressure_profiles(
                        (self.profiles_by_edge[edge_ids[0]],),
                        conveyor_speed_mm_per_sec=self.transport_profile.conveyor_speed_mm_per_sec,
                    )
                elif self.part.is_roadmap_configuration:
                    transitions = TransitionResolver(self.part).plan(pose_id)
                    edge_ids = tuple(transition.edge_id for transition in transitions)
                    source_profiles = tuple(
                        self.profiles_by_edge[transition.edge_id] for transition in transitions
                    )
                    if not source_profiles:
                        raise ValueError("no transition is required")
                    profile = compose_pressure_profiles(
                        source_profiles,
                        conveyor_speed_mm_per_sec=(
                            self.transport_profile.conveyor_speed_mm_per_sec
                        ),
                    )
                else:
                    edge_ids = tuple(
                        transition.edge_id or f"{transition.from_pose}->{transition.to_pose}"
                        for transition in self.part.transitions
                    )
                actuate = profile.active_array_mask != 0
                if not actuate:
                    raise ValueError("resolved profile has no active nozzle array")
                code = PartDecisionCode.ACTUATE
                reason = "planned"
            except (KeyError, ValueError) as exc:
                reason = f"bypass_unplanned: {exc}"

        mask = profile.active_array_mask if actuate else 0
        return QueuedPartProfile(
            sequence_id,
            decision.track_id,
            pose_id,
            self.part.target_pose,
            code,
            reason,
            mask,
            _queued_arrays(profile, actuate=actuate),
            edge_ids,
            decision.timestamp,
        )


def queue_record_values(record: QueuedPartProfile) -> dict[str, int | float]:
    values: dict[str, int | float] = {
        "MAIN.GuiReorientationQueueSequence": record.sequence_id,
        "MAIN.GuiReorientationQueueTrackId": record.track_id,
        "MAIN.GuiReorientationQueuePoseId": max(0, record.pose_id or 0),
        "MAIN.GuiReorientationQueueDecisionCode": int(record.decision_code),
        "MAIN.GuiReorientationQueueArrayMask": record.expected_array_mask,
    }
    for array in record.arrays:
        suffix = str(array.index)
        values.update(
            {
                f"MAIN.GuiReorientationQueueNozzleMask{suffix}": array.nozzle_mask,
                f"MAIN.GuiReorientationQueuePressureMbar{suffix}": array.pressure_mbar,
                f"MAIN.GuiReorientationQueueDelayMs{suffix}": array.delay_ms,
                f"MAIN.GuiReorientationQueuePulseMs{suffix}": array.pulse_duration_ms,
                f"MAIN.GuiReorientationQueueOffsetMm{suffix}": array.offset_mm,
                f"MAIN.GuiReorientationQueueForceResponseMs{suffix}": (
                    array.force_response_delay_ms
                ),
                f"MAIN.GuiReorientationQueueForceSingleMs{suffix}": (
                    array.force_single_nozzle_response_delay_ms
                ),
            }
        )
    values["MAIN.GuiReorientationQueueCommit"] = record.sequence_id
    return values
