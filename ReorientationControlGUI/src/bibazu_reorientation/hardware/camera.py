from __future__ import annotations

import re
import time
from typing import Any

import cv2
import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from bibazu_reorientation.hardware.base import DeviceAdapter
from bibazu_reorientation.models import CameraFrame, CameraStatus, ConnectionState
from bibazu_reorientation.settings import AppSettings


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
        raise ValueError(f"Gepacktes Pixelformat wird nicht unterstützt: {pixel_format}")
    if fmt.startswith("mono"):
        if image.size != width * height:
            raise ValueError(
                f"Unerwartete Datenmenge für {original_format}: {image.size}"
            )
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
                raise ValueError(
                    f"Unerwartete Datenmenge für {original_format}: {image.size}"
                )
            mosaic = _uint8(image.reshape(height, width), pixel_format)
            return np.ascontiguousarray(cv2.cvtColor(mosaic, code))

    if fmt.startswith("rgb8"):
        return np.ascontiguousarray(image.reshape(height, width, 3).astype(np.uint8))
    if fmt.startswith("bgr8"):
        bgr = image.reshape(height, width, 3).astype(np.uint8)
        return np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    raise ValueError(f"Nicht unterstütztes Pixelformat: {original_format}")


class _CameraWorker(QObject):
    frame = pyqtSignal(object)
    connected = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self._running = False

    @pyqtSlot()
    def run(self) -> None:
        harvester = None
        acquisition = None
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
                raise RuntimeError("Baumer-Kamera nicht eindeutig gefunden")
            acquisition = harvester.create({"serial_number": selected.serial_number})
            acquisition.start()
            self.connected.emit(
                CameraStatus(
                    model=str(getattr(selected, "model", "–")),
                    serial_number=str(getattr(selected, "serial_number", "–")),
                    ip_address=self.settings.camera_ip,
                )
            )
            self._running = True
            while self._running and not QThread.currentThread().isInterruptionRequested():
                with acquisition.fetch(timeout=1.0) as buffer:
                    component = buffer.payload.components[0]
                    image = convert_to_rgb(
                        component.data,
                        int(component.width),
                        int(component.height),
                        str(component.data_format),
                    )
                    self.frame.emit(CameraFrame(image, str(component.data_format), time.time()))
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        finally:
            if acquisition is not None:
                acquisition.stop()
                acquisition.destroy()
            if harvester is not None:
                harvester.reset()
            self.finished.emit()

    @pyqtSlot()
    def stop(self) -> None:
        self._running = False


class CameraAdapter(DeviceAdapter):
    frame_ready = pyqtSignal(object)
    status_changed = pyqtSignal(object)
    _stop_requested = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__("Baumer-Kamera")
        self.settings = settings
        self.thread: QThread | None = None
        self.worker: _CameraWorker | None = None

    def connect_device(self) -> None:
        if self.thread and self.thread.isRunning():
            return
        self._set_state(ConnectionState.CONNECTING)
        self.thread = QThread(self)
        worker = _CameraWorker(self.settings)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        self._stop_requested.connect(worker.stop)
        worker.frame.connect(self.frame_ready)
        worker.connected.connect(self._connected)
        worker.error.connect(self._emit_error)
        worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.start()

    def _connected(self, status: CameraStatus) -> None:
        self._set_state(ConnectionState.CONNECTED, status.serial_number)
        self.status_changed.emit(status)

    def disconnect_device(self) -> None:
        if self.thread:
            self.thread.requestInterruption()
        if self.worker:
            self.worker.stop()

    def shutdown(self) -> None:
        self.disconnect_device()
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
