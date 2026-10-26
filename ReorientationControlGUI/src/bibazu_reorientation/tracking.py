from __future__ import annotations

from dataclasses import dataclass, field, replace

from bibazu_reorientation.inference import (
    before_pose_cutoff,
    fully_visible,
    suppress_duplicate_detections,
)
from bibazu_reorientation.models import (
    Detection,
    InferenceFrame,
    PartDecision,
    PoseObservation,
    TrackedPart,
)


def _bbox(detection: Detection) -> tuple[float, float, float, float]:
    xs = [point[0] for point in detection.corners]
    ys = [point[1] for point in detection.corners]
    return min(xs), min(ys), max(xs), max(ys)


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _iou(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0.0:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / max(1e-9, first_area + second_area - intersection)


@dataclass(slots=True)
class _Track:
    track_id: int
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]
    confidence: float
    hits: int = 1
    missed_frames: int = 0
    handed_off: bool = False
    confirmed_pose_id: int | None = None
    streak_pose_id: int | None = None
    observations: list[PoseObservation] = field(default_factory=list)

    @property
    def pose_streak(self) -> int:
        return len(self.observations)


@dataclass(slots=True, frozen=True)
class TrackerUpdate:
    tracks: tuple[TrackedPart, ...]
    handoffs: tuple[PartDecision, ...]
    fault: str = ""


def snapshot_queue(
    frame: InferenceFrame,
    class_to_pose: dict[int, int],
    *,
    pose_cutoff_ratio: float = 0.10,
) -> TrackerUpdate:
    """Freeze one camera frame into the physical left-to-right PLC queue order."""
    height, width = frame.image.shape[:2]
    detections = suppress_duplicate_detections(frame.detections)
    ordered = sorted(detections, key=lambda detection: _center(_bbox(detection))[0])
    tracks: list[TrackedPart] = []
    decisions: list[PartDecision] = []
    for track_id, detection in enumerate(ordered, start=1):
        box = _bbox(detection)
        center = _center(box)
        mapped_pose_id = class_to_pose.get(detection.class_id)
        visible = fully_visible(detection, width, height)
        before_cutoff = before_pose_cutoff(detection, width, cutoff_ratio=pose_cutoff_ratio)
        pose_id = mapped_pose_id if visible and before_cutoff else None
        if mapped_pose_id is None:
            reason = "snapshot_unmapped_class"
        elif not visible:
            reason = "snapshot_partially_visible"
        elif not before_cutoff:
            reason = "snapshot_past_pose_cutoff"
        else:
            reason = "snapshot_confirmed"
        observations = (
            (
                PoseObservation(
                    pose_id,
                    detection.class_id,
                    detection.confidence,
                    frame.timestamp,
                ),
            )
            if pose_id is not None
            else ()
        )
        tracks.append(
            TrackedPart(
                track_id,
                box,
                center,
                detection.confidence,
                1 if pose_id is not None else 0,
                pose_id,
                0,
                True,
                leftmost=track_id == 1,
            )
        )
        decisions.append(
            PartDecision(
                track_id,
                pose_id,
                detection.confidence,
                observations,
                box,
                frame.timestamp,
                reason,
            )
        )
    return TrackerUpdate(tuple(tracks), tuple(decisions))


class MultiPartTracker:
    """Order-preserving tracker for workpieces moving from right to left."""

    def __init__(
        self,
        *,
        required_observations: int = 5,
        max_missed_frames: int = 2,
        handoff_line_ratio: float = 0.30,
        strict_order: bool = False,
        pose_cutoff_ratio: float = 0.10,
        pose_at_handoff: bool = False,
    ) -> None:
        if required_observations < 5:
            raise ValueError("At least five observations are required")
        if max_missed_frames < 0:
            raise ValueError("max_missed_frames must be non-negative")
        if not 0.05 <= handoff_line_ratio <= 0.80:
            raise ValueError("handoff_line_ratio must be between 0.05 and 0.80")
        if not 0.05 <= pose_cutoff_ratio <= 0.80:
            raise ValueError("pose_cutoff_ratio must be between 0.05 and 0.80")
        self.strict_order = strict_order
        self.pose_cutoff_ratio = pose_cutoff_ratio
        self.pose_at_handoff = pose_at_handoff
        self.required_observations = required_observations
        self.max_missed_frames = max_missed_frames
        self.handoff_line_ratio = handoff_line_ratio
        self._tracks: dict[int, _Track] = {}
        self._next_track_id = 1

    def reset(self) -> None:
        self._tracks.clear()
        self._next_track_id = 1

    @property
    def active_count(self) -> int:
        return len(self._tracks)

    def update(
        self,
        frame: InferenceFrame,
        class_to_pose: dict[int, int],
    ) -> TrackerUpdate:
        height, width = frame.image.shape[:2]
        line_x = width * self.handoff_line_ratio
        detections = list(suppress_duplicate_detections(frame.detections))
        boxes = [_bbox(detection) for detection in detections]
        centers = [_center(box) for box in boxes]
        previous_order = tuple(
            track.track_id for track in sorted(self._tracks.values(), key=lambda row: row.center[0])
        )
        matches, unmatched_tracks, unmatched_detections = self._match(centers, boxes, width)
        fault = ""

        for track_id, detection_index in matches:
            track = self._tracks[track_id]
            detection = detections[detection_index]
            new_center = centers[detection_index]
            track.bbox = boxes[detection_index]
            track.center = new_center
            track.confidence = detection.confidence
            track.hits += 1
            track.missed_frames = 0
            self._observe(track, detection, frame, class_to_pose, width, height)

        matched_ids = {track_id for track_id, _detection_index in matches}
        previous_matched_order = tuple(
            track_id for track_id in previous_order if track_id in matched_ids
        )
        current_matched_order = tuple(
            track.track_id
            for track in sorted(
                (self._tracks[track_id] for track_id in matched_ids),
                key=lambda row: row.center[0],
            )
        )
        # Queue-once production mode does not require tracking order validation at
        # every frame. We only need stable track identity until the workpiece has
        # passed the handoff line or been lost before it can be queued.
        if self.strict_order and previous_matched_order != current_matched_order:
            fault = "Tracked parts changed physical queue order"

        for track_id in unmatched_tracks:
            track = self._tracks[track_id]
            track.missed_frames += 1
            if track.confirmed_pose_id is None:
                track.streak_pose_id = None
                track.observations.clear()
            if track.missed_frames > self.max_missed_frames:
                # Live handoff selection needs a detection at the line, not an
                # uninterrupted upstream track. Drop expired tracks quietly;
                # a later detection at the line can still select its profile.
                if (
                    not self.pose_at_handoff
                    and not track.handed_off
                    and (self.strict_order or track.hits >= 2)
                ):
                    fault = fault or f"Track {track_id} was lost before the handoff line"

        for detection_index in unmatched_detections:
            detection = detections[detection_index]
            track = _Track(
                self._next_track_id,
                boxes[detection_index],
                centers[detection_index],
                detection.confidence,
            )
            self._next_track_id += 1
            self._tracks[track.track_id] = track
            self._observe(track, detection, frame, class_to_pose, width, height)
            if (track.bbox[0] if self.strict_order else track.center[0]) <= line_x and not (
                self.pose_at_handoff and track.bbox[2] > line_x
            ):
                fault = fault or f"New track {track.track_id} appeared beyond the handoff line"

        handoffs: list[PartDecision] = []
        crossing = sorted(
            (
                track
                for track in self._tracks.values()
                if not track.handed_off
                and track.missed_frames == 0
                and (track.bbox[0] if self.strict_order else track.center[0]) <= line_x
            ),
            key=lambda item: item.center[0],
        )
        for track in crossing:
            track.handed_off = True
            if self.pose_at_handoff:
                reason = (
                    "handoff_detection"
                    if track.confirmed_pose_id is not None
                    else "handoff_unmapped_class"
                )
            else:
                reason = (
                    "confirmed" if track.confirmed_pose_id is not None else "consensus_incomplete"
                )
            handoffs.append(
                PartDecision(
                    track.track_id,
                    track.confirmed_pose_id,
                    track.confidence,
                    tuple(track.observations),
                    track.bbox,
                    frame.timestamp,
                    reason,
                )
            )

        for track_id in tuple(self._tracks):
            if self._tracks[track_id].missed_frames > self.max_missed_frames:
                del self._tracks[track_id]

        ordered = sorted(self._tracks.values(), key=lambda item: item.center[0])
        snapshots = tuple(
            replace(self._snapshot(track), leftmost=index == 0)
            for index, track in enumerate(ordered)
        )
        return TrackerUpdate(snapshots, tuple(handoffs), fault)

    def _match(
        self,
        centers: list[tuple[float, float]],
        boxes: list[tuple[float, float, float, float]],
        width: int,
    ) -> tuple[list[tuple[int, int]], set[int], set[int]]:
        candidates: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            for detection_index, center in enumerate(centers):
                dx = abs(center[0] - track.center[0]) / max(1.0, width)
                dy = abs(center[1] - track.center[1]) / max(1.0, width)
                overlap = _iou(track.bbox, boxes[detection_index])
                if dx <= 0.20 and (overlap > 0.0 or dy <= 0.12):
                    candidates.append((dx + dy - overlap * 0.25, track_id, detection_index))
        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        matches: list[tuple[int, int]] = []
        for _score, track_id, detection_index in sorted(candidates):
            if track_id in used_tracks or detection_index in used_detections:
                continue
            used_tracks.add(track_id)
            used_detections.add(detection_index)
            matches.append((track_id, detection_index))
        return (
            matches,
            set(self._tracks) - used_tracks,
            set(range(len(centers))) - used_detections,
        )

    def _observe(
        self,
        track: _Track,
        detection: Detection,
        frame: InferenceFrame,
        class_to_pose: dict[int, int],
        width: int,
        height: int,
    ) -> None:
        if self.pose_at_handoff:
            if track.handed_off:
                return
            # Only the current YOLO output selects the profile. Track identity
            # prevents duplicate commits; earlier classes never lock the pose.
            track.confirmed_pose_id = class_to_pose.get(detection.class_id)
            track.observations.clear()
            if track.confirmed_pose_id is not None:
                track.observations.append(
                    PoseObservation(
                        track.confirmed_pose_id,
                        detection.class_id,
                        detection.confidence,
                        frame.timestamp,
                    )
                )
            return
        if track.confirmed_pose_id is not None:
            return
        pose_id = class_to_pose.get(detection.class_id)
        if (
            pose_id is None
            or not fully_visible(detection, width, height)
            or (
                self.strict_order
                and not before_pose_cutoff(detection, width, cutoff_ratio=self.pose_cutoff_ratio)
            )
        ):
            track.streak_pose_id = None
            track.observations.clear()
            return
        if track.streak_pose_id != pose_id:
            track.streak_pose_id = pose_id
            track.observations.clear()
        track.observations.append(
            PoseObservation(pose_id, detection.class_id, detection.confidence, frame.timestamp)
        )
        if len(track.observations) >= self.required_observations:
            track.observations[:] = track.observations[-self.required_observations :]
            track.confirmed_pose_id = pose_id

    @staticmethod
    def _snapshot(track: _Track) -> TrackedPart:
        return TrackedPart(
            track.track_id,
            track.bbox,
            track.center,
            track.confidence,
            track.pose_streak,
            track.confirmed_pose_id,
            track.missed_frames,
            track.handed_off,
        )
