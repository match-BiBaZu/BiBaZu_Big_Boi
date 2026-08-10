from __future__ import annotations

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


@dataclass(slots=True, frozen=True)
class InferenceConfig:
    model_path: Path
    confidence: float = 0.5
    image_size: int = 640
    max_fps: float = 5.0
    device: str = "auto"

    def validated(self, *, require_model: bool = True) -> InferenceConfig:
        model_path = Path(self.model_path).expanduser().resolve()
        if require_model and (not model_path.is_file() or model_path.suffix.lower() != ".pt"):
            raise ValueError(f"YOLO model not found: {model_path}")
        if not 0.01 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0.01 and 1.00")
        if self.image_size < 32 or self.image_size % 32:
            raise ValueError("Image size must be a multiple of 32")
        if not 0.2 <= self.max_fps <= 60:
            raise ValueError("Inference rate must be between 0.2 and 60 FPS")
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


def validate_model_classes(names: Any) -> dict[int, str]:
    if isinstance(names, dict):
        mapping = {int(key): str(value) for key, value in names.items()}
    else:
        mapping = {index: str(value) for index, value in enumerate(names)}
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


def draw_overlay(image: np.ndarray, detections: tuple[Detection, ...]) -> np.ndarray:
    annotated = np.ascontiguousarray(image).copy()
    palette = ((34, 197, 94), (249, 115, 22))
    for detection in detections:
        color = palette[detection.class_id % len(palette)]
        points = np.rint(np.asarray(detection.corners)).astype(np.int32)
        cv2.polylines(annotated, [points], True, color, 2, cv2.LINE_AA)
        label = f"{detection.class_name} {detection.confidence:.0%}"
        x = max(0, int(points[:, 0].min()))
        y = max(24, int(points[:, 1].min()))
        cv2.putText(
            annotated,
            label,
            (x, y - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
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
        accepted = [
            detection
            for detection in frame.detections
            if detection.class_id in class_to_pose and fully_visible(detection, width, height)
        ]
        if now - frame.timestamp > self.max_age_seconds or len(accepted) != 1:
            self.reset()
            return None
        if self._observations and frame.timestamp <= self._observations[-1].timestamp:
            self.reset()
            return None
        detection = accepted[0]
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
            self._latest = (image, timestamp)
            self._condition.notify()

    def stop(self, wait_ms: int = 10_000) -> bool:
        with self._condition:
            self._stopping = True
            self._latest = None
            self._condition.notify_all()
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
            names = validate_model_classes(getattr(model, "names", {}))
            device = self._config.device
            if device == "auto":
                import torch

                device = "0" if torch.cuda.is_available() else "cpu"
            # The warm-up result is deliberately discarded.
            model.predict(
                source=np.zeros((self._config.image_size, self._config.image_size, 3), np.uint8),
                imgsz=self._config.image_size,
                conf=self._config.confidence,
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
                    device=device,
                    verbose=False,
                )
                result = results[0] if results else None
                detections = () if result is None else extract_detections(result)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self.frame_ready.emit(
                    InferenceFrame(
                        draw_overlay(image, detections), detections, elapsed_ms, timestamp
                    )
                )
                next_allowed = started + 1.0 / self._config.max_fps
        except Exception as exc:
            self.status_changed.emit("Error")
            self.error.emit(f"YOLO inference failed: {exc}")
