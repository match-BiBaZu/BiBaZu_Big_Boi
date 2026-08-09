from __future__ import annotations

import numpy as np

from bibazu_reorientation.hardware.camera import advance_frame_deadline, convert_to_rgb


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
