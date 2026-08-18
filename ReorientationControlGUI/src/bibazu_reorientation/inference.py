from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from bibazu_reorientation.models import (
    ConsensusDecision,
    Detection,
    InferenceFrame,
    PoseObservation,
)

POSE_CUTOFF_RATIO = 0.10
DEFAULT_NMS_IOU = 0.90
DEFAULT_DUPLICATE_CENTER_DISTANCE = 0.25


@dataclass(slots=True, frozen=True)
class InferenceConfig:
    model_path: Path
    confidence: float = 0.5
    nms_iou: float = DEFAULT_NMS_IOU
    image_size: int = 640
    max_fps: float = 5.0
    device: str = "auto"
    expected_class_ids: tuple[int, ...] | None = None
    class_to_pose: tuple[tuple[int, int], ...] = ()

    def validated(self, *, require_model: bool = True) -> InferenceConfig:
        model_path = Path(self.model_path).expanduser().resolve()
        if require_model and (not model_path.is_file() or model_path.suffix.lower() != ".pt"):
            raise ValueError(f"YOLO model not found: {model_path}")
        if not 0.01 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0.01 and 1.00")
        if not 0.01 <= self.nms_iou <= 1.0:
            raise ValueError("NMS IoU must be between 0.01 and 1.00")
        if self.image_size < 32 or self.image_size % 32:
            raise ValueError("Image size must be a multiple of 32")
        if not 0.2 <= self.max_fps <= 60:
            raise ValueError("Inference rate must be between 0.2 and 60 FPS")
        class_ids = [class_id for class_id, _pose_id in self.class_to_pose]
        pose_ids = [pose_id for _class_id, pose_id in self.class_to_pose]
        if len(class_ids) != len(set(class_ids)):
            raise ValueError("Each YOLO class may be mapped to only one pose")
        if len(pose_ids) != len(set(pose_ids)):
            raise ValueError("Each pose may be mapped to only one YOLO class")
        return replace(self, model_path=model_path)


def _as_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def _name(names: Any, class_id: int) -> str:
    if isinstance(names, dict):
        return str(names.get(class_id, f"Class {class_id}"))
    try:
        return str(names[class_id])
    except (IndexError, KeyError, TypeError):
        return f"Class {class_id}"


def _overlap_ratios(first: Detection, second: Detection) -> tuple[float, float]:
    """Return polygon IoU and intersection over the smaller detection."""
    first_polygon = np.asarray(first.corners, dtype=np.float32).reshape(-1, 2)
    second_polygon = np.asarray(second.corners, dtype=np.float32).reshape(-1, 2)
    first_area = abs(float(cv2.contourArea(first_polygon)))
    second_area = abs(float(cv2.contourArea(second_polygon)))
    if first_area <= 0.0 or second_area <= 0.0:
        return 0.0, 0.0
    try:
        intersection = float(
            cv2.intersectConvexConvex(first_polygon, second_polygon)[0]
        )
    except cv2.error:
        return 0.0, 0.0
    intersection = max(0.0, intersection)
    union = first_area + second_area - intersection
    return (
        intersection / max(1e-9, union),
        intersection / max(1e-9, min(first_area, second_area)),
    )


def _normalized_center_distance(first: Detection, second: Detection) -> float:
    """Return centroid separation relative to the smaller polygon's area scale."""
    first_polygon = np.asarray(first.corners, dtype=np.float32).reshape(-1, 2)
    second_polygon = np.asarray(second.corners, dtype=np.float32).reshape(-1, 2)
    first_area = abs(float(cv2.contourArea(first_polygon)))
    second_area = abs(float(cv2.contourArea(second_polygon)))
    if first_area <= 0.0 or second_area <= 0.0:
        return float("inf")
    first_center = first_polygon.mean(axis=0)
    second_center = second_polygon.mean(axis=0)
    distance = float(np.linalg.norm(first_center - second_center))
    return distance / max(1e-9, math.sqrt(min(first_area, second_area)))


def suppress_duplicate_detections(
    detections: tuple[Detection, ...],
    *,
    iou_threshold: float = 0.75,
    containment_threshold: float = 0.90,
    center_distance_threshold: float = DEFAULT_DUPLICATE_CENTER_DISTANCE,
) -> tuple[Detection, ...]:
    """Suppress near-coincident duplicates without rejecting neighbouring parts.

    Shadow-inflated OBBs from separate long workpieces may overlap heavily. Treat
    detections as duplicates only when their polygon centres also nearly coincide.
    """
    if len(detections) < 2:
        return detections
    selected_indices: list[int] = []
    by_confidence = sorted(
        range(len(detections)),
        key=lambda index: detections[index].confidence,
        reverse=True,
    )
    for candidate_index in by_confidence:
        candidate = detections[candidate_index]
        duplicate = False
        for selected_index in selected_indices:
            iou, containment = _overlap_ratios(candidate, detections[selected_index])
            centers_coincide = (
                _normalized_center_distance(candidate, detections[selected_index])
                <= center_distance_threshold
            )
            if centers_coincide and (
                iou >= iou_threshold or containment >= containment_threshold
            ):
                duplicate = True
                break
        if not duplicate:
            selected_indices.append(candidate_index)
    return tuple(detections[index] for index in sorted(selected_indices))


def validate_model_classes(
    names: Any, expected_class_ids: tuple[int, ...] | None = None
) -> dict[int, str]:
    if isinstance(names, dict):
        mapping = {int(key): str(value) for key, value in names.items()}
    else:
        mapping = {index: str(value) for index, value in enumerate(names)}
    if expected_class_ids is not None:
        missing = set(expected_class_ids) - set(mapping)
        if missing:
            raise ValueError(f"YOLO model is missing configured classes: {sorted(missing)}")
        return mapping
    if set(mapping) != {0, 1}:
        raise ValueError("V1 requires a YOLO model with exactly classes 0 and 1")
    for class_id, expected in ((0, "pose1"), (1, "pose2")):
        normalized = re.sub(r"[^a-z0-9]", "", mapping[class_id].casefold())
        if normalized != expected:
            raise ValueError(
                f"YOLO class {class_id} is named {mapping[class_id]!r}; "
                f"expected 'Pose {class_id + 1}'"
            )
    return mapping


def extract_detections(result: Any) -> tuple[Detection, ...]:
    names = getattr(result, "names", {})
    obb = getattr(result, "obb", None)
    if obb is not None and getattr(obb, "xyxyxyxy", None) is not None:
        corners = _as_numpy(obb.xyxyxyxy)
        classes = _as_numpy(obb.cls).reshape(-1)
        confidences = _as_numpy(obb.conf).reshape(-1)
        detections = []
        for points, class_value, confidence in zip(corners, classes, confidences, strict=False):
            points = np.asarray(points, dtype=np.float32).reshape(-1, 2)
            if points.shape == (4, 2) and np.isfinite(points).all():
                class_id = int(class_value)
                detections.append(
                    Detection(
                        class_id,
                        _name(names, class_id),
                        float(confidence),
                        tuple((float(x), float(y)) for x, y in points),
                        "obb",
                    )
                )
        return tuple(detections)

    boxes = getattr(result, "boxes", None)
    if boxes is None or getattr(boxes, "xyxy", None) is None:
        return ()
    rectangles = _as_numpy(boxes.xyxy).reshape(-1, 4)
    classes = _as_numpy(boxes.cls).reshape(-1)
    confidences = _as_numpy(boxes.conf).reshape(-1)
    detections = []
    for rectangle, class_value, confidence in zip(rectangles, classes, confidences, strict=False):
        x1, y1, x2, y2 = (float(value) for value in rectangle)
        if not np.isfinite((x1, y1, x2, y2)).all() or x2 <= x1 or y2 <= y1:
            continue
        class_id = int(class_value)
        detections.append(
            Detection(
                class_id,
                _name(names, class_id),
                float(confidence),
                ((x1, y1), (x2, y1), (x2, y2), (x1, y2)),
                "detect",
            )
        )
    return tuple(detections)


def fully_visible(detection: Detection, width: int, height: int, margin: float = 1.0) -> bool:
    return all(
        margin < x < width - 1 - margin and margin < y < height - 1 - margin
        for x, y in detection.corners
    )


def before_pose_cutoff(
    detection: Detection,
    width: int,
    *,
    cutoff_ratio: float = POSE_CUTOFF_RATIO,
) -> bool:
    """Return whether the whole detection remains right of the pose cutoff."""
    cutoff_x = width * cutoff_ratio
    return min(x for x, _y in detection.corners) > cutoff_x


def overlay_labels(
    detection: Detection, class_to_pose: dict[int, int] | None = None
) -> tuple[str, str]:
    """Return a prominent mapped pose and confidence without the model class."""
    pose_id = None if class_to_pose is None else class_to_pose.get(detection.class_id)
    if pose_id is None:
        return f"{detection.class_name} {detection.confidence:.0%}", ""
    return f"Pose {pose_id}", f"{detection.confidence:.0%}"


def draw_overlay(
    image: np.ndarray,
    detections: tuple[Detection, ...],
    class_to_pose: dict[int, int] | None = None,
) -> np.ndarray:
    annotated = np.ascontiguousarray(image).copy()
    cutoff_x = round(annotated.shape[1] * POSE_CUTOFF_RATIO)
    cv2.line(
        annotated,
        (cutoff_x, 0),
        (cutoff_x, annotated.shape[0] - 1),
        (250, 204, 21),
        2,
        cv2.LINE_AA,
    )
    palette = ((34, 197, 94), (249, 115, 22))
    for detection in detections:
        if not before_pose_cutoff(detection, annotated.shape[1]):
            continue
        color = palette[detection.class_id % len(palette)]
        points = np.rint(np.asarray(detection.corners)).astype(np.int32)
        cv2.polylines(annotated, [points], True, color, 2, cv2.LINE_AA)
        primary, secondary = overlay_labels(detection, class_to_pose)
        x = max(0, int(points[:, 0].min()))
        y = max(42, int(points[:, 1].min()))
        primary_origin = (x, y - 18 if secondary else y - 5)
        cv2.putText(
            annotated,
            primary,
            primary_origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            1.15 if secondary else 0.7,
            color,
            3 if secondary else 2,
            cv2.LINE_AA,
        )
        if secondary:
            primary_width = cv2.getTextSize(
                primary, cv2.FONT_HERSHEY_SIMPLEX, 1.15, 3
            )[0][0]
            secondary_width = cv2.getTextSize(
                secondary, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1
            )[0][0]
            secondary_x = x + primary_width + 14
            if secondary_x + secondary_width >= annotated.shape[1]:
                secondary_x = max(0, x - secondary_width - 14)
            cv2.putText(
                annotated,
                secondary,
                (secondary_x, primary_origin[1]),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                1,
                cv2.LINE_AA,
            )
    return annotated


class PoseConsensus:
    def __init__(self, required: int = 3, max_age_seconds: float = 1.0) -> None:
        if required != 3:
            raise ValueError("V1 uses exactly three observations")
        self.required = required
        self.max_age_seconds = max_age_seconds
        self._observations: list[PoseObservation] = []

    @property
    def observations(self) -> tuple[PoseObservation, ...]:
        return tuple(self._observations)

    def reset(self) -> None:
        self._observations.clear()

    def add(
        self,
        frame: InferenceFrame,
        class_to_pose: dict[int, int],
        *,
        now: float | None = None,
    ) -> ConsensusDecision | None:
        now = time.time() if now is None else now
        height, width = frame.image.shape[:2]
        if now - frame.timestamp > self.max_age_seconds or len(frame.detections) != 1:
            self.reset()
            return None
        detection = frame.detections[0]
        if detection.class_id not in class_to_pose or not fully_visible(
            detection, width, height
        ):
            self.reset()
            return None
        if self._observations and frame.timestamp <= self._observations[-1].timestamp:
            self.reset()
            return None
        observation = PoseObservation(
            pose_id=class_to_pose[detection.class_id],
            class_id=detection.class_id,
            confidence=detection.confidence,
            timestamp=frame.timestamp,
        )
        if self._observations and self._observations[-1].pose_id != observation.pose_id:
            self.reset()
        self._observations.append(observation)
        if len(self._observations) < self.required:
            return None
        selected = tuple(self._observations[-3:])
        self.reset()
        return ConsensusDecision(selected[-1].pose_id, selected)  # type: ignore[arg-type]


class InferenceWorker(QThread):
    frame_ready = pyqtSignal(object)
    model_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        config: InferenceConfig,
        parent: Any = None,
        *,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config.validated()
        self._model_factory = model_factory
        self._condition = threading.Condition()
        self._latest: tuple[np.ndarray, float] | None = None
        self._stopping = False

    def submit(self, image: np.ndarray, timestamp: float) -> None:
        if image.ndim != 3 or image.shape[2] != 3:
            return
        with self._condition:
            if self._stopping:
                return
            self._latest = (image, timestamp)
            self._condition.notify()

    def request_stop(self) -> None:
        """Request termination without blocking the caller's Qt thread."""
        with self._condition:
            self._stopping = True
            self._latest = None
            self._condition.notify_all()

    def stop(self, wait_ms: int = 10_000) -> bool:
        self.request_stop()
        return self.wait(wait_ms) if QThread.currentThread() is not self else True

    def _model(self) -> Any:
        if self._model_factory is not None:
            return self._model_factory(str(self._config.model_path))
        from ultralytics import YOLO

        return YOLO(str(self._config.model_path))

    def run(self) -> None:
        try:
            self.status_changed.emit("Loading model …")
            model = self._model()
            names = validate_model_classes(
                getattr(model, "names", {}), self._config.expected_class_ids
            )
            device = self._config.device
            if device == "auto":
                import torch

                device = "0" if torch.cuda.is_available() else "cpu"
            # The warm-up result is deliberately discarded.
            model.predict(
                source=np.zeros((self._config.image_size, self._config.image_size, 3), np.uint8),
                imgsz=self._config.image_size,
                conf=self._config.confidence,
                iou=self._config.nms_iou,
                device=device,
                verbose=False,
            )
            self.model_ready.emit(
                {"names": names, "device": device, "task": getattr(model, "task", "")}
            )
            self.status_changed.emit(f"Ready · {self._config.model_path.name}")
            next_allowed = 0.0
            while True:
                with self._condition:
                    while not self._stopping:
                        delay = max(0.0, next_allowed - time.monotonic())
                        if self._latest is not None and delay <= 0:
                            break
                        self._condition.wait(timeout=delay if delay else 0.5)
                    if self._stopping:
                        return
                    image, timestamp = self._latest
                    self._latest = None
                started = time.perf_counter()
                bgr = np.ascontiguousarray(image[:, :, ::-1])
                results = model.predict(
                    source=bgr,
                    imgsz=self._config.image_size,
                    conf=self._config.confidence,
                    iou=self._config.nms_iou,
                    device=device,
                    verbose=False,
                )
                result = results[0] if results else None
                detections = (
                    ()
                    if result is None
                    else suppress_duplicate_detections(extract_detections(result))
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self.frame_ready.emit(
                    InferenceFrame(
                        draw_overlay(
                            image,
                            detections,
                            dict(self._config.class_to_pose),
                        ),
                        detections,
                        elapsed_ms,
                        timestamp,
                    )
                )
                next_allowed = started + 1.0 / self._config.max_fps
        except Exception as exc:
            self.status_changed.emit("Error")
            self.error.emit(f"YOLO inference failed: {exc}")
