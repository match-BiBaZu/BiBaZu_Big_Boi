import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

from high_speed_calibration import (
    LINE0_RISING_EVENT_ID,
    FramePacket,
    RecordingSession,
    decode_baumer_usb_line_event,
    estimate_camera_event_timestamp_ns,
    estimate_plc_event_host_ns,
    estimate_timing_uncertainty_ms,
    load_recording,
    uint32_elapsed,
    update_movement_evaluation,
)

HAS_IMAGE_DEPENDENCIES = bool(
    importlib.util.find_spec("cv2") and importlib.util.find_spec("numpy")
)


class TimingTests(unittest.TestCase):
    def test_baumer_usb_line_event_decodes_event_id_and_timestamp(self):
        timestamp = 1_287_298_020_290
        payload = (
            b"\x00\x00"
            + LINE0_RISING_EVENT_ID.to_bytes(2, "little")
            + timestamp.to_bytes(8, "little")
        )

        self.assertEqual(decode_baumer_usb_line_event(payload), timestamp)
        self.assertIsNone(decode_baumer_usb_line_event(payload[:8]))
        self.assertIsNone(decode_baumer_usb_line_event(payload, 0x8008))

    def test_uint32_elapsed_handles_plc_clock_wraparound(self):
        self.assertEqual(uint32_elapsed(25, 0xFFFFFFF0), 41)

    def test_plc_event_time_is_mapped_back_from_snapshot_clock(self):
        sampled_ns = 12_000_000_000

        result = estimate_plc_event_host_ns(sampled_ns, 1_250, 1_200)

        self.assertEqual(result, 11_950_000_000)

    def test_camera_clock_mapping_uses_nearby_median_offset(self):
        event_host_ns = 5_000_000_000
        anchors = [
            (camera_ns, camera_ns + 2_000_000 + jitter)
            for camera_ns, jitter in (
                (4_990_000_000, -10_000),
                (4_994_000_000, 20_000),
                (4_998_000_000, 0),
                (5_002_000_000, 10_000),
                (5_006_000_000, -20_000),
            )
        ]

        result = estimate_camera_event_timestamp_ns(anchors, event_host_ns)

        self.assertEqual(result, 4_998_000_000)

    def test_timing_uncertainty_includes_frame_and_ads_resolution(self):
        anchors = [
            (1_000_000_000, 3_000_000_000),
            (1_004_000_000, 3_004_000_000),
            (1_008_000_000, 3_008_000_000),
        ]

        result = estimate_timing_uncertainty_ms(anchors, 4_000_000)

        self.assertAlmostEqual(result, 5.0)


@unittest.skipUnless(HAS_IMAGE_DEPENDENCIES, "OpenCV and NumPy are not installed")
class RecordingWriterTests(unittest.TestCase):
    def _packet(self, index, frame_id=None):
        import numpy as np

        return FramePacket(
            image=np.full((24, 32), index * 20, dtype=np.uint8),
            pixel_format="BayerRG8",
            width=32,
            height=24,
            frame_id=index if frame_id is None else frame_id,
            camera_timestamp_ns=1_000_000_000 + index * 4_000_000,
            host_monotonic_ns=3_000_000_000 + index * 4_000_000,
            wall_time_ns=1_800_000_000_000_000_000 + index * 4_000_000,
        )

    def test_writer_creates_images_metadata_and_upserts_movement_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = RecordingSession(
                root,
                light_barrier=2,
                post_trigger_ms=200,
                camera_info={"model": "VCXU-02C", "serial": "700005072151"},
            )
            session.set_trigger(
                event_count=8,
                event_plc_time_ms=1_000,
                event_host_ns=3_008_000_000,
                ads_roundtrip_ns=2_000_000,
            )
            for index in range(6):
                session.add_frame(self._packet(index))
            session.stop("manual")
            self.assertTrue(session.wait())

            document, frames = load_recording(session.directory)
            self.assertEqual(document["frame_count"], 6)
            self.assertTrue(document["recording_complete"])
            self.assertTrue(document["trigger"]["detected"])
            self.assertEqual(len(frames), 6)
            self.assertTrue((session.directory / "frame_000005.jpg").is_file())
            self.assertAlmostEqual(
                float(frames[2]["relative_to_light_barrier_ms"]), 0.0
            )

            first = update_movement_evaluation(session.directory, 4)
            second = update_movement_evaluation(session.directory, 5)
            self.assertAlmostEqual(first["evaluation"]["delay_ms"], 8.0)
            self.assertAlmostEqual(second["evaluation"]["delay_ms"], 12.0)
            with (root / "calibration_results.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                results = list(csv.DictReader(handle))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["session_id"], session.session_id)

    def test_frame_id_gap_marks_session_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = RecordingSession(
                Path(temporary),
                light_barrier=4,
                post_trigger_ms=200,
                camera_info={},
            )
            session.add_frame(self._packet(0, frame_id=10))
            session.add_frame(self._packet(1, frame_id=12))
            session.stop("manual")
            self.assertTrue(session.wait())

            document, _frames = load_recording(session.directory)
            self.assertFalse(document["recording_complete"])
            self.assertEqual(document["frame_id_gaps"], [[10, 12]])

    def test_auto_stop_is_requested_on_first_frame_at_post_trigger_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = RecordingSession(
                Path(temporary),
                light_barrier=2,
                post_trigger_ms=8,
                camera_info={},
            )
            stops = []
            session.auto_stop_requested.connect(lambda: stops.append(True))
            session.set_trigger(1, 100, 3_000_000_000, 1_000_000)

            session.add_frame(self._packet(0))
            session.add_frame(self._packet(1))
            self.assertEqual(stops, [])
            session.add_frame(self._packet(2))
            session.add_frame(self._packet(3))
            self.assertEqual(stops, [True])
            session.stop("post_trigger_complete")
            self.assertTrue(session.wait())

    def test_hardware_trigger_uses_camera_clock_for_post_trigger_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = RecordingSession(
                Path(temporary),
                light_barrier=2,
                post_trigger_ms=8,
                camera_info={"hardware_trigger_available": True},
            )
            stops = []
            session.auto_stop_requested.connect(lambda: stops.append(True))
            accepted = session.set_hardware_trigger(
                event_camera_ns=1_000_000_000,
                received_host_ns=session.started_monotonic_ns + 1,
            )

            self.assertTrue(accepted)
            session.add_frame(self._packet(0))
            session.add_frame(self._packet(1))
            self.assertEqual(stops, [])
            session.add_frame(self._packet(2))
            self.assertEqual(stops, [True])
            session.stop("post_trigger_complete")
            self.assertTrue(session.wait())

            document, frames = load_recording(session.directory)
            self.assertEqual(document["trigger"]["source"], "camera_line0_rising_edge")
            self.assertEqual(document["trigger"]["event_id"], LINE0_RISING_EVENT_ID)
            self.assertEqual(document["trigger"]["camera_timestamp_ns"], 1_000_000_000)
            self.assertAlmostEqual(
                float(frames[2]["relative_to_light_barrier_ms"]), 8.0
            )


if __name__ == "__main__":
    unittest.main()
