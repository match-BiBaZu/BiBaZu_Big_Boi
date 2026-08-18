from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from bibazu_reorientation.inference import (
    InferenceConfig,
    PoseConsensus,
    before_pose_cutoff,
    draw_overlay,
    extract_detections,
    overlay_labels,
    suppress_duplicate_detections,
    validate_model_classes,
)
from bibazu_reorientation.models import Detection, InferenceFrame


def frame(class_id: int = 1, count: int = 1, timestamp: float | None = None) -> InferenceFrame:
    detection = Detection(
        class_id, f"Pose {class_id + 1}", 0.9, ((5, 5), (20, 5), (20, 20), (5, 20)), "detect"
    )
    return InferenceFrame(
        np.zeros((30, 30, 3), np.uint8), (detection,) * count, 1.0, timestamp or time.time()
    )


def test_three_fresh_frames_form_consensus() -> None:
    consensus = PoseConsensus()
    started = time.time()
    assert consensus.add(frame(timestamp=started), {0: 1, 1: 2}) is None
    assert consensus.add(frame(timestamp=started + 0.01), {0: 1, 1: 2}) is None
    assert consensus.add(frame(timestamp=started + 0.02), {0: 1, 1: 2}).pose_id == 2


@pytest.mark.parametrize("bad", [frame(count=0), frame(count=2), frame(timestamp=1.0)])
def test_invalid_frame_resets_consensus(bad: InferenceFrame) -> None:
    consensus = PoseConsensus()
    consensus.add(frame(), {0: 1, 1: 2})
    assert consensus.add(bad, {0: 1, 1: 2}) is None
    assert consensus.observations == ()


def test_detect_and_obb_extraction() -> None:
    boxes = SimpleNamespace(
        xyxy=np.array([[1, 2, 10, 12]]), cls=np.array([0]), conf=np.array([0.8])
    )
    detect = SimpleNamespace(boxes=boxes, obb=None, names={0: "Pose 1"})
    assert extract_detections(detect)[0].kind == "detect"
    obb = SimpleNamespace(
        xyxyxyxy=np.array([[[1, 2], [10, 2], [10, 12], [1, 12]]]),
        cls=np.array([1]),
        conf=np.array([0.9]),
    )
    oriented = SimpleNamespace(boxes=None, obb=obb, names={1: "Pose 2"})
    assert extract_detections(oriented)[0].kind == "obb"


def test_overlapping_pose_classes_are_suppressed_by_confidence() -> None:
    lower = Detection(
        0,
        "Pose 1",
        0.72,
        ((5, 5), (25, 5), (25, 25), (5, 25)),
        "detect",
    )
    higher = Detection(
        1,
        "Pose 2",
        0.91,
        ((6, 6), (24, 6), (24, 24), (6, 24)),
        "detect",
    )

    assert suppress_duplicate_detections((lower, higher)) == (higher,)


def test_overlapping_detections_of_the_same_class_are_suppressed() -> None:
    lower = Detection(
        1,
        "Pose 2",
        0.80,
        ((5, 5), (25, 5), (25, 25), (5, 25)),
        "detect",
    )
    higher = Detection(
        1,
        "Pose 2",
        0.94,
        ((5.5, 5.5), (24.5, 5.5), (24.5, 24.5), (5.5, 24.5)),
        "detect",
    )

    assert suppress_duplicate_detections((lower, higher)) == (higher,)


def test_separate_or_slightly_overlapping_parts_are_preserved() -> None:
    first = Detection(
        0,
        "Pose 1",
        0.90,
        ((5, 5), (20, 5), (20, 20), (5, 20)),
        "detect",
    )
    second = Detection(
        1,
        "Pose 2",
        0.85,
        ((17, 5), (32, 5), (32, 20), (17, 20)),
        "detect",
    )

    assert suppress_duplicate_detections((first, second)) == (first, second)


def test_shadow_inflated_heavily_overlapping_parts_with_separate_centers_are_preserved() -> None:
    first = Detection(
        0,
        "Pose 1",
        0.90,
        ((0, 0), (100, 0), (100, 30), (0, 30)),
        "obb",
    )
    second = Detection(
        0,
        "Pose 1",
        0.88,
        ((14, 0), (114, 0), (114, 30), (14, 30)),
        "obb",
    )

    assert suppress_duplicate_detections((first, second)) == (first, second)


def test_default_nms_iou_preserves_more_overlapping_candidates(tmp_path) -> None:
    config = InferenceConfig(tmp_path / "best.pt").validated(require_model=False)

    assert config.nms_iou == pytest.approx(0.90)


def test_model_class_contract() -> None:
    assert validate_model_classes({0: "Pose 1", 1: "Pose 2"})[1] == "Pose 2"
    with pytest.raises(ValueError):
        validate_model_classes({0: "front", 1: "back"})


def test_roadmap_model_contract_allows_extra_classes_and_explicit_mapping() -> None:
    names = {0: "unknown", 3: "Ql1i pose 9", 7: "Ql1i pose 24", 10: "other"}
    assert validate_model_classes(names, (3, 7)) == names
    with pytest.raises(ValueError, match="missing configured classes"):
        validate_model_classes(names, (3, 8))


def test_unmapped_detection_alongside_mapped_object_resets_consensus() -> None:
    consensus = PoseConsensus()
    started = time.time()
    consensus.add(frame(class_id=0, timestamp=started), {0: 1, 1: 2})
    mapped = frame(class_id=0, timestamp=started + 0.01).detections[0]
    unknown = frame(class_id=7, timestamp=started + 0.01).detections[0]
    mixed = InferenceFrame(
        np.zeros((30, 30, 3), np.uint8),
        (mapped, unknown),
        1.0,
        started + 0.01,
    )
    assert consensus.add(mixed, {0: 1, 1: 2}) is None
    assert consensus.observations == ()


def test_overlay_promotes_mapped_roadmap_pose_over_yolo_class(tmp_path) -> None:
    detection = frame(class_id=1).detections[0]
    assert overlay_labels(detection, {1: 10}) == ("Pose 10", "90%")
    assert overlay_labels(detection, {}) == ("Pose 2 90%", "")

    config = InferenceConfig(
        tmp_path / "best.pt",
        class_to_pose=((1, 10),),
    ).validated(require_model=False)
    assert dict(config.class_to_pose) == {1: 10}


def test_mapped_confidence_is_drawn_beside_pose_not_on_box(monkeypatch) -> None:
    detection = Detection(
        1,
        "Pose 2",
        0.9,
        ((100, 5), (120, 5), (120, 20), (100, 20)),
        "detect",
    )
    calls: list[tuple[str, tuple[int, int]]] = []

    def record_text(_image, text, origin, *_args):
        calls.append((text, origin))
        return _image

    monkeypatch.setattr("bibazu_reorientation.inference.cv2.putText", record_text)
    draw_overlay(np.zeros((100, 400, 3), np.uint8), (detection,), {1: 10})

    assert [text for text, _origin in calls] == ["Pose 10", "90%"]
    assert calls[1][1][0] > calls[0][1][0]
    assert calls[1][1][1] == calls[0][1][1]


def test_pose_prediction_is_ignored_at_ten_percent_left_cutoff(monkeypatch) -> None:
    crossing = Detection(
        1,
        "Pose 2",
        0.99,
        ((35, 5), (60, 5), (60, 25), (35, 25)),
        "detect",
    )
    calls: list[str] = []

    def record_text(_image, text, *_args):
        calls.append(text)
        return _image

    monkeypatch.setattr("bibazu_reorientation.inference.cv2.putText", record_text)
    assert not before_pose_cutoff(crossing, 400)

    draw_overlay(np.zeros((100, 400, 3), np.uint8), (crossing,), {1: 10})

    assert calls == []
