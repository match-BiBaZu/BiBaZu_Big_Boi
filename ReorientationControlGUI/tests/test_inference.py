from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from bibazu_reorientation.inference import PoseConsensus, extract_detections, validate_model_classes
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


def test_model_class_contract() -> None:
    assert validate_model_classes({0: "Pose 1", 1: "Pose 2"})[1] == "Pose 2"
    with pytest.raises(ValueError):
        validate_model_classes({0: "front", 1: "back"})
