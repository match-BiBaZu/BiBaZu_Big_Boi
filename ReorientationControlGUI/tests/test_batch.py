from __future__ import annotations

from pathlib import Path

from bibazu_reorientation.batch import PartQueuePlanner, queue_record_values
from bibazu_reorientation.models import (
    ArrayProfile,
    ConveyorCalibration,
    PartDecision,
    PartDecisionCode,
    PartDefinition,
    PoseDefinition,
    PressureProfile,
    TransitionSpec,
)


def profile(tmp_path: Path) -> PressureProfile:
    source = tmp_path / "profile.json"
    source.write_text("{}", encoding="utf-8")
    return PressureProfile(
        source,
        9,
        None,
        18.0,
        20,
        (True,) * 8,
        (False,) * 8,
        True,
        False,
        100.0,
        1000.0,
        ConveyorCalibration(315.0, 0.3296, True),
        (15.0,) * 4,
        (10.0,) * 4,
        (
            ArrayProfile(1, False, (False,) * 6, 3000, 0, 50, 0.0),
            ArrayProfile(2, True, (True, False, False, False, False, False), 5770, 2, 27, 28.0),
            ArrayProfile(3, False, (False,) * 6, 3000, 0, 50, 0.0),
            ArrayProfile(4, False, (False,) * 6, 3000, 0, 50, 0.0),
        ),
        "hash",
    )


def definition(tmp_path: Path) -> PartDefinition:
    return PartDefinition(
        1,
        "Kk1a",
        tmp_path / "best.pt",
        (PoseDefinition(1, "Pose 1", 0), PoseDefinition(2, "Pose 2", 1)),
        1,
        (TransitionSpec(2, 1, tmp_path / "profile.json", "2-to-1"),),
    )


def decision(pose_id: int | None) -> PartDecision:
    return PartDecision(7, pose_id, 0.9, (), (1.0, 2.0, 3.0, 4.0), 12.0, "confirmed")


def test_target_and_uncertain_records_disable_every_array(tmp_path: Path) -> None:
    planner = PartQueuePlanner(definition(tmp_path), profile(tmp_path))

    target = planner.build(1, decision(1))
    uncertain = planner.build(2, decision(None))

    assert target.decision_code == PartDecisionCode.TARGET
    assert uncertain.decision_code == PartDecisionCode.BYPASS_UNCERTAIN
    assert target.expected_array_mask == uncertain.expected_array_mask == 0
    assert not any(row.enabled or row.nozzle_mask for row in target.arrays)


def test_actuation_record_contains_immutable_array_snapshot(tmp_path: Path) -> None:
    planner = PartQueuePlanner(definition(tmp_path), profile(tmp_path))

    record = planner.build(42, decision(2))
    values = queue_record_values(record)

    assert record.decision_code == PartDecisionCode.ACTUATE
    assert record.expected_array_mask == 2
    assert record.arrays[1].nozzle_mask == 1
    assert values["MAIN.GuiReorientationQueueSequence"] == 42
    assert values["MAIN.GuiReorientationQueueNozzleMask2"] == 1
    assert values["MAIN.GuiReorientationQueuePressureMbar2"] == 5770
    assert values["MAIN.GuiReorientationQueueCommit"] == 42


def test_missing_roadmap_path_becomes_zero_mask_bypass(tmp_path: Path) -> None:
    part = PartDefinition(
        2,
        "roadmap part",
        tmp_path / "best.pt",
        (PoseDefinition(5, "Pose 5", 0), PoseDefinition(10, "Pose 10", 1)),
        10,
        (TransitionSpec(5, 10, None, "missing-profile"),),
    )
    planner = PartQueuePlanner(part, profile(tmp_path), {})

    record = planner.build(1, decision(5))

    assert record.decision_code == PartDecisionCode.BYPASS_UNCERTAIN
    assert record.expected_array_mask == 0
    assert record.reason.startswith("bypass_unplanned")
