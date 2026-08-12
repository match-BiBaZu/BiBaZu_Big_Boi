from __future__ import annotations

import numpy as np

from bibazu_reorientation.models import Detection, InferenceFrame
from bibazu_reorientation.tracking import MultiPartTracker, snapshot_queue


def frame(*rows: tuple[int, float, int], timestamp: float) -> InferenceFrame:
    detections = tuple(
        Detection(
            class_id,
            f"class {class_id}",
            0.9,
            ((x - 5, y - 5), (x + 5, y - 5), (x + 5, y + 5), (x - 5, y + 5)),
            "detect",
        )
        for class_id, x, y in rows
    )
    return InferenceFrame(np.zeros((100, 100, 3), np.uint8), detections, 1.0, timestamp)


def test_tracks_multiple_parts_and_hands_each_off_once() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.30)
    mapping = {0: 10, 1: 5}
    decisions = []

    for index in range(5):
        update = tracker.update(
            frame(
                (1, 70 - index * 10, 30),
                (0, 95 - index * 10, 70),
                timestamp=1.0 + index,
            ),
            mapping,
        )
        decisions.extend(update.handoffs)
        assert not update.fault

    assert [(item.track_id, item.pose_id) for item in decisions] == [(1, 5)]
    assert len(decisions[0].observations) == 5

    for index in range(3):
        update = tracker.update(
            frame((0, 45 - index * 10, 70), timestamp=6.0 + index), mapping
        )
        decisions.extend(update.handoffs)
    assert [(item.track_id, item.pose_id) for item in decisions] == [(1, 5), (2, 10)]


def test_snapshot_queue_freezes_every_detection_in_leading_order() -> None:
    update = snapshot_queue(
        frame((1, 70, 30), (0, 25, 70), timestamp=1.0),
        {0: 10, 1: 5},
    )

    assert [(item.track_id, item.pose_id) for item in update.handoffs] == [(1, 10), (2, 5)]
    assert all(item.reason == "snapshot_confirmed" for item in update.handoffs)


def test_snapshot_queue_does_not_queue_overlapping_duplicate_classes() -> None:
    lower = Detection(
        0,
        "class 0",
        0.75,
        ((40, 30), (60, 30), (60, 50), (40, 50)),
        "detect",
    )
    higher = Detection(
        1,
        "class 1",
        0.95,
        ((41, 31), (59, 31), (59, 49), (41, 49)),
        "detect",
    )
    duplicate_frame = InferenceFrame(
        np.zeros((100, 100, 3), np.uint8),
        (lower, higher),
        1.0,
        1.0,
    )

    update = snapshot_queue(duplicate_frame, {0: 10, 1: 5})

    assert len(update.handoffs) == 1
    assert update.handoffs[0].pose_id == 5
    assert update.handoffs[0].confidence == 0.95


def test_wrong_class_resets_five_frame_streak() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.20)
    mapping = {0: 10, 1: 5}

    for index, class_id in enumerate((1, 1, 0, 1, 1, 1, 1)):
        update = tracker.update(
            frame((class_id, 80 - index * 5, 50), timestamp=float(index + 1)), mapping
        )
    assert update.tracks[0].confirmed_pose_id is None
    assert update.tracks[0].pose_streak == 4

    update = tracker.update(frame((1, 40, 50), timestamp=8.0), mapping)
    assert update.tracks[0].confirmed_pose_id == 5
    assert update.tracks[0].pose_streak == 5


def test_unconfirmed_part_crosses_as_bypass_decision() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.30)
    mapping = {0: 10}
    tracker.update(frame((0, 45, 50), timestamp=1.0), mapping)
    update = tracker.update(frame((0, 25, 50), timestamp=2.0), mapping)

    assert len(update.handoffs) == 1
    assert update.handoffs[0].pose_id is None
    assert update.handoffs[0].reason == "consensus_incomplete"


def test_lost_real_track_before_line_is_fault() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.20, max_missed_frames=2)
    mapping = {0: 10}
    tracker.update(frame((0, 80, 50), timestamp=1.0), mapping)
    tracker.update(frame((0, 70, 50), timestamp=2.0), mapping)
    tracker.update(frame(timestamp=3.0), mapping)
    tracker.update(frame(timestamp=4.0), mapping)
    update = tracker.update(frame(timestamp=5.0), mapping)

    assert "lost before the handoff line" in update.fault


def test_part_exiting_left_edge_is_not_a_direction_fault() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.20)
    mapping = {0: 10}
    tracker.update(frame((0, 8, 50), timestamp=1.0), mapping)
    update = tracker.update(frame((0, 25, 50), timestamp=2.0), mapping)

    assert not update.fault


def test_order_inversion_does_not_fault_in_queue_once_mode() -> None:
    tracker = MultiPartTracker(handoff_line_ratio=0.20)
    mapping = {0: 10}
    tracker.update(frame((0, 40, 30), (0, 60, 70), timestamp=1.0), mapping)
    update = tracker.update(frame((0, 55, 30), (0, 45, 70), timestamp=2.0), mapping)

    assert not update.fault
