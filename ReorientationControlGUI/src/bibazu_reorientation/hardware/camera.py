from __future__ import annotations

import queue
import re
import threading
import time
from typing import Any

import cv2
import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from bibazu_reorientation.hardware.base import DeviceAdapter
from bibazu_reorientation.models import CameraFrame, CameraStatus, ConnectionState
from bibazu_reorientation.settings import AppSettings

FETCH_TIMEOUT_SECONDS = 1.0
FETCH_TIMEOUT_MARGIN_SECONDS = 0.5
PREVIEW_MAX_WIDTH = 1280
PREVIEW_MAX_HEIGHT = 720


def advance_frame_deadline(deadline: float, interval: float, now: float) -> float:
    """Advance a preview deadline without building up delayed Qt frames."""
    if interval <= 0.0:
        return now
    if deadline <= 0.0:
        return now + interval
    if deadline > now:
        return deadline
    missed_intervals = int((now - deadline) // interval) + 1
    return deadline + missed_intervals * interval


def camera_fetch_timeout_seconds(exposure_time_us: float | None) -> float:
    """Allow a complete exposure plus transfer margin before a fetch times out."""
    if exposure_time_us is None:
        return FETCH_TIMEOUT_SECONDS
    return max(
        FETCH_TIMEOUT_SECONDS,
        max(0.0, float(exposure_time_us)) / 1_000_000.0 + FETCH_TIMEOUT_MARGIN_SECONDS,
    )


def _find_node(node_map: Any, *names: str) -> Any:
    for name in names:
        try:
            return getattr(node_map, name)
        except Exception:
            continue
    return None


def _node_value(node_map: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(node_map, name).value
    except Exception:
        return default


def _node_number(node: Any, attribute: str) -> float | None:
    try:
        return float(getattr(node, attribute))
    except Exception:
        return None


def _node_writable(node: Any) -> bool:
    if node is None:
        return False
    try:
        from genicam.genapi import is_writable

        return bool(is_writable(node))
    except Exception:
        access_mode = str(getattr(node, "access_mode", "")).upper()
        return access_mode in {"RW", "WO", "4"}


def _uint8(image: np.ndarray, pixel_format: str) -> np.ndarray:
    if image.dtype == np.uint8:
        return image
    match = re.search(r"(10|12|14|16)", pixel_format)
    bits = int(match.group(1)) if match else image.dtype.itemsize * 8
    return np.clip(image.astype(np.float32) * (255.0 / ((1 << bits) - 1)), 0, 255).astype(np.uint8)


def convert_to_rgb(data: Any, width: int, height: int, pixel_format: str) -> np.ndarray:
    image = np.asarray(data)
    original_format = str(pixel_format)
    fmt = original_format.lower()
    if "packed" in fmt or fmt.endswith("p"):
        raise ValueError(f"Packed pixel format is not supported: {pixel_format}")
    if fmt.startswith("mono"):
        if image.size != width * height:
            raise ValueError(f"Unexpected data size for {original_format}: {image.size}")
        mono = _uint8(image.reshape(height, width), pixel_format)
        return np.ascontiguousarray(cv2.cvtColor(mono, cv2.COLOR_GRAY2RGB))

    bayer_codes = {
        "bayerrg": cv2.COLOR_BayerRG2RGB,
        "bayerbg": cv2.COLOR_BayerBG2RGB,
        "bayergr": cv2.COLOR_BayerGR2RGB,
        "bayergb": cv2.COLOR_BayerGB2RGB,
    }
    for prefix, code in bayer_codes.items():
        if fmt.startswith(prefix):
            if image.size != width * height:
                raise ValueError(f"Unexpected data size for {original_format}: {image.size}")
            mosaic = _uint8(image.reshape(height, width), pixel_format)
            return np.ascontiguousarray(cv2.cvtColor(mosaic, code))

    if fmt.startswith("rgb8"):
        return np.ascontiguousarray(image.reshape(height, width, 3).astype(np.uint8))
    if fmt.startswith("bgr8"):
        bgr = image.reshape(height, width, 3).astype(np.uint8)
        return np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    raise ValueError(f"Unsupported pixel format: {original_format}")


def resize_preview(
    image: np.ndarray,
    max_width: int = PREVIEW_MAX_WIDTH,
    max_height: int = PREVIEW_MAX_HEIGHT,
) -> np.ndarray:
    """Downscale a live preview before it crosses into the Qt GUI thread."""
    height, width = image.shape[:2]
    scale = min(1.0, max_width / width, max_height / height)
    if scale >= 1.0:
        return np.ascontiguousarray(image)
    resized = cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return np.ascontiguousarray(resized)


class _CameraWorker(QObject):
    frame = pyqtSignal(object)
    connected = pyqtSignal(object)
    status_changed = pyqtSignal(object)
    exposure_applied = pyqtSignal(float)
    exposure_failed = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self._running = False
        self._exposure_commands: queue.Queue[float] = queue.Queue()
        self._preview_slot_available = threading.Event()
        self._preview_slot_available.set()

    def enqueue_exposure(self, exposure_time_us: float) -> None:
        self._exposure_commands.put(float(exposure_time_us))

    def take_preview_slot(self) -> bool:
        """Allow at most one queued full-size preview frame in the GUI thread."""
        if not self._preview_slot_available.is_set():
            return False
        self._preview_slot_available.clear()
        return True

    def acknowledge_preview(self) -> None:
        self._preview_slot_available.set()

    @pyqtSlot()
    def run(self) -> None:
        harvester = None
        acquisition = None
        exposure_node: Any = None
        exposure_auto_node: Any = None
        original_exposure: float | None = None
        original_exposure_auto: str | None = None
        try:
            from harvesters.core import Harvester

            harvester = Harvester()
            harvester.add_file(self.settings.cti_path)
            harvester.update()
            selected = None
            for info in harvester.device_info_list:
                serial = str(getattr(info, "serial_number", ""))
                ip = str(getattr(info, "property_dict", {}).get("ip_address", ""))
                if self.settings.camera_serial and serial == self.settings.camera_serial:
                    selected = info
                    break
                if self.settings.camera_ip and (
                    ip == self.settings.camera_ip or self.settings.camera_ip in repr(info)
                ):
                    selected = info
            if selected is None and len(harvester.device_info_list) == 1:
                selected = harvester.device_info_list[0]
            if selected is None:
                raise RuntimeError("Baumer camera could not be identified uniquely")
            acquisition = harvester.create({"serial_number": selected.serial_number})
            node_map = acquisition.remote_device.node_map
            exposure_node = _find_node(node_map, "ExposureTime", "ExposureTimeAbs")
            exposure_auto_node = _find_node(node_map, "ExposureAuto")
            original_exposure = _node_number(exposure_node, "value")
            exposure_auto = str(_node_value(node_map, "ExposureAuto", "–"))
            original_exposure_auto = exposure_auto
            camera_fps_raw = _node_value(
                node_map,
                "ResultingFrameRate",
                _node_value(node_map, "AcquisitionFrameRate", None),
            )
            camera_fps = float(camera_fps_raw) if camera_fps_raw is not None else None
            status = CameraStatus(
                model=str(getattr(selected, "model", "–")),
                serial_number=str(getattr(selected, "serial_number", "–")),
                ip_address=self.settings.camera_ip,
                width=int(_node_value(node_map, "Width", 0)),
                height=int(_node_value(node_map, "Height", 0)),
                pixel_format=str(_node_value(node_map, "PixelFormat", "–")),
                camera_fps=camera_fps,
                exposure_time_us=original_exposure,
                exposure_min_us=_node_number(exposure_node, "min"),
                exposure_max_us=_node_number(exposure_node, "max"),
                exposure_writable=exposure_node is not None
                and (_node_writable(exposure_node) or _node_writable(exposure_auto_node)),
                exposure_auto=exposure_auto,
            )
            acquisition.start()
            self.connected.emit(status)
            self._running = True
            preview_interval = 1.0 / max(1.0, float(self.settings.preview_fps))
            next_preview = 0.0
            fps_window_start = time.monotonic()
            stream_frames = 0
            preview_frames = 0
            while self._running and not QThread.currentThread().isInterruptionRequested():
                try:
                    while True:
                        requested_exposure = self._exposure_commands.get_nowait()
                        if exposure_node is None or not status.exposure_writable:
                            raise RuntimeError(
                                "The camera does not expose a writable exposure time"
                            )
                        current_auto = str(
                            getattr(exposure_auto_node, "value", status.exposure_auto)
                        )
                        if current_auto.lower() not in {"off", "–", "none"}:
                            if exposure_auto_node is None or not _node_writable(exposure_auto_node):
                                raise RuntimeError("ExposureAuto is active and cannot be disabled")
                            exposure_auto_node.value = "Off"
                        minimum = status.exposure_min_us or requested_exposure
                        maximum = status.exposure_max_us or requested_exposure
                        requested_exposure = max(minimum, min(maximum, requested_exposure))
                        if not _node_writable(exposure_node):
                            raise RuntimeError("ExposureTime is not writable")
                        exposure_node.value = requested_exposure
                        applied = float(exposure_node.value)
                        status.exposure_time_us = applied
                        status.exposure_auto = str(
                            getattr(exposure_auto_node, "value", status.exposure_auto)
                        )
                        self.status_changed.emit(status)
                        self.exposure_applied.emit(applied)
                except queue.Empty:
                    pass
                except Exception as exc:
                    self.exposure_failed.emit(str(exc) or type(exc).__name__)

                fetch_timeout = camera_fetch_timeout_seconds(status.exposure_time_us)
                with acquisition.fetch(timeout=fetch_timeout) as buffer:
                    now = time.monotonic()
                    stream_frames += 1
                    if now < next_preview or not self.take_preview_slot():
                        # Fetching still returns the GenTL buffer immediately, so the
                        # camera stream is drained without flooding Qt with old frames.
                        component = None
                    else:
                        try:
                            component = buffer.payload.components[0]
                            image = convert_to_rgb(
                                component.data,
                                int(component.width),
                                int(component.height),
                                str(component.data_format),
                            )
                            image = resize_preview(image)
                            self.frame.emit(
                                CameraFrame(image, str(component.data_format), time.time())
                            )
                            preview_frames += 1
                            next_preview = advance_frame_deadline(
                                next_preview, preview_interval, now
                            )
                        except Exception:
                            self.acknowledge_preview()
                            raise
                elapsed = now - fps_window_start
                if elapsed >= 1.0:
                    status.stream_fps = stream_frames / elapsed
                    status.preview_fps = preview_frames / elapsed
                    camera_fps_raw = _node_value(
                        node_map,
                        "ResultingFrameRate",
                        _node_value(node_map, "AcquisitionFrameRate", None),
                    )
                    status.camera_fps = (
                        float(camera_fps_raw) if camera_fps_raw is not None else None
                    )
                    status.exposure_time_us = _node_number(exposure_node, "value")
                    status.exposure_auto = str(
                        getattr(exposure_auto_node, "value", status.exposure_auto)
                    )
                    self.status_changed.emit(status)
                    fps_window_start = now
                    stream_frames = 0
                    preview_frames = 0
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        finally:
            if acquisition is not None:
                if exposure_node is not None and original_exposure is not None:
                    try:
                        current_auto = str(
                            getattr(exposure_auto_node, "value", original_exposure_auto)
                        )
                        if (
                            current_auto.lower() not in {"off", "–", "none"}
                            and exposure_auto_node is not None
                            and _node_writable(exposure_auto_node)
                        ):
                            exposure_auto_node.value = "Off"
                        exposure_node.value = original_exposure
                        if exposure_auto_node is not None and original_exposure_auto is not None:
                            exposure_auto_node.value = original_exposure_auto
                    except Exception:
                        pass
                try:
                    acquisition.stop()
                except Exception:
                    pass
                try:
                    acquisition.destroy()
                except Exception:
                    pass
            if harvester is not None:
                try:
                    harvester.reset()
                except Exception:
                    pass
            self.finished.emit()

    @pyqtSlot()
    def stop(self) -> None:
        self._running = False


class CameraAdapter(DeviceAdapter):
    frame_ready = pyqtSignal(object)
    status_changed = pyqtSignal(object)
    exposure_applied = pyqtSignal(float)
    exposure_failed = pyqtSignal(str)
    _stop_requested = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__("Baumer camera")
        self.settings = settings
        self.thread: QThread | None = None
        self.worker: _CameraWorker | None = None
        self._disconnect_requested = False
        self.status = CameraStatus()

    def connect_device(self) -> None:
        if self.thread and self.thread.isRunning():
            return
        self._disconnect_requested = False
        self._set_state(ConnectionState.CONNECTING)
        self.thread = QThread(self)
        worker = _CameraWorker(self.settings)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        self._stop_requested.connect(worker.stop)
        worker.frame.connect(self._forward_frame)
        worker.connected.connect(self._connected)
        worker.status_changed.connect(self._status_changed)
        worker.exposure_applied.connect(self.exposure_applied)
        worker.exposure_failed.connect(self.exposure_failed)
        worker.error.connect(self._emit_error)
        worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def _connected(self, status: CameraStatus) -> None:
        self.status = status
        self._set_state(ConnectionState.CONNECTED, status.serial_number)
        self.status_changed.emit(status)

    @pyqtSlot(object)
    def _forward_frame(self, frame: object) -> None:
        try:
            self.frame_ready.emit(frame)
        finally:
            worker = self.worker
            if worker is not None:
                worker.acknowledge_preview()

    def _status_changed(self, status: CameraStatus) -> None:
        self.status = status
        self.status_changed.emit(status)

    def set_exposure_time(self, exposure_time_us: float) -> bool:
        if self.worker is None or self.state is not ConnectionState.CONNECTED:
            self.exposure_failed.emit("Exposure can only be changed while the camera is connected")
            return False
        if not self.status.exposure_writable:
            self.exposure_failed.emit("The camera exposure time is not writable")
            return False
        self.worker.enqueue_exposure(exposure_time_us)
        return True

    def disconnect_device(self) -> None:
        self._disconnect_requested = True
        if self.thread:
            self.thread.requestInterruption()
        if self.worker:
            self.worker.stop()

    def _thread_finished(self) -> None:
        self.worker = None
        if self._disconnect_requested or self.state is ConnectionState.CONNECTED:
            self._set_state(ConnectionState.DISCONNECTED, "Disconnected by operator")

    def shutdown(self) -> None:
        self.disconnect_device()
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
