from __future__ import annotations

import csv
import json
import math
import queue
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QDir,
    QObject,
    QPointF,
    QRectF,
    QSettings,
    Qt,
    QThread,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFileSystemModel, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

USB_CTI_PATH = Path(r"C:\Program Files\Baumer Camera Explorer\bgapi2_usb.cti")
DEFAULT_CAMERA_SERIAL = "700005072151"
DEFAULT_POST_TRIGGER_MS = 200
PREVIEW_INTERVAL_SECONDS = 1.0 / 30.0
RECORDING_QUEUE_CAPACITY = 512
JPEG_QUALITY = 95
UINT32_MASK = 0xFFFFFFFF
LINE0_RISING_EVENT_ID = 0x8007
HARDWARE_TRIGGER_UNCERTAINTY_MS = 0.5
PRESSURE_MIN_MBAR = 0
PRESSURE_MAX_MBAR = 6000
LIGHT_BARRIER_COUNT = 8
FAST_RESPONSE_ENABLE_CONTEXT = "pressure_delay_fast_response_enable"
FAST_RESPONSE_RESTORE_CONTEXT = "pressure_delay_fast_response_restore"
PRESSURE_APPLY_CONTEXT_PREFIX = "pressure_delay_pressure_array_"
PULSE_DURATION_APPLY_CONTEXT_PREFIX = "pressure_delay_pulse_duration_array_"

FRAME_COLUMNS = (
    "index",
    "filename",
    "frame_id",
    "camera_timestamp_ns",
    "host_monotonic_ns",
    "wall_time_utc",
    "relative_to_light_barrier_ms",
)
RESULT_COLUMNS = (
    "session_id",
    "recorded_at_utc",
    "light_barrier",
    "movement_frame_index",
    "movement_filename",
    "delay_ms",
    "evaluation_method",
    "timing_uncertainty_ms",
    "frame_count",
    "camera_model",
    "camera_serial",
    "exposure_us",
    "array_index",
    "pressure_mbar",
    "pulse_duration_ms",
    "fastest_response_mode",
    "session_directory",
)


def pressure_array_for_barrier(light_barrier: int) -> int:
    sensor = int(light_barrier)
    if not 1 <= sensor <= LIGHT_BARRIER_COUNT:
        raise ValueError(f"Light barrier must be 1..{LIGHT_BARRIER_COUNT}")
    return (sensor + 1) // 2


def fastest_response_write_values(
    light_barrier: int, pressure_mbar: int
) -> dict[str, bool | int | float]:
    sensor = int(light_barrier)
    array_index = pressure_array_for_barrier(sensor)
    pressure = max(PRESSURE_MIN_MBAR, min(PRESSURE_MAX_MBAR, int(pressure_mbar)))
    return {
        f"MAIN.GuiPressureMbar{array_index}": pressure,
        f"MAIN.GuiDelayMs{array_index}": 0,
        f"MAIN.GuiOffsetMm{array_index}": 0.0,
        f"MAIN.GuiForceResponseDelayMs{array_index}": 0.0,
        f"MAIN.GuiForceSingleNozzleResponseDelayMs{array_index}": 0.0,
        f"MAIN.GuiLightBarrierDebounceEnabled{sensor}": False,
    }


def uint32_elapsed(newer: int, older: int) -> int:
    """Return an unsigned PLC millisecond difference, including wraparound."""
    return (int(newer) - int(older)) & UINT32_MASK


def estimate_plc_event_host_ns(
    sampled_monotonic_ns: int,
    plc_event_clock_ms: int,
    event_time_ms: int,
) -> int:
    elapsed_ms = uint32_elapsed(plc_event_clock_ms, event_time_ms)
    return int(sampled_monotonic_ns) - elapsed_ms * 1_000_000


def estimate_camera_event_timestamp_ns(
    anchors: list[tuple[int, int]], event_host_ns: int, sample_count: int = 64
) -> int | None:
    """Map a host-monotonic event time onto the camera's nanosecond clock."""
    if not anchors:
        return None
    nearest = sorted(anchors, key=lambda pair: abs(pair[1] - event_host_ns))[
        :sample_count
    ]
    host_minus_camera = [host_ns - camera_ns for camera_ns, host_ns in nearest]
    return int(event_host_ns - statistics.median(host_minus_camera))


def estimate_timing_uncertainty_ms(
    anchors: list[tuple[int, int]], ads_roundtrip_ns: int
) -> float:
    if len(anchors) >= 2:
        camera_times = sorted(camera for camera, _host in anchors)
        intervals = [
            newer - older for older, newer in pairwise(camera_times) if newer > older
        ]
        frame_interval_ns = statistics.median(intervals) if intervals else 0.0
        offsets = [host - camera for camera, host in anchors]
        median_offset = statistics.median(offsets)
        offset_mad_ns = statistics.median(
            abs(value - median_offset) for value in offsets
        )
    else:
        frame_interval_ns = 0.0
        offset_mad_ns = 0.0
    return (
        1.0
        + max(0, int(ads_roundtrip_ns)) / 2_000_000.0
        + frame_interval_ns / 2_000_000.0
        + offset_mad_ns / 1_000_000.0
    )


def analyze_recording_movement(
    directory: Path,
    frames: list[dict[str, Any]],
    *,
    minimum_displacement_px: float = 0.2,
    persistence_frames: int = 3,
    maximum_delay_ms: float = 100.0,
) -> dict[str, Any]:
    """Find the first persistent component motion after the hardware trigger."""
    import cv2
    import numpy as np

    directory = Path(directory)
    if persistence_frames < 2:
        raise ValueError("Movement persistence must contain at least two frames")
    relative_times: list[float | None] = []
    for frame in frames:
        relative = frame.get("relative_to_light_barrier_ms", "")
        relative_times.append(
            None if relative in {"", None} else float(relative)
        )
    trigger_index = next(
        (
            index
            for index, relative in enumerate(relative_times)
            if relative is not None and relative >= 0.0
        ),
        None,
    )
    if trigger_index is None:
        raise ValueError("The recording has no frames after the light-barrier trigger")
    pretrigger = [
        index
        for index in range(trigger_index)
        if relative_times[index] is not None and relative_times[index] < 0.0
    ]
    if len(pretrigger) < 4:
        raise ValueError(
            "At least four pre-trigger frames are required for automatic analysis"
        )
    reference_index = pretrigger[max(0, len(pretrigger) - 12)]

    def load_gray(index: int):
        filename = frames[index].get("filename")
        image = cv2.imread(str(directory / str(filename)), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise OSError(f"Could not read {filename}")
        return image

    reference = load_gray(reference_index)
    height, width = reference.shape[:2]
    feature_mask = np.zeros_like(reference)
    feature_mask[
        int(height * 0.30) : int(height * 0.72),
        int(width * 0.25) : int(width * 0.80),
    ] = 255
    reference_points = cv2.goodFeaturesToTrack(
        reference,
        mask=feature_mask,
        maxCorners=500,
        qualityLevel=0.002,
        minDistance=3,
        blockSize=5,
    )
    if reference_points is None or len(reference_points) < 20:
        raise ValueError(
            "Too few component features were found; improve lighting or focus"
        )

    def coherent_displacement(index: int) -> tuple[float, float, float, int]:
        if index == reference_index:
            return 0.0, 0.0, 0.0, len(reference_points)
        target = load_gray(index)
        tracked, status, errors = cv2.calcOpticalFlowPyrLK(
            reference,
            target,
            reference_points,
            None,
            winSize=(41, 41),
            maxLevel=4,
            criteria=(
                cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                50,
                0.001,
            ),
        )
        if tracked is None or status is None or errors is None:
            raise ValueError("Optical-flow tracking failed")
        valid = status.ravel() == 1
        source = reference_points[valid].reshape(-1, 2)
        destination = tracked[valid].reshape(-1, 2)
        error_values = errors[valid].ravel()
        displacement = destination - source
        plausible = (
            (np.abs(displacement[:, 0]) < 40.0)
            & (np.abs(displacement[:, 1]) < 40.0)
            & (error_values < 50.0)
        )
        displacement = displacement[plausible]
        if len(displacement) < max(20, len(reference_points) // 10):
            raise ValueError(
                "Too few component features remained trackable during movement"
            )
        median_dx = float(np.median(displacement[:, 0]))
        median_dy = float(np.median(displacement[:, 1]))
        return (
            math.hypot(median_dx, median_dy),
            median_dx,
            median_dy,
            len(displacement),
        )

    baseline_samples = [
        coherent_displacement(index)[0]
        for index in pretrigger
        if index >= reference_index
    ]
    baseline_median = float(statistics.median(baseline_samples))
    baseline_mad = float(
        statistics.median(
            abs(value - baseline_median) for value in baseline_samples
        )
    )
    threshold = max(
        float(minimum_displacement_px),
        baseline_median + 8.0 * max(baseline_mad, 0.01),
        max(baseline_samples) * 2.5,
    )
    if threshold > 1.0:
        raise ValueError(
            "The pre-trigger image is not stable enough for automatic analysis"
        )

    persistent: list[dict[str, Any]] = []
    for index in range(trigger_index, len(frames)):
        relative = relative_times[index]
        if relative is None:
            continue
        if relative > float(maximum_delay_ms):
            break
        magnitude, dx, dy, track_count = coherent_displacement(index)
        sample = {
            "frame_index": index,
            "filename": str(frames[index]["filename"]),
            "relative_to_light_barrier_ms": relative,
            "coherent_displacement_px": magnitude,
            "median_dx_px": dx,
            "median_dy_px": dy,
            "track_count": track_count,
        }
        if magnitude >= threshold:
            persistent.append(sample)
            if len(persistent) >= persistence_frames:
                movement = persistent[0]
                previous_index = max(0, movement["frame_index"] - 1)
                movement.update(
                    {
                        "previous_frame_index": previous_index,
                        "previous_relative_ms": relative_times[previous_index],
                        "threshold_px": threshold,
                        "baseline_median_px": baseline_median,
                        "baseline_mad_px": baseline_mad,
                        "persistence_frames": persistence_frames,
                        "reference_frame_index": reference_index,
                    }
                )
                return movement
        else:
            persistent.clear()
    raise ValueError(
        f"No persistent movement above {threshold:.3f} px was found within "
        f"{maximum_delay_ms:.1f} ms"
    )


def build_pressure_delay_comparison(
    directories: list[Path],
) -> dict[str, Any]:
    """Load marked sessions and calculate grouped statistics and a linear fit."""
    trials: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()
    for raw_directory in directories:
        directory = Path(raw_directory).resolve()
        if directory in seen:
            continue
        seen.add(directory)
        try:
            document = json.loads(
                (directory / "session.json").read_text(encoding="utf-8")
            )
            pressure = document.get("plc_measurement_setup", {}).get(
                "pressure_mbar"
            )
            delay = document.get("evaluation", {}).get("delay_ms")
            light_barrier = document.get("light_barrier")
            if pressure is None:
                raise ValueError("pressure is missing")
            if delay is None:
                raise ValueError("movement frame is not marked")
            if light_barrier is None:
                raise ValueError("light barrier is missing")
            pressure_mbar = float(pressure)
            delay_ms = float(delay)
            if not math.isfinite(pressure_mbar) or not math.isfinite(delay_ms):
                raise ValueError("pressure or delay is not finite")
            trials.append(
                {
                    "directory": str(directory),
                    "session_id": str(document.get("session_id", directory.name)),
                    "light_barrier": int(light_barrier),
                    "pressure_mbar": pressure_mbar,
                    "pressure_bar": pressure_mbar / 1000.0,
                    "delay_ms": delay_ms,
                }
            )
        except Exception as exc:
            skipped.append(
                {"directory": str(directory), "reason": str(exc) or type(exc).__name__}
            )
    if not trials:
        raise ValueError(
            "None of the selected folders contains a marked pressure-delay result"
        )

    barriers = sorted({trial["light_barrier"] for trial in trials})
    if len(barriers) != 1:
        joined = ", ".join(f"LB {barrier}" for barrier in barriers)
        raise ValueError(
            f"Select recordings from one light barrier only; found {joined}"
        )

    grouped: dict[float, list[float]] = {}
    for trial in trials:
        grouped.setdefault(trial["pressure_mbar"], []).append(trial["delay_ms"])
    groups = []
    for pressure_mbar, delays in sorted(grouped.items()):
        groups.append(
            {
                "pressure_mbar": pressure_mbar,
                "pressure_bar": pressure_mbar / 1000.0,
                "count": len(delays),
                "mean_delay_ms": statistics.fmean(delays),
                "standard_deviation_ms": (
                    statistics.stdev(delays) if len(delays) > 3 else None
                ),
            }
        )

    regression = None
    x_values = [trial["pressure_bar"] for trial in trials]
    y_values = [trial["delay_ms"] for trial in trials]
    x_mean = statistics.fmean(x_values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    if len(trials) >= 2 and denominator > 0.0:
        y_mean = statistics.fmean(y_values)
        slope = sum(
            (x_value - x_mean) * (y_value - y_mean)
            for x_value, y_value in zip(x_values, y_values)
        ) / denominator
        intercept = y_mean - slope * x_mean
        predictions = [slope * value + intercept for value in x_values]
        residual_sum = sum(
            (actual - predicted) ** 2
            for actual, predicted in zip(y_values, predictions)
        )
        total_sum = sum((value - y_mean) ** 2 for value in y_values)
        regression = {
            "slope_ms_per_bar": slope,
            "intercept_ms": intercept,
            "r_squared": 1.0 - residual_sum / total_sum if total_sum > 0.0 else 1.0,
        }
    return {
        "light_barrier": barriers[0],
        "trials": trials,
        "groups": groups,
        "regression": regression,
        "skipped": skipped,
    }


class MovementAnalysisWorker(QThread):
    result_ready = pyqtSignal(object)
    analysis_failed = pyqtSignal(str)

    def __init__(
        self,
        directory: Path,
        frames: list[dict[str, Any]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.directory = Path(directory)
        self.frames = [dict(frame) for frame in frames]

    def run(self) -> None:
        try:
            result = analyze_recording_movement(self.directory, self.frames)
        except Exception as exc:
            self.analysis_failed.emit(str(exc) or type(exc).__name__)
            return
        self.result_ready.emit(result)


class RecordingFolderSelectionDialog(QDialog):
    """Directory tree with extended selection for pressure-delay sessions."""

    def __init__(self, start_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select pressure-delay recordings")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        instruction = QLabel(
            "Select several recording folders with Ctrl/Shift. You may also "
            "select a parent folder containing recordings."
        )
        instruction.setWordWrap(True)
        layout.addWidget(instruction)

        root = Path(start_directory).expanduser().resolve()
        while not root.exists() and root != root.parent:
            root = root.parent
        if not root.exists():
            root = Path.cwd()
        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot)
        root_index = self.model.setRootPath(str(root))
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIndex(root_index)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setHeaderHidden(True)
        for column in range(1, self.model.columnCount()):
            self.tree.hideColumn(column)
        layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_recording_directories(self) -> list[Path]:
        selected: set[Path] = set()
        for index in self.tree.selectionModel().selectedRows(0):
            directory = Path(self.model.filePath(index))
            if (directory / "session.json").is_file():
                selected.add(directory)
                continue
            for manifest in directory.rglob("session.json"):
                selected.add(manifest.parent)
        return sorted(selected, key=lambda path: str(path).casefold())


class PressureDelayPlotWidget(QWidget):
    def __init__(self, comparison: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.comparison = comparison
        self.setMinimumSize(760, 480)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        trials = self.comparison["trials"]
        groups = self.comparison["groups"]
        regression = self.comparison.get("regression")
        if not trials:
            return

        left, top, right, bottom = 74.0, 42.0, 28.0, 68.0
        plot = QRectF(
            left,
            top,
            max(1.0, self.width() - left - right),
            max(1.0, self.height() - top - bottom),
        )
        x_values = [float(item["pressure_bar"]) for item in trials]
        y_values = [float(item["delay_ms"]) for item in trials]
        for group in groups:
            deviation = group.get("standard_deviation_ms")
            if deviation is not None:
                y_values.extend(
                    [
                        float(group["mean_delay_ms"]) - float(deviation),
                        float(group["mean_delay_ms"]) + float(deviation),
                    ]
                )
        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = min(y_values), max(y_values)
        x_padding = max(0.1, (x_max - x_min) * 0.08)
        y_padding = max(0.2, (y_max - y_min) * 0.12)
        if x_min == x_max:
            x_padding = max(0.5, abs(x_min) * 0.1)
        if y_min == y_max:
            y_padding = max(1.0, abs(y_min) * 0.1)
        x_min -= x_padding
        x_max += x_padding
        y_min -= y_padding
        y_max += y_padding

        def map_x(value: float) -> float:
            return plot.left() + (value - x_min) / (x_max - x_min) * plot.width()

        def map_y(value: float) -> float:
            return plot.bottom() - (value - y_min) / (y_max - y_min) * plot.height()

        painter.setPen(QPen(QColor("#d1d5db"), 1.0))
        for tick in range(6):
            fraction = tick / 5.0
            x_value = x_min + fraction * (x_max - x_min)
            y_value = y_min + fraction * (y_max - y_min)
            x = map_x(x_value)
            y = map_y(y_value)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#374151"))
            painter.drawText(
                QRectF(x - 35, plot.bottom() + 8, 70, 22),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                f"{x_value:.2f}",
            )
            painter.drawText(
                QRectF(2, y - 11, left - 10, 22),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{y_value:.2f}",
            )
            painter.setPen(QPen(QColor("#d1d5db"), 1.0))

        painter.setPen(QPen(QColor("#111827"), 1.5))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())
        painter.drawText(
            QRectF(plot.left(), self.height() - 42, plot.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            "Pressure [bar]",
        )
        painter.save()
        painter.translate(20, plot.center().y())
        painter.rotate(-90)
        painter.drawText(
            QRectF(-plot.height() / 2, -12, plot.height(), 24),
            Qt.AlignmentFlag.AlignCenter,
            "LB-to-movement delay [ms]",
        )
        painter.restore()

        painter.setPen(QPen(QColor("#6b7280"), 1.0))
        painter.setBrush(QColor(107, 114, 128, 130))
        for trial in trials:
            point = QPointF(
                map_x(float(trial["pressure_bar"])),
                map_y(float(trial["delay_ms"])),
            )
            painter.drawEllipse(point, 4.0, 4.0)

        painter.setPen(QPen(QColor("#dc2626"), 2.0))
        painter.setBrush(QColor("#f97316"))
        for group in groups:
            x = map_x(float(group["pressure_bar"]))
            mean = float(group["mean_delay_ms"])
            deviation = group.get("standard_deviation_ms")
            if deviation is not None:
                upper = map_y(mean + float(deviation))
                lower = map_y(mean - float(deviation))
                painter.drawLine(QPointF(x, upper), QPointF(x, lower))
                painter.drawLine(QPointF(x - 7, upper), QPointF(x + 7, upper))
                painter.drawLine(QPointF(x - 7, lower), QPointF(x + 7, lower))
            painter.drawEllipse(QPointF(x, map_y(mean)), 5.5, 5.5)

        model_text = "Linear model unavailable (two pressure levels required)"
        if regression is not None:
            slope = float(regression["slope_ms_per_bar"])
            intercept = float(regression["intercept_ms"])
            painter.setPen(QPen(QColor("#2563eb"), 2.0))
            painter.drawLine(
                QPointF(map_x(x_min), map_y(slope * x_min + intercept)),
                QPointF(map_x(x_max), map_y(slope * x_max + intercept)),
            )
            model_text = (
                f"Linear model: delay = {slope:+.3f} ms/bar × pressure "
                f"{intercept:+.3f} ms · R² = {float(regression['r_squared']):.3f}"
            )
        painter.setPen(QColor("#111827"))
        painter.drawText(
            QRectF(plot.left(), 8, plot.width(), 25),
            Qt.AlignmentFlag.AlignCenter,
            f"Pressure-delay comparison · LB {self.comparison['light_barrier']}",
        )
        painter.setPen(QColor("#2563eb"))
        painter.drawText(
            QRectF(plot.left() + 5, plot.top() + 5, plot.width() - 10, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            model_text,
        )


class PressureDelayPlotDialog(QDialog):
    def __init__(self, comparison: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pressure-delay comparison")
        self.resize(920, 650)
        layout = QVBoxLayout(self)
        groups = comparison["groups"]
        skipped = comparison["skipped"]
        summary = QLabel(
            f"{len(comparison['trials'])} valid trials at {len(groups)} pressure "
            f"levels · error bars show ±1 standard deviation for more than "
            f"3 trials per pressure"
            + (f" · {len(skipped)} folders skipped" if skipped else "")
        )
        summary.setWordWrap(True)
        if skipped:
            summary.setToolTip(
                "\n".join(
                    f"{item['directory']}: {item['reason']}" for item in skipped
                )
            )
        layout.addWidget(summary)
        layout.addWidget(PressureDelayPlotWidget(comparison), 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


@dataclass(slots=True)
class FramePacket:
    image: Any
    pixel_format: str
    width: int
    height: int
    frame_id: int
    camera_timestamp_ns: int
    host_monotonic_ns: int
    wall_time_ns: int


@dataclass(slots=True)
class SavedFrame:
    index: int
    filename: str
    frame_id: int
    camera_timestamp_ns: int
    host_monotonic_ns: int
    wall_time_ns: int
    relative_to_light_barrier_ms: float | None = None


def _safe_buffer_value(buffer: Any, name: str, default: int) -> int:
    try:
        return int(getattr(buffer, name))
    except Exception:
        return int(default)


def _device_field(device: Any, name: str, default: str = "") -> str:
    value = getattr(device, name, default)
    if callable(value):
        value = value()
    return str(value) if value is not None else default


def _node_value(node_map: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(node_map, name).value
    except Exception:
        return default


def _find_node(node_map: Any, *names: str) -> Any:
    for name in names:
        try:
            return getattr(node_map, name)
        except Exception:
            continue
    return None


def _node_writable(node: Any) -> bool:
    if node is None:
        return False
    try:
        from genicam.genapi import is_writable

        return bool(is_writable(node))
    except Exception:
        return str(getattr(node, "access_mode", "")).upper() in {"RW", "WO", "4"}


def decode_baumer_usb_line_event(
    payload: bytes, expected_event_id: int = LINE0_RISING_EVENT_ID
) -> int | None:
    """Return the camera timestamp from a Baumer USB3 line-event payload.

    The producer exposes a compact 12-byte remote-device event: two reserved
    bytes, a little-endian 16-bit event ID, and a little-endian 64-bit
    timestamp. Harvester 1.4.3 cannot pass this compact packet through its
    EventAdapterU3V, so the worker decodes the producer payload directly.
    """
    data = bytes(payload)
    if len(data) < 12:
        return None
    event_id = int.from_bytes(data[2:4], "little", signed=False)
    if event_id != int(expected_event_id):
        return None
    return int.from_bytes(data[4:12], "little", signed=False)


def frame_to_rgb(image: Any, pixel_format: str) -> Any:
    import cv2
    import numpy as np

    array = np.asarray(image)
    fmt = str(pixel_format).lower()
    if fmt.startswith("mono"):
        if array.dtype != np.uint8:
            maximum = float(max(1, int(array.max())))
            array = np.clip(
                array.astype(np.float32) * (255.0 / maximum), 0, 255
            ).astype(np.uint8)
        return np.ascontiguousarray(cv2.cvtColor(array, cv2.COLOR_GRAY2RGB))
    bayer_codes = {
        "bayerrg": cv2.COLOR_BayerRG2RGB,
        "bayerbg": cv2.COLOR_BayerBG2RGB,
        "bayergr": cv2.COLOR_BayerGR2RGB,
        "bayergb": cv2.COLOR_BayerGB2RGB,
    }
    for prefix, code in bayer_codes.items():
        if fmt.startswith(prefix):
            return np.ascontiguousarray(cv2.cvtColor(array, code))
    if fmt.startswith("rgb8"):
        return np.ascontiguousarray(array)
    if fmt.startswith("bgr8"):
        return np.ascontiguousarray(cv2.cvtColor(array, cv2.COLOR_BGR2RGB))
    raise ValueError(f"Unsupported camera pixel format: {pixel_format}")


class CameraWorker(QObject):
    connection_changed = pyqtSignal(bool, str)
    camera_info = pyqtSignal(object)
    preview_ready = pyqtSignal(object)
    recording_frame = pyqtSignal(object)
    hardware_trigger = pyqtSignal(object)
    exposure_applied = pyqtSignal(float)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, cti_path: Path, serial: str) -> None:
        super().__init__()
        self.cti_path = Path(cti_path)
        self.serial = str(serial)
        self._stop = threading.Event()
        self._recording = threading.Event()
        self._exposure_commands: queue.Queue[float] = queue.Queue()

    def stop(self) -> None:
        self._stop.set()

    def set_recording(self, enabled: bool) -> None:
        if enabled:
            self._recording.set()
        else:
            self._recording.clear()

    def request_exposure(self, exposure_us: float) -> None:
        self._exposure_commands.put(float(exposure_us))

    def _select_device(self, devices: list[Any]) -> Any:
        for device in devices:
            if _device_field(device, "serial_number") == self.serial:
                return device
        if len(devices) == 1:
            return devices[0]
        found = ", ".join(
            f"{_device_field(device, 'model', '?')} "
            f"({_device_field(device, 'serial_number', '?')})"
            for device in devices
        )
        raise RuntimeError(
            f"USB camera {self.serial} was not found. "
            f"Detected devices: {found or 'none'}"
        )

    @pyqtSlot()
    def run(self) -> None:
        harvester = None
        acquirer = None
        exposure_node = None
        exposure_auto_node = None
        frame_rate_node = None
        frame_rate_enable_node = None
        original_exposure = None
        original_exposure_auto = None
        original_frame_rate = None
        original_frame_rate_enable = None
        event_selector_node = None
        event_notification_node = None
        original_event_selector = None
        original_line0_notification = None
        line_event_monitor = None
        line_event_id = LINE0_RISING_EVENT_ID
        try:
            import numpy as np
            from harvesters.core import Harvester

            harvester = Harvester()
            harvester.add_file(
                str(self.cti_path), check_existence=True, check_validity=True
            )
            harvester.update()
            devices = list(harvester.device_info_list)
            if not devices:
                raise RuntimeError(
                    "No Baumer USB camera was found by the GenTL producer"
                )
            selected = self._select_device(devices)
            acquirer = harvester.create(selected)
            node_map = acquirer.remote_device.node_map
            exposure_node = _find_node(node_map, "ExposureTime", "ExposureTimeAbs")
            exposure_auto_node = _find_node(node_map, "ExposureAuto")
            frame_rate_node = _find_node(node_map, "AcquisitionFrameRate")
            frame_rate_enable_node = _find_node(node_map, "AcquisitionFrameRateEnable")
            event_selector_node = _find_node(node_map, "EventSelector")
            event_notification_node = _find_node(node_map, "EventNotification")
            if exposure_node is not None:
                original_exposure = float(exposure_node.value)
            if exposure_auto_node is not None:
                original_exposure_auto = str(exposure_auto_node.value)
            if frame_rate_node is not None:
                original_frame_rate = float(frame_rate_node.value)
            if frame_rate_enable_node is not None:
                original_frame_rate_enable = bool(frame_rate_enable_node.value)
            try:
                if _node_writable(frame_rate_enable_node):
                    frame_rate_enable_node.value = False
                elif _node_writable(frame_rate_node):
                    frame_rate_node.value = float(frame_rate_node.max)
            except Exception:
                pass

            hardware_trigger_available = False
            try:
                if not _node_writable(event_selector_node) or not _node_writable(
                    event_notification_node
                ):
                    raise RuntimeError("camera line-event nodes are not writable")
                original_event_selector = str(event_selector_node.value)
                event_selector_node.value = "Line0RisingEdge"
                original_line0_notification = str(event_notification_node.value)
                event_notification_node.value = "On"
                line_event_id = int(
                    _node_value(node_map, "EventLine0RisingEdge", LINE0_RISING_EVENT_ID)
                )
                line_event_monitor = acquirer._module_event_monitor_dict[
                    acquirer.remote_device
                ]
                hardware_trigger_available = True
            except Exception:
                line_event_monitor = None

            info = {
                "model": _device_field(selected, "model", "Baumer"),
                "serial": _device_field(selected, "serial_number", self.serial),
                "width": int(_node_value(node_map, "Width", 0)),
                "height": int(_node_value(node_map, "Height", 0)),
                "pixel_format": str(_node_value(node_map, "PixelFormat", "Unknown")),
                "exposure_us": float(_node_value(node_map, "ExposureTime", 0.0)),
                "exposure_min_us": float(getattr(exposure_node, "min", 20.0)),
                "exposure_max_us": float(getattr(exposure_node, "max", 1_000_000.0)),
                "stream_fps": 0.0,
                "trigger_input": "Line0",
                "trigger_activation": "RisingEdge",
                "hardware_trigger_available": hardware_trigger_available,
            }
            self.camera_info.emit(dict(info))
            acquirer.start()
            self.connection_changed.emit(True, "")
            next_preview = 0.0
            fps_started = time.monotonic()
            fps_frames = 0
            fallback_frame_id = 0
            while not self._stop.is_set():
                try:
                    while True:
                        requested = self._exposure_commands.get_nowait()
                        if exposure_node is None:
                            raise RuntimeError("Camera exposure is not writable")
                        if exposure_auto_node is not None and str(
                            exposure_auto_node.value
                        ).lower() not in {"off", "none"}:
                            if not _node_writable(exposure_auto_node):
                                raise RuntimeError("ExposureAuto cannot be disabled")
                            exposure_auto_node.value = "Off"
                        minimum = float(getattr(exposure_node, "min", requested))
                        maximum = float(getattr(exposure_node, "max", requested))
                        exposure_node.value = max(minimum, min(maximum, requested))
                        applied = float(exposure_node.value)
                        info["exposure_us"] = applied
                        self.exposure_applied.emit(applied)
                        self.camera_info.emit(dict(info))
                except queue.Empty:
                    pass

                with acquirer.fetch(timeout=1.0) as buffer:
                    component = buffer.payload.components[0]
                    host_ns = time.perf_counter_ns()
                    wall_ns = time.time_ns()
                    camera_ns = _safe_buffer_value(buffer, "timestamp", host_ns)
                    frame_id = _safe_buffer_value(buffer, "frame_id", fallback_frame_id)
                    fallback_frame_id = frame_id + 1
                    width = int(component.width)
                    height = int(component.height)
                    pixel_format = str(
                        getattr(component, "data_format", info["pixel_format"])
                    )
                    now = time.monotonic()
                    preview_due = now >= next_preview
                    recording = self._recording.is_set()
                    if preview_due or recording:
                        image = np.array(
                            component.data.reshape(height, width), copy=True, order="C"
                        )
                        packet = FramePacket(
                            image=image,
                            pixel_format=pixel_format,
                            width=width,
                            height=height,
                            frame_id=frame_id,
                            camera_timestamp_ns=camera_ns,
                            host_monotonic_ns=host_ns,
                            wall_time_ns=wall_ns,
                        )
                        if recording:
                            self.recording_frame.emit(packet)
                        if preview_due:
                            next_preview = now + PREVIEW_INTERVAL_SECONDS
                            self.preview_ready.emit(packet)
                if line_event_monitor is not None:
                    from genicam.gentl import TimeoutException

                    while True:
                        try:
                            line_event_monitor.update_event_data(0)
                        except TimeoutException:
                            break
                        raw_event = bytes(line_event_monitor.optional_data)
                        event_camera_ns = decode_baumer_usb_line_event(
                            raw_event, line_event_id
                        )
                        if event_camera_ns is not None:
                            self.hardware_trigger.emit(
                                {
                                    "source": "camera_line0_rising_edge",
                                    "event_id": line_event_id,
                                    "camera_timestamp_ns": event_camera_ns,
                                    "received_host_monotonic_ns": (
                                        time.perf_counter_ns()
                                    ),
                                }
                            )
                fps_frames += 1
                elapsed = time.monotonic() - fps_started
                if elapsed >= 1.0:
                    info["stream_fps"] = fps_frames / elapsed
                    info["exposure_us"] = float(
                        _node_value(node_map, "ExposureTime", info["exposure_us"])
                    )
                    self.camera_info.emit(dict(info))
                    fps_started = time.monotonic()
                    fps_frames = 0
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if "busy" in message.lower() or "resource" in message.lower():
                message += ". Close Baumer Camera Explorer and reconnect."
            self.error.emit(message)
        finally:
            if acquirer is not None:
                try:
                    acquirer.stop()
                except Exception:
                    pass
                try:
                    if exposure_node is not None and original_exposure is not None:
                        if (
                            exposure_auto_node is not None
                            and str(exposure_auto_node.value).lower()
                            not in {"off", "none"}
                            and _node_writable(exposure_auto_node)
                        ):
                            exposure_auto_node.value = "Off"
                        exposure_node.value = original_exposure
                        if (
                            exposure_auto_node is not None
                            and original_exposure_auto is not None
                            and _node_writable(exposure_auto_node)
                        ):
                            exposure_auto_node.value = original_exposure_auto
                except Exception:
                    pass
                try:
                    if original_frame_rate is not None and _node_writable(
                        frame_rate_node
                    ):
                        frame_rate_node.value = original_frame_rate
                    if original_frame_rate_enable is not None and _node_writable(
                        frame_rate_enable_node
                    ):
                        frame_rate_enable_node.value = original_frame_rate_enable
                except Exception:
                    pass
                try:
                    if event_selector_node is not None:
                        event_selector_node.value = "Line0RisingEdge"
                        if (
                            event_notification_node is not None
                            and original_line0_notification is not None
                        ):
                            event_notification_node.value = original_line0_notification
                        if original_event_selector is not None:
                            event_selector_node.value = original_event_selector
                except Exception:
                    pass
                try:
                    acquirer.destroy()
                except Exception:
                    pass
            if harvester is not None:
                try:
                    harvester.reset()
                except Exception:
                    pass
            self.connection_changed.emit(False, "")
            self.finished.emit()


class UsbHighSpeedCamera(QObject):
    connection_changed = pyqtSignal(bool, str)
    camera_info = pyqtSignal(object)
    preview_ready = pyqtSignal(object)
    recording_frame = pyqtSignal(object)
    hardware_trigger = pyqtSignal(object)
    exposure_applied = pyqtSignal(float)
    error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.thread: QThread | None = None
        self.worker: CameraWorker | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def connect_camera(self, serial: str = DEFAULT_CAMERA_SERIAL) -> None:
        if self.running:
            return
        self.thread = QThread(self)
        self.worker = CameraWorker(USB_CTI_PATH, serial)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.connection_changed.connect(self.connection_changed)
        self.worker.camera_info.connect(self.camera_info)
        self.worker.preview_ready.connect(self.preview_ready)
        self.worker.recording_frame.connect(self.recording_frame)
        self.worker.hardware_trigger.connect(self.hardware_trigger)
        self.worker.exposure_applied.connect(self.exposure_applied)
        self.worker.error.connect(self.error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    @pyqtSlot()
    def _thread_finished(self) -> None:
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None

    def disconnect_camera(self) -> None:
        if self.worker is not None:
            self.worker.set_recording(False)
            self.worker.stop()
        if self.thread is not None:
            self.thread.quit()

    def set_recording(self, enabled: bool) -> None:
        if self.worker is not None:
            self.worker.set_recording(enabled)

    def set_exposure(self, exposure_us: float) -> None:
        if self.worker is not None:
            self.worker.request_exposure(exposure_us)

    def shutdown(self, timeout_ms: int = 2500) -> None:
        self.disconnect_camera()
        if self.thread is not None and self.thread.isRunning():
            self.thread.wait(timeout_ms)


class RecordingSession(QObject):
    auto_stop_requested = pyqtSignal()
    failed = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(
        self,
        output_root: Path,
        light_barrier: int,
        post_trigger_ms: int,
        camera_info: dict[str, Any],
        parent: QObject | None = None,
        measurement_settings: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        started = datetime.now(timezone.utc)
        self.measurement_settings = dict(measurement_settings or {})
        base_id = started.astimezone().strftime(f"%Y%m%d_%H%M%S_LB{int(light_barrier)}")
        pressure_mbar = self.measurement_settings.get("pressure_mbar")
        if pressure_mbar is not None:
            base_id += f"_{int(round(float(pressure_mbar)))}mbar"
        self.output_root = Path(output_root)
        self.session_id, self.directory = self._unique_directory(base_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.light_barrier = int(light_barrier)
        self.post_trigger_ms = int(post_trigger_ms)
        self.camera_info = dict(camera_info)
        self.started_at_utc = started.isoformat()
        self.started_monotonic_ns = time.perf_counter_ns()
        self.frames: list[SavedFrame] = []
        self.anchors: list[tuple[int, int]] = []
        self.event_count: int | None = None
        self.event_plc_time_ms: int | None = None
        self.event_host_ns: int | None = None
        self.event_camera_ns: int | None = None
        self.event_received_host_ns: int | None = None
        self.trigger_source: str | None = None
        self.trigger_event_id: int | None = None
        self.ads_roundtrip_ns = 0
        self.timing_uncertainty_ms: float | None = None
        self.trigger_host_deadline_ns: int | None = None
        self.trigger_camera_deadline_ns: int | None = None
        self.stop_reason = "recording"
        self.recording_complete = True
        self.error_message = ""
        self.frame_id_gaps: list[tuple[int, int]] = []
        self._last_frame_id: int | None = None
        self._accepting = True
        self._stop_sent = False
        self._queue: queue.Queue[tuple[int, FramePacket] | None] = queue.Queue(
            RECORDING_QUEUE_CAPACITY
        )
        self._writer = threading.Thread(
            target=self._writer_main,
            name=f"pressure-delay-writer-{self.session_id}",
            daemon=True,
        )
        self._write_initial_manifest()
        self._writer.start()

    def _unique_directory(self, base_id: str) -> tuple[str, Path]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        candidate = self.output_root / base_id
        if not candidate.exists():
            return base_id, candidate
        for suffix in range(1, 1000):
            session_id = f"{base_id}_{suffix:02d}"
            candidate = self.output_root / session_id
            if not candidate.exists():
                return session_id, candidate
        raise RuntimeError("Could not allocate a unique recording directory")

    def _write_initial_manifest(self) -> None:
        self._write_json(
            {
                "version": 1,
                "session_id": self.session_id,
                "state": "recording",
                "recorded_at_utc": self.started_at_utc,
                "light_barrier": self.light_barrier,
                "post_trigger_ms": self.post_trigger_ms,
                "camera": self.camera_info,
                "plc_measurement_setup": self.measurement_settings,
            }
        )

    def add_frame(self, packet: FramePacket) -> None:
        if not self._accepting:
            return
        index = len(self.anchors)
        self.anchors.append((packet.camera_timestamp_ns, packet.host_monotonic_ns))
        if (
            self._last_frame_id is not None
            and packet.frame_id != self._last_frame_id + 1
        ):
            self.frame_id_gaps.append((self._last_frame_id, packet.frame_id))
            self.recording_complete = False
        self._last_frame_id = packet.frame_id
        try:
            self._queue.put_nowait((index, packet))
        except queue.Full:
            self.recording_complete = False
            self.error_message = "Recording writer queue overflow; recording stopped"
            self.failed.emit(self.error_message)
            return
        camera_deadline_reached = (
            self.trigger_camera_deadline_ns is not None
            and packet.camera_timestamp_ns >= self.trigger_camera_deadline_ns
        )
        host_deadline_reached = (
            self.trigger_camera_deadline_ns is None
            and self.trigger_host_deadline_ns is not None
            and packet.host_monotonic_ns >= self.trigger_host_deadline_ns
        )
        if (camera_deadline_reached or host_deadline_reached) and not self._stop_sent:
            self._stop_sent = True
            self.auto_stop_requested.emit()

    def set_hardware_trigger(
        self,
        event_camera_ns: int,
        received_host_ns: int,
        event_id: int = LINE0_RISING_EVENT_ID,
    ) -> bool:
        if self.event_camera_ns is not None or self.event_host_ns is not None:
            return False
        if int(received_host_ns) < self.started_monotonic_ns:
            return False
        self.trigger_source = "camera_line0_rising_edge"
        self.trigger_event_id = int(event_id)
        self.event_camera_ns = int(event_camera_ns)
        self.event_received_host_ns = int(received_host_ns)
        self.event_host_ns = int(received_host_ns)
        self.trigger_camera_deadline_ns = (
            self.event_camera_ns + self.post_trigger_ms * 1_000_000
        )
        self.timing_uncertainty_ms = HARDWARE_TRIGGER_UNCERTAINTY_MS
        return True

    def set_trigger(
        self,
        event_count: int,
        event_plc_time_ms: int,
        event_host_ns: int,
        ads_roundtrip_ns: int,
    ) -> None:
        if self.event_host_ns is not None:
            return
        self.event_count = int(event_count)
        self.trigger_source = "plc_ads"
        self.event_plc_time_ms = int(event_plc_time_ms)
        self.event_host_ns = int(event_host_ns)
        self.ads_roundtrip_ns = int(ads_roundtrip_ns)
        self.trigger_host_deadline_ns = (
            self.event_host_ns + self.post_trigger_ms * 1_000_000
        )

    def stop(self, reason: str, error_message: str = "") -> None:
        if not self._accepting:
            return
        self._accepting = False
        self.stop_reason = str(reason)
        if error_message:
            self.error_message = error_message
            self.recording_complete = False
        self._queue.put(None)

    def wait(self, timeout_seconds: float = 5.0) -> bool:
        self._writer.join(timeout_seconds)
        return not self._writer.is_alive()

    def _writer_main(self) -> None:
        try:
            import cv2

            while True:
                item = self._queue.get()
                if item is None:
                    break
                index, packet = item
                filename = f"frame_{index:06d}.jpg"
                rgb = frame_to_rgb(packet.image, packet.pixel_format)
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                ok = cv2.imwrite(
                    str(self.directory / filename),
                    bgr,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
                )
                if not ok:
                    raise OSError(f"Could not write {filename}")
                self.frames.append(
                    SavedFrame(
                        index=index,
                        filename=filename,
                        frame_id=packet.frame_id,
                        camera_timestamp_ns=packet.camera_timestamp_ns,
                        host_monotonic_ns=packet.host_monotonic_ns,
                        wall_time_ns=packet.wall_time_ns,
                    )
                )
            self.frames.sort(key=lambda frame: frame.index)
            self._finalize_files()
        except Exception as exc:
            self.recording_complete = False
            self.error_message = str(exc) or type(exc).__name__
            try:
                self._finalize_files()
            except Exception:
                pass
            self.failed.emit(self.error_message)
        finally:
            self.finished.emit(self.directory)

    def _finalize_files(self) -> None:
        if self.event_camera_ns is None and self.event_host_ns is not None:
            self.event_camera_ns = estimate_camera_event_timestamp_ns(
                self.anchors, self.event_host_ns
            )
            self.timing_uncertainty_ms = estimate_timing_uncertainty_ms(
                self.anchors, self.ads_roundtrip_ns
            )
        if self.event_camera_ns is not None:
            for frame in self.frames:
                frame.relative_to_light_barrier_ms = (
                    frame.camera_timestamp_ns - self.event_camera_ns
                ) / 1_000_000.0
        with (self.directory / "frames.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=FRAME_COLUMNS)
            writer.writeheader()
            for frame in self.frames:
                writer.writerow(
                    {
                        "index": frame.index,
                        "filename": frame.filename,
                        "frame_id": frame.frame_id,
                        "camera_timestamp_ns": frame.camera_timestamp_ns,
                        "host_monotonic_ns": frame.host_monotonic_ns,
                        "wall_time_utc": datetime.fromtimestamp(
                            frame.wall_time_ns / 1_000_000_000.0, timezone.utc
                        ).isoformat(),
                        "relative_to_light_barrier_ms": (
                            ""
                            if frame.relative_to_light_barrier_ms is None
                            else f"{frame.relative_to_light_barrier_ms:.6f}"
                        ),
                    }
                )
        self._write_json(
            {
                "version": 1,
                "session_id": self.session_id,
                "state": "complete" if self.recording_complete else "incomplete",
                "recorded_at_utc": self.started_at_utc,
                "light_barrier": self.light_barrier,
                "post_trigger_ms": self.post_trigger_ms,
                "stop_reason": self.stop_reason,
                "recording_complete": self.recording_complete,
                "error": self.error_message,
                "frame_count": len(self.frames),
                "frame_id_gaps": self.frame_id_gaps,
                "camera": self.camera_info,
                "plc_measurement_setup": self.measurement_settings,
                "trigger": {
                    "detected": self.event_camera_ns is not None,
                    "source": self.trigger_source,
                    "event_id": self.trigger_event_id,
                    "event_count": self.event_count,
                    "plc_time_ms": self.event_plc_time_ms,
                    "estimated_host_monotonic_ns": self.event_host_ns,
                    "received_host_monotonic_ns": self.event_received_host_ns,
                    "camera_timestamp_ns": self.event_camera_ns,
                    "estimated_camera_timestamp_ns": self.event_camera_ns,
                    "ads_roundtrip_ns": self.ads_roundtrip_ns,
                    "timing_uncertainty_ms": self.timing_uncertainty_ms,
                },
                "evaluation": {
                    "movement_frame_index": None,
                    "movement_filename": None,
                    "delay_ms": None,
                },
            }
        )

    def _write_json(self, document: dict[str, Any]) -> None:
        path = self.directory / "session.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(path)


def load_recording(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = Path(directory)
    document = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    with (directory / "frames.csv").open(newline="", encoding="utf-8") as handle:
        frames = list(csv.DictReader(handle))
    return document, frames


def update_movement_evaluation(
    directory: Path,
    frame_index: int,
    *,
    method: str = "manual",
    analysis_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory = Path(directory)
    document, frames = load_recording(directory)
    if not 0 <= int(frame_index) < len(frames):
        raise IndexError("Movement frame index is outside the recording")
    frame = frames[int(frame_index)]
    relative = frame.get("relative_to_light_barrier_ms", "")
    if relative in {"", None}:
        raise ValueError("This recording has no light-barrier timestamp")
    delay_ms = float(relative)
    document["evaluation"] = {
        "movement_frame_index": int(frame_index),
        "movement_filename": frame["filename"],
        "delay_ms": delay_ms,
        "method": str(method),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if analysis_details:
        document["evaluation"]["automatic_analysis"] = dict(analysis_details)
    path = directory / "session.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)
    _upsert_result(directory.parent, directory, document)
    return document


def _upsert_result(root: Path, directory: Path, document: dict[str, Any]) -> None:
    result_path = Path(root) / "calibration_results.csv"
    rows: list[dict[str, str]] = []
    if result_path.is_file():
        with result_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    evaluation = document["evaluation"]
    trigger = document.get("trigger", {})
    camera = document.get("camera", {})
    measurement_setup = document.get("plc_measurement_setup", {})
    row = {
        "session_id": str(document["session_id"]),
        "recorded_at_utc": str(document.get("recorded_at_utc", "")),
        "light_barrier": str(document.get("light_barrier", "")),
        "movement_frame_index": str(evaluation["movement_frame_index"]),
        "movement_filename": str(evaluation["movement_filename"]),
        "delay_ms": f"{float(evaluation['delay_ms']):.6f}",
        "evaluation_method": str(evaluation.get("method", "manual")),
        "timing_uncertainty_ms": (
            ""
            if trigger.get("timing_uncertainty_ms") is None
            else f"{float(trigger['timing_uncertainty_ms']):.6f}"
        ),
        "frame_count": str(document.get("frame_count", "")),
        "camera_model": str(camera.get("model", "")),
        "camera_serial": str(camera.get("serial", "")),
        "exposure_us": str(camera.get("exposure_us", "")),
        "array_index": str(measurement_setup.get("array_index", "")),
        "pressure_mbar": str(measurement_setup.get("pressure_mbar", "")),
        "pulse_duration_ms": str(
            measurement_setup.get("pulse_duration_ms", "")
        ),
        "fastest_response_mode": str(
            measurement_setup.get("fastest_response_mode", "")
        ),
        "session_directory": str(directory),
    }
    rows = [
        existing for existing in rows if existing.get("session_id") != row["session_id"]
    ]
    rows.append(row)
    temporary = result_path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(result_path)


class PressureDelayTab(QWidget):
    status_message = pyqtSignal(str)

    def __init__(
        self, parent: QWidget | None = None, ads_controller: Any | None = None
    ) -> None:
        super().__init__(parent)
        self.settings = QSettings("LeibnizUniversitaetHannover", "BiBaZuConveyorSetup")
        self.ads = ads_controller
        self.camera = UsbHighSpeedCamera(self)
        self.camera_connected = False
        self.ads_connected = False
        self.latest_status: dict[str, Any] | None = None
        self.camera_status: dict[str, Any] = {}
        self.session: RecordingSession | None = None
        self.trigger_baseline_count: int | None = None
        self.loaded_directory: Path | None = None
        self.loaded_document: dict[str, Any] | None = None
        self.loaded_frames: list[dict[str, Any]] = []
        self.analysis_worker: MovementAnalysisWorker | None = None
        self.analysis_in_progress = False
        self.analysis_result: dict[str, Any] | None = None
        self._exposure_edit_dirty = False
        self._updating_exposure_input = False
        self.fast_response_active = False
        self.fast_response_pending = ""
        self.fast_response_saved_values: dict[str, bool | int | float] = {}
        self._pressure_array_loaded: int | None = None
        self._pulse_array_loaded: int | None = None
        self._build_ui()
        self._connect_signals()
        self._update_controls()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        camera_box = QGroupBox("Baumer USB High-Speed Camera")
        camera_layout = QGridLayout(camera_box)
        self.camera_state_label = QLabel("Disconnected")
        self.camera_info_label = QLabel(
            f"Expected VCXU-02C · {DEFAULT_CAMERA_SERIAL} · {USB_CTI_PATH.name}"
        )
        self.camera_info_label.setWordWrap(True)
        self.connect_button = QPushButton("Connect / Rescan")
        self.disconnect_button = QPushButton("Disconnect")
        self.exposure_input = QDoubleSpinBox()
        self.exposure_input.setRange(20.0, 1_000_000.0)
        self.exposure_input.setDecimals(1)
        self.exposure_input.setSuffix(" µs")
        self.exposure_input.setValue(
            float(self.settings.value("pressure_delay/exposure_us", 4000.0))
        )
        self.apply_exposure_button = QPushButton("Apply Exposure")
        camera_layout.addWidget(QLabel("State"), 0, 0)
        camera_layout.addWidget(self.camera_state_label, 0, 1)
        camera_layout.addWidget(self.connect_button, 0, 2)
        camera_layout.addWidget(self.disconnect_button, 0, 3)
        camera_layout.addWidget(QLabel("Camera"), 1, 0)
        camera_layout.addWidget(self.camera_info_label, 1, 1, 1, 3)
        camera_layout.addWidget(QLabel("Exposure"), 2, 0)
        camera_layout.addWidget(self.exposure_input, 2, 1)
        camera_layout.addWidget(self.apply_exposure_button, 2, 2)
        layout.addWidget(camera_box)

        content = QGridLayout()
        self.image_label = QLabel("Connect the USB camera to show the live feed")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(640, 480)
        self.image_label.setStyleSheet("background: #111827; color: #d1d5db;")
        content.addWidget(self.image_label, 0, 0, 2, 1)

        recording_box = QGroupBox("Recording")
        recording_layout = QFormLayout(recording_box)
        self.barrier_input = QComboBox()
        for sensor in range(1, 9):
            self.barrier_input.addItem(f"Light barrier {sensor}", sensor)
        self.post_trigger_input = QSpinBox()
        self.post_trigger_input.setRange(10, 5000)
        self.post_trigger_input.setSuffix(" ms")
        self.post_trigger_input.setValue(
            int(
                self.settings.value(
                    "pressure_delay/post_trigger_ms", DEFAULT_POST_TRIGGER_MS
                )
            )
        )
        self.pressure_array_label = QLabel("Array 1 (LB 1/2)")
        self.pressure_input = QSpinBox()
        self.pressure_input.setRange(PRESSURE_MIN_MBAR, PRESSURE_MAX_MBAR)
        self.pressure_input.setSingleStep(10)
        self.pressure_input.setSuffix(" mbar")
        self.pressure_input.setValue(
            int(self.settings.value("pressure_delay/pressure_mbar", 3000))
        )
        self.apply_pressure_button = QPushButton("Apply Pressure")
        pressure_row = QWidget()
        pressure_layout = QHBoxLayout(pressure_row)
        pressure_layout.setContentsMargins(0, 0, 0, 0)
        pressure_layout.addWidget(self.pressure_input, 1)
        pressure_layout.addWidget(self.apply_pressure_button)
        self.pulse_duration_input = QSpinBox()
        self.pulse_duration_input.setRange(1, 500)
        self.pulse_duration_input.setSingleStep(1)
        self.pulse_duration_input.setSuffix(" ms")
        self.pulse_duration_input.setValue(
            int(self.settings.value("pressure_delay/pulse_duration_ms", 100))
        )
        self.apply_pulse_duration_button = QPushButton("Apply Pulse Duration")
        pulse_duration_row = QWidget()
        pulse_duration_layout = QHBoxLayout(pulse_duration_row)
        pulse_duration_layout.setContentsMargins(0, 0, 0, 0)
        pulse_duration_layout.addWidget(self.pulse_duration_input, 1)
        pulse_duration_layout.addWidget(self.apply_pulse_duration_button)
        self.enable_fast_response_button = QPushButton("Enable Fastest Response")
        self.enable_fast_response_button.setToolTip(
            "For the selected light barrier and paired nozzle array: disable "
            "debounce and set manual delay, offset and both response-delay "
            "compensations to zero. The current PLC values are saved first."
        )
        self.restore_plc_setup_button = QPushButton("Restore Previous Setup")
        fast_response_row = QWidget()
        fast_response_layout = QHBoxLayout(fast_response_row)
        fast_response_layout.setContentsMargins(0, 0, 0, 0)
        fast_response_layout.addWidget(self.enable_fast_response_button)
        fast_response_layout.addWidget(self.restore_plc_setup_button)
        self.plc_setup_state_label = QLabel(
            "Normal PLC timing; fastest-response mode is not enabled"
        )
        self.plc_setup_state_label.setWordWrap(True)
        default_output = (
            Path.home() / "Pictures" / "BiBaZu" / "PressureDelayCalibration"
        )
        self.output_input = QLineEdit(
            str(self.settings.value("pressure_delay/output", str(default_output)))
        )
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_input, 1)
        self.browse_output_button = QPushButton("Browse")
        output_layout.addWidget(self.browse_output_button)
        recording_layout.addRow("Stop trigger", self.barrier_input)
        recording_layout.addRow("Post-trigger", self.post_trigger_input)
        recording_layout.addRow("Pressure array", self.pressure_array_label)
        recording_layout.addRow("Test pressure", pressure_row)
        recording_layout.addRow("Pulse duration", pulse_duration_row)
        recording_layout.addRow("Fast reaction", fast_response_row)
        recording_layout.addRow("PLC setup", self.plc_setup_state_label)
        recording_layout.addRow("Output", output_row)
        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.record_button = QPushButton("Record")
        self.stop_button = QPushButton("Stop")
        self.open_button = QPushButton("Open Recording")
        button_layout.addWidget(self.record_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addWidget(self.open_button)
        recording_layout.addRow(buttons)
        self.recording_state_label = QLabel("Ready")
        self.recording_state_label.setWordWrap(True)
        self.frame_count_label = QLabel("0 frames")
        recording_layout.addRow("State", self.recording_state_label)
        recording_layout.addRow("Captured", self.frame_count_label)
        content.addWidget(recording_box, 0, 1)

        review_box = QGroupBox("Frame Review")
        review_layout = QVBoxLayout(review_box)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(0, 0)
        review_layout.addWidget(self.frame_slider)
        navigation = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.analyze_movement_button = QPushButton("Analyze Movement")
        self.compare_recordings_button = QPushButton("Compare Recordings…")
        self.mark_movement_button = QPushButton("Mark First Movement")
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        review_layout.addLayout(navigation)
        analysis_actions = QHBoxLayout()
        analysis_actions.addWidget(self.analyze_movement_button)
        analysis_actions.addWidget(self.mark_movement_button)
        review_layout.addLayout(analysis_actions)
        review_layout.addWidget(self.compare_recordings_button)
        self.review_frame_label = QLabel("No recording loaded")
        self.review_time_label = QLabel("-")
        self.result_label = QLabel("Delay: -")
        self.result_label.setStyleSheet("font-weight: 600;")
        review_layout.addWidget(self.review_frame_label)
        review_layout.addWidget(self.review_time_label)
        review_layout.addWidget(self.result_label)
        content.addWidget(review_box, 1, 1)
        content.setColumnStretch(0, 3)
        content.setColumnStretch(1, 2)
        layout.addLayout(content)

    def _connect_signals(self) -> None:
        self.connect_button.clicked.connect(self.connect_camera)
        self.disconnect_button.clicked.connect(self.camera.disconnect_camera)
        self.exposure_input.valueChanged.connect(self._exposure_input_changed)
        self.apply_exposure_button.clicked.connect(self._apply_exposure)
        self.barrier_input.currentIndexChanged.connect(
            self._selected_barrier_changed
        )
        self.apply_pressure_button.clicked.connect(self._apply_pressure)
        self.apply_pulse_duration_button.clicked.connect(
            self._apply_pulse_duration
        )
        self.enable_fast_response_button.clicked.connect(
            self._enable_fast_response
        )
        self.restore_plc_setup_button.clicked.connect(
            self._restore_previous_plc_setup
        )
        self.browse_output_button.clicked.connect(self._browse_output)
        self.record_button.clicked.connect(self.start_recording)
        self.stop_button.clicked.connect(lambda: self.stop_recording("manual"))
        self.open_button.clicked.connect(self._open_recording_dialog)
        self.frame_slider.valueChanged.connect(self._show_review_frame)
        self.previous_button.clicked.connect(
            lambda: self.frame_slider.setValue(self.frame_slider.value() - 1)
        )
        self.next_button.clicked.connect(
            lambda: self.frame_slider.setValue(self.frame_slider.value() + 1)
        )
        self.analyze_movement_button.clicked.connect(self._analyze_movement)
        self.compare_recordings_button.clicked.connect(self._compare_recordings)
        self.mark_movement_button.clicked.connect(self._mark_movement)
        self.camera.connection_changed.connect(self._camera_connection_changed)
        self.camera.camera_info.connect(self._camera_info_changed)
        self.camera.preview_ready.connect(self._show_live_frame)
        self.camera.recording_frame.connect(self._record_frame)
        self.camera.hardware_trigger.connect(self._hardware_trigger)
        self.camera.exposure_applied.connect(self._exposure_applied)
        self.camera.error.connect(self._camera_error)
        if self.ads is not None:
            self.ads.write_finished.connect(self._ads_write_finished)
            self.ads.operation_failed.connect(self._ads_operation_failed)

    def connect_camera(self) -> None:
        if self.camera.running:
            return
        self.camera_state_label.setText("Discovering via bgapi2_usb.cti …")
        self.camera.connect_camera(DEFAULT_CAMERA_SERIAL)
        self._update_controls()

    def activate(self) -> None:
        if not self.camera.running:
            self.connect_camera()

    @pyqtSlot(bool, str)
    def _camera_connection_changed(self, connected: bool, message: str) -> None:
        self.camera_connected = connected
        self.camera_state_label.setText(
            "Connected" if connected else (message or "Disconnected")
        )
        if not connected and self.session is not None:
            self.stop_recording("camera_disconnected", "Camera disconnected")
        self._update_controls()

    @pyqtSlot(object)
    def _camera_info_changed(self, info: object) -> None:
        if not isinstance(info, dict):
            return
        self.camera_status = dict(info)
        trigger_status = (
            "ready" if info.get("hardware_trigger_available") else "ADS fallback"
        )
        self.camera_info_label.setText(
            f"{info.get('model', '?')} · SN {info.get('serial', '?')} · "
            f"{info.get('width', 0)}×{info.get('height', 0)} · "
            f"{info.get('pixel_format', '?')} · {info.get('stream_fps', 0.0):.1f} FPS"
            f" · Line0 {trigger_status}"
        )
        if not self._exposure_edit_dirty and not self.exposure_input.hasFocus():
            self._updating_exposure_input = True
            try:
                self.exposure_input.setRange(
                    float(info.get("exposure_min_us", 20.0)),
                    float(info.get("exposure_max_us", 1_000_000.0)),
                )
                if info.get("exposure_us") is not None:
                    self.exposure_input.setValue(float(info["exposure_us"]))
            finally:
                self._updating_exposure_input = False

    @pyqtSlot(float)
    def _exposure_input_changed(self, _value: float) -> None:
        if not self._updating_exposure_input:
            self._exposure_edit_dirty = True

    def _apply_exposure(self, _checked: bool = False) -> None:
        self._exposure_edit_dirty = True
        self.camera.set_exposure(self.exposure_input.value())

    @pyqtSlot(float)
    def _exposure_applied(self, value: float) -> None:
        self._updating_exposure_input = True
        try:
            self.exposure_input.setValue(value)
        finally:
            self._updating_exposure_input = False
        self._exposure_edit_dirty = False
        self.settings.setValue("pressure_delay/exposure_us", value)
        self.status_message.emit(f"High-speed camera exposure set to {value:.1f} µs")

    @pyqtSlot(str)
    def _camera_error(self, message: str) -> None:
        self.camera_state_label.setText(f"Camera error: {message}")
        if self.session is not None:
            self.stop_recording("camera_error", message)
        self.status_message.emit(f"High-speed camera: {message}")

    @pyqtSlot(object)
    def _show_live_frame(self, packet: object) -> None:
        if not isinstance(packet, FramePacket):
            return
        try:
            rgb = frame_to_rgb(packet.image, packet.pixel_format)
            image = QImage(
                rgb.data,
                packet.width,
                packet.height,
                int(rgb.strides[0]),
                QImage.Format.Format_RGB888,
            ).copy()
            self._set_pixmap(QPixmap.fromImage(image))
        except Exception as exc:
            self.camera_state_label.setText(f"Preview error: {exc}")

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def _selected_array_index(self) -> int:
        return pressure_array_for_barrier(int(self.barrier_input.currentData()))

    def _selected_array_status(self) -> dict[str, Any] | None:
        if not self.latest_status:
            return None
        selected = self._selected_array_index()
        for values in self.latest_status.get("arrays", []):
            if int(values.get("index", 0)) == selected:
                return dict(values)
        return None

    @pyqtSlot(int)
    def _selected_barrier_changed(self, _index: int) -> None:
        sensor = int(self.barrier_input.currentData())
        array_index = pressure_array_for_barrier(sensor)
        first_barrier = array_index * 2 - 1
        self.pressure_array_label.setText(
            f"Array {array_index} (LB {first_barrier}/{first_barrier + 1})"
        )
        self._pressure_array_loaded = None
        self._pulse_array_loaded = None
        self._sync_pressure_from_status()
        self._render_plc_setup_state()
        self._update_controls()

    def _sync_pressure_from_status(self) -> None:
        if self.fast_response_active or self.fast_response_pending:
            return
        array_index = self._selected_array_index()
        if (
            self._pressure_array_loaded == array_index
            and self._pulse_array_loaded == array_index
        ):
            return
        values = self._selected_array_status()
        if values is None:
            return
        if self._pressure_array_loaded != array_index:
            self.pressure_input.setValue(int(values["pressure_mbar"]))
            self._pressure_array_loaded = array_index
        if self._pulse_array_loaded != array_index:
            self.pulse_duration_input.setValue(int(values["pulse_duration_ms"]))
            self._pulse_array_loaded = array_index

    def _render_plc_setup_state(self) -> None:
        sensor = int(self.barrier_input.currentData())
        array_index = self._selected_array_index()
        if self.fast_response_pending == "enable":
            self.plc_setup_state_label.setText("Applying fastest-response settings …")
            return
        if self.fast_response_pending == "restore":
            self.plc_setup_state_label.setText("Restoring previous PLC settings …")
            return
        if self.fast_response_pending == "pressure":
            self.plc_setup_state_label.setText(
                f"Applying {self.pressure_input.value()} mbar to array "
                f"{array_index} …"
            )
            return
        if self.fast_response_pending == "pulse_duration":
            self.plc_setup_state_label.setText(
                f"Applying {self.pulse_duration_input.value()} ms pulse duration "
                f"to array {array_index} …"
            )
            return
        if self.fast_response_active:
            self.plc_setup_state_label.setText(
                f"FASTEST RESPONSE ACTIVE · array {array_index}: pressure "
                f"{self.pressure_input.value()} mbar, pulse "
                f"{self.pulse_duration_input.value()} ms, delay 0 ms, offset 0 mm, "
                f"response compensation 0 ms; LB {sensor} debounce disabled"
            )
            return
        values = self._selected_array_status()
        debounce_values = (
            self.latest_status.get("light_barrier_debounce_enabled", [])
            if self.latest_status
            else []
        )
        if values is None or len(debounce_values) < sensor:
            self.plc_setup_state_label.setText(
                "Normal PLC timing; waiting for current setup values"
            )
            return
        self.plc_setup_state_label.setText(
            f"Normal timing · array {array_index}: pulse "
            f"{values['pulse_duration_ms']} ms, delay {values['delay_ms']} ms, "
            f"offset {float(values['offset_mm']):.2f} mm; LB {sensor} debounce "
            f"{'on' if debounce_values[sensor - 1] else 'off'}"
        )

    def _current_restore_values(self) -> dict[str, bool | int | float]:
        sensor = int(self.barrier_input.currentData())
        array_index = self._selected_array_index()
        values = self._selected_array_status()
        debounce_values = (
            self.latest_status.get("light_barrier_debounce_enabled", [])
            if self.latest_status
            else []
        )
        if values is None or len(debounce_values) < sensor:
            raise RuntimeError("Current PLC timing values are not available yet")
        return {
            f"MAIN.GuiPressureMbar{array_index}": int(values["pressure_mbar"]),
            f"MAIN.GuiDelayMs{array_index}": int(values["delay_ms"]),
            f"MAIN.GuiOffsetMm{array_index}": float(values["offset_mm"]),
            f"MAIN.GuiForceResponseDelayMs{array_index}": float(
                values["force_response_delay_ms"]
            ),
            f"MAIN.GuiForceSingleNozzleResponseDelayMs{array_index}": float(
                values["force_single_nozzle_response_delay_ms"]
            ),
            f"MAIN.GuiLightBarrierDebounceEnabled{sensor}": bool(
                debounce_values[sensor - 1]
            ),
        }

    def _measurement_settings_snapshot(self) -> dict[str, Any]:
        sensor = int(self.barrier_input.currentData())
        array_index = self._selected_array_index()
        values = self._selected_array_status() or {}
        debounce_values = (
            self.latest_status.get("light_barrier_debounce_enabled", [])
            if self.latest_status
            else []
        )
        fast = self.fast_response_active
        return {
            "fastest_response_mode": fast,
            "array_index": array_index,
            "pressure_mbar": (
                self.pressure_input.value()
                if fast
                else values.get("pressure_mbar")
            ),
            "pulse_duration_ms": values.get(
                "pulse_duration_ms", self.pulse_duration_input.value()
            ),
            "manual_delay_ms": 0 if fast else values.get("delay_ms"),
            "offset_mm": 0.0 if fast else values.get("offset_mm"),
            "force_response_delay_ms": (
                0.0 if fast else values.get("force_response_delay_ms")
            ),
            "force_single_nozzle_response_delay_ms": (
                0.0
                if fast
                else values.get("force_single_nozzle_response_delay_ms")
            ),
            "light_barrier": sensor,
            "light_barrier_debounce_enabled": (
                False
                if fast
                else (
                    bool(debounce_values[sensor - 1])
                    if len(debounce_values) >= sensor
                    else None
                )
            ),
        }

    def _apply_pressure(self, _checked: bool = False) -> None:
        if self.ads is None or not self.ads_connected:
            return
        array_index = self._selected_array_index()
        pressure = self.pressure_input.value()
        self.settings.setValue("pressure_delay/pressure_mbar", pressure)
        self.fast_response_pending = "pressure"
        self.ads.write_now(
            {f"MAIN.GuiPressureMbar{array_index}": pressure},
            f"{PRESSURE_APPLY_CONTEXT_PREFIX}{array_index}",
        )
        self.plc_setup_state_label.setText(
            f"Applying {pressure} mbar to array {array_index} …"
        )
        self._update_controls()

    def _enable_fast_response(self, _checked: bool = False) -> None:
        if (
            self.ads is None
            or not self.ads_connected
            or self.session is not None
            or self.fast_response_pending
            or self.fast_response_active
        ):
            return
        try:
            self.fast_response_saved_values = self._current_restore_values()
        except Exception as exc:
            QMessageBox.warning(self, "Fastest response", str(exc))
            return
        sensor = int(self.barrier_input.currentData())
        self.settings.setValue(
            "pressure_delay/pressure_mbar", self.pressure_input.value()
        )
        self.fast_response_pending = "enable"
        self.ads.write_now(
            fastest_response_write_values(sensor, self.pressure_input.value()),
            FAST_RESPONSE_ENABLE_CONTEXT,
        )
        self._render_plc_setup_state()
        self._update_controls()

    def _apply_pulse_duration(self, _checked: bool = False) -> None:
        if self.ads is None or not self.ads_connected:
            return
        array_index = self._selected_array_index()
        duration_ms = self.pulse_duration_input.value()
        self.settings.setValue("pressure_delay/pulse_duration_ms", duration_ms)
        self.fast_response_pending = "pulse_duration"
        self.ads.write_now(
            {f"MAIN.GuiPulseDurationMs{array_index}": duration_ms},
            f"{PULSE_DURATION_APPLY_CONTEXT_PREFIX}{array_index}",
        )
        self._render_plc_setup_state()
        self._update_controls()

    def _restore_previous_plc_setup(self, _checked: bool = False) -> None:
        if (
            self.ads is None
            or not self.ads_connected
            or self.session is not None
            or self.fast_response_pending
            or not self.fast_response_saved_values
        ):
            return
        self.fast_response_pending = "restore"
        self.ads.write_now(
            dict(self.fast_response_saved_values), FAST_RESPONSE_RESTORE_CONTEXT
        )
        self._render_plc_setup_state()
        self._update_controls()

    @pyqtSlot(str)
    def _ads_write_finished(self, context: str) -> None:
        if context == FAST_RESPONSE_ENABLE_CONTEXT:
            self.fast_response_pending = ""
            self.fast_response_active = True
            self.status_message.emit("Fastest-response PLC settings are active")
        elif context == FAST_RESPONSE_RESTORE_CONTEXT:
            self.fast_response_pending = ""
            self.fast_response_active = False
            self.fast_response_saved_values = {}
            self._pressure_array_loaded = None
            self._pulse_array_loaded = None
            self.status_message.emit("Previous PLC timing and pressure restored")
        elif context.startswith(PRESSURE_APPLY_CONTEXT_PREFIX):
            self.fast_response_pending = ""
            values = self._selected_array_status()
            if values is not None and self.latest_status is not None:
                for cached in self.latest_status.get("arrays", []):
                    if int(cached.get("index", 0)) == self._selected_array_index():
                        cached["pressure_mbar"] = self.pressure_input.value()
                        break
            self.status_message.emit(
                f"Test pressure applied: {self.pressure_input.value()} mbar"
            )
        elif context.startswith(PULSE_DURATION_APPLY_CONTEXT_PREFIX):
            self.fast_response_pending = ""
            if self.latest_status is not None:
                for cached in self.latest_status.get("arrays", []):
                    if int(cached.get("index", 0)) == self._selected_array_index():
                        cached["pulse_duration_ms"] = (
                            self.pulse_duration_input.value()
                        )
                        break
            self.status_message.emit(
                f"Pulse duration applied: {self.pulse_duration_input.value()} ms"
            )
        else:
            return
        self._render_plc_setup_state()
        self._update_controls()

    @pyqtSlot(str, str)
    def _ads_operation_failed(self, context: str, message: str) -> None:
        if context not in {
            FAST_RESPONSE_ENABLE_CONTEXT,
            FAST_RESPONSE_RESTORE_CONTEXT,
        } and not context.startswith(
            PRESSURE_APPLY_CONTEXT_PREFIX
        ) and not context.startswith(PULSE_DURATION_APPLY_CONTEXT_PREFIX):
            return
        self.fast_response_pending = ""
        if context == FAST_RESPONSE_ENABLE_CONTEXT:
            self.fast_response_active = False
        self.plc_setup_state_label.setText(f"PLC setup failed: {message}")
        self._update_controls()

    def set_ads_connected(self, connected: bool) -> None:
        self.ads_connected = bool(connected)
        if not connected:
            self.fast_response_pending = ""
            if self.fast_response_saved_values:
                self.plc_setup_state_label.setText(
                    "ADS connection lost; reconnect and restore the saved PLC setup"
                )
        if not connected and self.session is not None:
            self.stop_recording("ads_disconnected", "ADS connection lost")
        self._update_controls()

    def process_setup_status(self, status: dict[str, Any]) -> None:
        self.latest_status = status
        self._sync_pressure_from_status()
        self._render_plc_setup_state()
        if (
            self.session is None
            or getattr(self.session, "event_camera_ns", None) is not None
        ):
            self._update_controls()
            return
        if self.camera_status.get("hardware_trigger_available"):
            self._update_controls()
            return
        sensor = self.session.light_barrier
        counts = status.get("light_barrier_event_counts")
        times = status.get("light_barrier_event_times_ms")
        states = status.get("light_barriers")
        if (
            not counts
            or not times
            or not states
            or len(counts) < sensor
            or len(times) < sensor
            or len(states) < sensor
        ):
            return
        count = int(counts[sensor - 1])
        if self.trigger_baseline_count is None or count == self.trigger_baseline_count:
            return
        self.trigger_baseline_count = count
        if bool(states[sensor - 1]):
            self.recording_state_label.setText(
                f"Recording; ignored non-falling LB {sensor} transition"
            )
            return
        sampled_ns = status.get("sampled_monotonic_ns")
        plc_clock_ms = status.get("plc_event_clock_ms")
        if sampled_ns is None or plc_clock_ms is None:
            self.stop_recording(
                "timing_error", "PLC event-clock synchronization is unavailable"
            )
            return
        event_plc_ms = int(times[sensor - 1])
        event_host_ns = estimate_plc_event_host_ns(
            int(sampled_ns), int(plc_clock_ms), event_plc_ms
        )
        self.session.set_trigger(
            count,
            event_plc_ms,
            event_host_ns,
            int(status.get("ads_roundtrip_ns", 0)),
        )
        self.recording_state_label.setText(
            f"LB {sensor} triggered; recording "
            f"{self.session.post_trigger_ms} ms post-roll"
        )

    @pyqtSlot(object)
    def _hardware_trigger(self, event: object) -> None:
        if self.session is None or not isinstance(event, dict):
            return
        accepted = self.session.set_hardware_trigger(
            int(event["camera_timestamp_ns"]),
            int(event["received_host_monotonic_ns"]),
            int(event.get("event_id", LINE0_RISING_EVENT_ID)),
        )
        if accepted:
            self.recording_state_label.setText(
                f"LB {self.session.light_barrier} hardware edge captured; recording "
                f"{self.session.post_trigger_ms} ms post-roll"
            )

    def _browse_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Pressure-delay recording directory", self.output_input.text()
        )
        if selected:
            self.output_input.setText(selected)
            self.settings.setValue("pressure_delay/output", selected)

    def start_recording(self) -> None:
        if self.session is not None:
            return
        if (
            not self.camera_connected
            or not self.ads_connected
            or self.latest_status is None
            or self.fast_response_pending
        ):
            QMessageBox.warning(
                self,
                "Recording unavailable",
                "The USB camera and ADS setup status must both be online.",
            )
            return
        sensor = int(self.barrier_input.currentData())
        counts = self.latest_status.get("light_barrier_event_counts")
        if not counts or len(counts) < sensor:
            QMessageBox.warning(
                self, "Recording unavailable", "No barrier counters received."
            )
            return
        output = Path(self.output_input.text()).expanduser()
        self.settings.setValue("pressure_delay/output", str(output))
        self.settings.setValue(
            "pressure_delay/post_trigger_ms", self.post_trigger_input.value()
        )
        try:
            self.session = RecordingSession(
                output,
                sensor,
                self.post_trigger_input.value(),
                self.camera_status,
                self,
                measurement_settings=self._measurement_settings_snapshot(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Recording error", str(exc))
            self.session = None
            return
        self.trigger_baseline_count = int(counts[sensor - 1])
        self.session.auto_stop_requested.connect(
            lambda: self.stop_recording("post_trigger_complete")
        )
        self.session.failed.connect(self._session_failed)
        self.session.finished.connect(self._session_finished)
        self.camera.set_recording(True)
        self.recording_state_label.setText(f"Recording; waiting for LB {sensor}")
        self.frame_count_label.setText("0 frames")
        self._update_controls()

    @pyqtSlot(object)
    def _record_frame(self, packet: object) -> None:
        if self.session is None or not isinstance(packet, FramePacket):
            return
        self.session.add_frame(packet)
        self.frame_count_label.setText(f"{len(self.session.anchors)} frames")

    def stop_recording(self, reason: str, error_message: str = "") -> None:
        if self.session is None:
            return
        self.camera.set_recording(False)
        self.session.stop(reason, error_message)
        self.recording_state_label.setText("Finalizing images and metadata …")
        self._update_controls(finalizing=True)

    @pyqtSlot(str)
    def _session_failed(self, message: str) -> None:
        if self.session is not None and self.session._accepting:
            self.stop_recording("writer_error", message)
        self.status_message.emit(f"Pressure-delay recording failed: {message}")

    @pyqtSlot(object)
    def _session_finished(self, directory: object) -> None:
        path = Path(directory)
        session = self.session
        self.session = None
        self.trigger_baseline_count = None
        self.recording_state_label.setText(
            "Saved"
            if session is None or session.recording_complete
            else "Saved as incomplete"
        )
        self.load_recording(path)
        self._update_controls()
        self.status_message.emit(f"Pressure-delay recording saved: {path}")
        if (
            (session is None or session.recording_complete)
            and self.loaded_document is not None
            and self.loaded_document.get("trigger", {}).get("detected")
        ):
            self._analyze_movement()

    def _open_recording_dialog(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Open pressure-delay recording", self.output_input.text()
        )
        if selected:
            try:
                self.load_recording(Path(selected))
            except Exception as exc:
                QMessageBox.critical(self, "Open recording", str(exc))

    def _compare_recordings(self) -> None:
        selector = RecordingFolderSelectionDialog(
            Path(self.output_input.text()), self
        )
        if selector.exec() != QDialog.DialogCode.Accepted:
            return
        directories = selector.selected_recording_directories()
        if not directories:
            QMessageBox.information(
                self,
                "Compare recordings",
                "No recording folders containing session.json were selected.",
            )
            return
        try:
            comparison = build_pressure_delay_comparison(directories)
        except Exception as exc:
            QMessageBox.warning(self, "Compare recordings", str(exc))
            return
        PressureDelayPlotDialog(comparison, self).exec()

    def load_recording(self, directory: Path) -> None:
        document, frames = load_recording(directory)
        self.loaded_directory = Path(directory)
        self.loaded_document = document
        self.loaded_frames = frames
        self.analysis_result = None
        self.frame_slider.setRange(0, max(0, len(frames) - 1))
        evaluation = document.get("evaluation", {})
        movement_index = evaluation.get("movement_frame_index")
        trigger = document.get("trigger", {})
        if (
            movement_index is not None
            and 0 <= int(movement_index) < len(frames)
        ):
            self.frame_slider.setValue(int(movement_index))
        elif trigger.get("detected"):
            closest = min(
                range(len(frames)),
                key=lambda index: abs(
                    float(frames[index].get("relative_to_light_barrier_ms") or 0.0)
                ),
                default=0,
            )
            self.frame_slider.setValue(closest)
        else:
            self.frame_slider.setValue(0)
        if evaluation.get("delay_ms") is not None:
            prefix = (
                "Automatic delay"
                if evaluation.get("method") == "automatic_optical_flow"
                else "Delay"
            )
            self.result_label.setText(
                f"{prefix}: {float(evaluation['delay_ms']):.3f} ms"
            )
        else:
            self.result_label.setText("Delay: not marked")
        self._show_review_frame(self.frame_slider.value())
        self._update_controls()

    def _analyze_movement(self) -> None:
        if (
            self.analysis_in_progress
            or self.loaded_directory is None
            or not self.loaded_frames
        ):
            return
        self.analysis_in_progress = True
        self.analysis_result = None
        self.result_label.setText("Analyzing movement …")
        worker = MovementAnalysisWorker(
            self.loaded_directory, self.loaded_frames, self
        )
        worker.result_ready.connect(self._analysis_ready)
        worker.analysis_failed.connect(self._analysis_failed)
        worker.finished.connect(self._analysis_finished)
        self.analysis_worker = worker
        worker.start()
        self._update_controls()

    @pyqtSlot(object)
    def _analysis_ready(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        self.analysis_result = dict(result)
        index = int(result["frame_index"])
        delay_ms = float(result["relative_to_light_barrier_ms"])
        previous_ms = result.get("previous_relative_ms")
        self.frame_slider.setValue(index)
        if self.loaded_directory is None:
            self._analysis_failed("The recording directory is no longer loaded")
            return
        try:
            self.loaded_document = update_movement_evaluation(
                self.loaded_directory,
                index,
                method="automatic_optical_flow",
                analysis_details=self.analysis_result,
            )
        except Exception as exc:
            self._analysis_failed(
                f"Movement was detected but could not be saved: {exc}"
            )
            return
        if previous_ms is None:
            onset = f"at {delay_ms:+.3f} ms"
        else:
            onset = f"between {float(previous_ms):+.3f} and {delay_ms:+.3f} ms"
        self.result_label.setText(
            f"Automatic delay: {delay_ms:.3f} ms · Frame {index + 1} · "
            f"onset {onset}"
        )
        self.status_message.emit(
            f"First movement automatically marked: frame {index + 1}, "
            f"{delay_ms:.3f} ms; review and change it manually if needed"
        )

    @pyqtSlot(str)
    def _analysis_failed(self, message: str) -> None:
        self.analysis_result = None
        self.result_label.setText(f"Automatic analysis failed: {message}")
        self.status_message.emit(f"Automatic movement analysis failed: {message}")

    @pyqtSlot()
    def _analysis_finished(self) -> None:
        worker = self.analysis_worker
        self.analysis_worker = None
        self.analysis_in_progress = False
        if worker is not None:
            worker.deleteLater()
        self._update_controls()

    @pyqtSlot(int)
    def _show_review_frame(self, index: int) -> None:
        if not self.loaded_frames or self.loaded_directory is None:
            return
        index = max(0, min(int(index), len(self.loaded_frames) - 1))
        frame = self.loaded_frames[index]
        pixmap = QPixmap(str(self.loaded_directory / frame["filename"]))
        if not pixmap.isNull():
            self._set_pixmap(pixmap)
        self.review_frame_label.setText(
            f"Frame {index + 1}/{len(self.loaded_frames)} · "
            f"camera ID {frame['frame_id']}"
        )
        relative = frame.get("relative_to_light_barrier_ms", "")
        if relative in {"", None}:
            self.review_time_label.setText(
                "No light-barrier timestamp in this recording"
            )
        else:
            uncertainty = (
                self.loaded_document.get("trigger", {}).get("timing_uncertainty_ms")
                if self.loaded_document
                else None
            )
            suffix = (
                ""
                if uncertainty is None
                else f" · estimated ±{float(uncertainty):.2f} ms"
            )
            self.review_time_label.setText(
                f"LB-relative time: {float(relative):+.3f} ms{suffix}"
            )
        self._update_controls()

    def _mark_movement(self) -> None:
        if self.loaded_directory is None:
            return
        try:
            self.loaded_document = update_movement_evaluation(
                self.loaded_directory, self.frame_slider.value()
            )
        except Exception as exc:
            QMessageBox.warning(self, "Movement frame", str(exc))
            return
        delay = float(self.loaded_document["evaluation"]["delay_ms"])
        self.result_label.setText(f"Delay: {delay:.3f} ms")
        self.status_message.emit(f"LB-to-movement delay saved: {delay:.3f} ms")

    def _update_controls(self, finalizing: bool = False) -> None:
        recording = self.session is not None
        analyzing = self.analysis_in_progress
        ready = (
            self.camera_connected
            and self.ads_connected
            and self.latest_status is not None
            and not recording
            and not finalizing
            and not self.fast_response_pending
            and not analyzing
        )
        plc_ready = (
            self.ads_connected
            and self.latest_status is not None
            and self._selected_array_status() is not None
            and not recording
            and not finalizing
            and not self.fast_response_pending
            and not analyzing
        )
        self.connect_button.setEnabled(not self.camera.running)
        self.disconnect_button.setEnabled(self.camera.running and not recording)
        self.apply_exposure_button.setEnabled(self.camera_connected and not recording)
        self.exposure_input.setEnabled(self.camera_connected and not recording)
        self.record_button.setEnabled(ready)
        self.stop_button.setEnabled(recording and not finalizing)
        self.barrier_input.setEnabled(
            not recording
            and not self.fast_response_pending
            and not self.fast_response_saved_values
        )
        self.pressure_input.setEnabled(plc_ready)
        self.apply_pressure_button.setEnabled(plc_ready)
        self.pulse_duration_input.setEnabled(plc_ready)
        self.apply_pulse_duration_button.setEnabled(plc_ready)
        self.enable_fast_response_button.setEnabled(
            plc_ready
            and not self.fast_response_active
            and not self.fast_response_saved_values
        )
        self.restore_plc_setup_button.setEnabled(
            plc_ready and bool(self.fast_response_saved_values)
        )
        self.post_trigger_input.setEnabled(not recording)
        self.output_input.setEnabled(not recording)
        self.browse_output_button.setEnabled(not recording)
        self.open_button.setEnabled(not recording and not analyzing)
        self.compare_recordings_button.setEnabled(not recording and not analyzing)
        loaded = bool(self.loaded_frames) and not recording and not analyzing
        self.frame_slider.setEnabled(loaded)
        self.previous_button.setEnabled(loaded and self.frame_slider.value() > 0)
        self.next_button.setEnabled(
            loaded and self.frame_slider.value() < len(self.loaded_frames) - 1
        )
        current_has_time = loaded and self.loaded_frames[self.frame_slider.value()].get(
            "relative_to_light_barrier_ms"
        ) not in {"", None}
        self.analyze_movement_button.setEnabled(current_has_time)
        self.mark_movement_button.setEnabled(current_has_time)

    def shutdown(self) -> None:
        if self.analysis_worker is not None and self.analysis_worker.isRunning():
            self.analysis_worker.wait(5000)
        if self.session is not None:
            self.camera.set_recording(False)
            self.session.stop("application_close")
            self.session.wait(5.0)
            self.session = None
        if (
            self.ads is not None
            and self.ads_connected
            and self.fast_response_saved_values
            and self.fast_response_pending != "restore"
        ):
            self.ads.write_now(
                dict(self.fast_response_saved_values), FAST_RESPONSE_RESTORE_CONTEXT
            )
        self.camera.shutdown()
