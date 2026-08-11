from __future__ import annotations

import numpy as np

from bibazu_reorientation.hardware.camera import (
    _CameraWorker,
    advance_frame_deadline,
    camera_fetch_timeout_seconds,
    convert_to_rgb,
    resize_preview,
)
from bibazu_reorientation.settings import AppSettings


def test_mono12_is_scaled_to_rgb8() -> None:
    source = np.array([[0, 4095], [2048, 1024]], dtype=np.uint16)
    result = convert_to_rgb(source, 2, 2, "Mono12")
    assert result.shape == (2, 2, 3)
    assert result.dtype == np.uint8
    assert result[0, 1, 0] == 255
    assert np.array_equal(result[..., 0], result[..., 1])


def test_bgr_is_converted_to_rgb() -> None:
    source = np.array([[[10, 20, 30]]], dtype=np.uint8)
    result = convert_to_rgb(source, 1, 1, "BGR8")
    assert result.tolist() == [[[30, 20, 10]]]


def test_preview_deadline_skips_missed_frames_without_backlog() -> None:
    assert np.isclose(advance_frame_deadline(1.0, 0.1, 1.35), 1.4)
    assert np.isclose(advance_frame_deadline(0.0, 0.1, 5.0), 5.1)


def test_fetch_timeout_includes_long_exposure() -> None:
    assert camera_fetch_timeout_seconds(None) == 1.0
    assert camera_fetch_timeout_seconds(100_000) == 1.0
    assert np.isclose(camera_fetch_timeout_seconds(2_000_000), 2.5)


def test_preview_backpressure_allows_only_one_outstanding_frame() -> None:
    worker = _CameraWorker(AppSettings())

    assert worker.take_preview_slot()
    assert not worker.take_preview_slot()
    worker.acknowledge_preview()
    assert worker.take_preview_slot()


def test_large_preview_is_downscaled_before_entering_qt() -> None:
    source = np.zeros((3000, 4000, 3), dtype=np.uint8)

    result = resize_preview(source)

    assert result.shape == (720, 960, 3)
    assert result.flags.c_contiguous
