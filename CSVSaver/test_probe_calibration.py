"""Calibration behaviour with synthetic position/force data; no device connections."""
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from probe_calibration import (
    MASK, BarrierHistory, EdgeCapture, MeasurementError, PositionTimeline, ProbeConfig,
    ProbeRecording, barrier_row, force_peak, persist_spacing, smooth_force, statistics_for,
)
from probe_hardware import ForceStream, ProbeRunner, ProbePlc
from probe_vendor.force_sources import ForceSample, parse_quantumx_message
from probe_vendor.platform_calibration import PlatformCalibration


def force_sample(sequence=1, stamp=1_000_000_000, first=0.1, second=0.2):
    return ForceSample(source="quantumx", sequence=sequence, timestamp_utc_ns=stamp,
                       force_1_n=first, force_2_n=second, force_total_n=first + second,
                       force_1_raw_n=first, force_2_raw_n=second, force_total_raw_n=first + second,
                       status="ok", channel_1_status="ok", channel_2_status="ok")


def profile():
    return PlatformCalibration("test", "TAL221", "2026-10-18", "192.168.10.20",
                               ({"logical_name": "force_1", "mx_channel": 3}, {"logical_name": "force_2", "mx_channel": 4}),
                               9.80665, 2.0, 0.0, 0.0, 1, 1, 3)


def ring(newest=0, clock=1000, events=()):
    result = [0] * 771
    result[:3] = [1, newest, clock]
    for sequence, stamp, sensor, clear in events:
        slot = 3 + (sequence % 256) * 3
        result[slot:slot + 3] = [sequence, stamp, sensor * 2 + int(clear)]
    return result


class PositionAndEdgesTests(unittest.TestCase):
    def test_interpolates_actual_positions_and_bounds_uncertainty(self):
        timeline = PositionTimeline()
        timeline.add(1_000_000_000, 10, 10_000_000)
        timeline.add(1_100_000_000, 10.3, 20_000_000)
        position, uncertainty = timeline.at(1_050_000_000)
        self.assertAlmostEqual(position, 10.15)
        self.assertAlmostEqual(uncertainty, 0.06)
        for stamp in (999_999_999, 1_100_000_001):
            with self.assertRaises(MeasurementError):
                timeline.at(stamp)

    def test_position_gaps_and_nonmonotonic_times_fail(self):
        timeline = PositionTimeline()
        timeline.add(1, 0)
        timeline.add(400_000_001, 1)
        with self.assertRaises(MeasurementError):
            timeline.at(200_000_000)
        with self.assertRaises(MeasurementError):
            timeline.add(1, 1)

    def test_falling_edge_is_on_and_reverse_uses_same_centres(self):
        timeline = PositionTimeline(max_gap_s=1)
        for i in range(11):
            timeline.add(i * 100_000_000, i)
        for direction, positions in (("forward", [(1, False, 1), (1, True, 3), (2, False, 6), (2, True, 8)]),
                                     ("backward", [(2, False, 8), (2, True, 6), (1, False, 3), (1, True, 1)])):
            capture = EdgeCapture((1, 2))
            for sensor, clear, position in positions:
                capture.add({"sensor": sensor, "clear": clear, "time_ns": position * 100_000_000,
                             "time_uncertainty_s": 0.001})
            row = barrier_row(1, direction, capture.positioned(timeline), 1, 2, 2)
            self.assertEqual(row["distance_mm"], 5)
            self.assertEqual(row["centres_mm"], {1: 2, 2: 7})

    def test_extra_or_missing_flanks_fail(self):
        capture = EdgeCapture((1, 2))
        with self.assertRaises(MeasurementError):
            capture.add({"sensor": 1, "clear": True})
        capture.add({"sensor": 1, "clear": False})
        with self.assertRaises(MeasurementError):
            capture.add({"sensor": 1, "clear": False})
        with self.assertRaises(MeasurementError):
            capture.positioned(PositionTimeline())


class HistoryTests(unittest.TestCase):
    def test_only_new_events_are_replayed_with_source_time(self):
        history = BarrierHistory()
        self.assertEqual(history.consume(ring(3), 1_000_000_000), [])
        events = history.consume(ring(5, 1100, [(4, 1050, 1, False), (5, 1080, 1, True)]), 2_000_000_000)
        self.assertEqual([e["clear"] for e in events], [False, True])
        self.assertEqual(events[0]["time_ns"], 1_950_000_000)
        self.assertEqual(history.consume(ring(5, 1101), 2_001_000_000), [])

    def test_clock_and_sequence_wraparound(self):
        history = BarrierHistory()
        history.consume(ring(MASK, MASK - 10), 1_000_000_000)
        events = history.consume(ring(0, 5, [(0, MASK - 1, 8, False)]), 1_016_000_000)
        self.assertEqual(events[0]["time_ns"], 1_009_000_000)

    def test_overflow_restart_and_incoherent_slot_fail(self):
        for next_ring in (ring(257, 1100), ring(1, 999), ring(1, 1100)):
            history = BarrierHistory()
            history.consume(ring(), 1_000_000_000)
            with self.assertRaises(MeasurementError):
                history.consume(next_ring, 1_100_000_000)


class ForceAndRecordingTests(unittest.TestCase):
    @staticmethod
    def curve(reverse=False, sign=1, centre=30):
        positions = [i / 10 for i in range(601)]
        if reverse:
            positions.reverse()
        return [{"position_mm": x, "time_ns": i * 10_000_000,
                 "force_1_n": sign * 0.06 * math.exp(-((x-centre)/2)**2),
                 "force_2_n": sign * 0.04 * math.exp(-((x-centre)/2)**2),
                 "force_total_n": sign * 0.1 * math.exp(-((x-centre)/2)**2)} for i, x in enumerate(positions)]

    def test_centred_filter_and_signed_peaks_both_directions(self):
        positions = []
        for reverse in (False, True):
            peaks, smoothed = force_peak(self.curve(reverse, -1), 0.0001)
            self.assertLess(abs(peaks["force_total_n"]["position_mm"] - 30), 0.06)
            self.assertLess(peaks["force_total_n"]["force_n"], 0)
            self.assertEqual(len(smoothed), 582)
            positions.append(peaks["force_total_n"]["position_mm"])
        self.assertAlmostEqual(sum(positions) / 2, 30)
        self.assertAlmostEqual(smooth_force(self.curve())[0]["position_mm"], 0.95)

    def test_weak_missing_and_border_peak_fail(self):
        for samples, noise in ((self.curve(), 0.1), (self.curve()[:20], 0), (self.curve(centre=0), 0)):
            with self.assertRaises(MeasurementError):
                force_peak(samples, noise)

    def test_profile_applies_to_raw_and_mean_without_double_tare(self):
        stream = ForceStream()
        stream.profile = profile().with_tare(0.1, 0.2)
        result = stream.calibrated(force_sample(first=0.2, second=0.3))
        self.assertAlmostEqual(result.force_total_raw_n, 0.4)

    def test_force_status_stale_overrange_and_limit(self):
        stream = ForceStream()
        with self.assertRaises(MeasurementError):
            stream.check(1)
        stream.receive(force_sample())
        self.assertTrue(stream.check(1).valid)
        stream.receive(force_sample(first=1.1))
        with self.assertRaises(MeasurementError):
            stream.check(1)
        stream.receive(replace(force_sample(), status="overrange"))
        with self.assertRaises(MeasurementError):
            stream.check(1)

    def test_quantumx_protocol_requires_two_valid_channels_and_consistent_sum(self):
        payload = dict(schema_version=1, sequence=1, timestamp_utc_ns=1, status="ok",
                       channel_1_status="ok", channel_2_status="ok",
                       force_1_n=-0.1, force_2_n=-0.2, force_total_n=-0.3)
        self.assertTrue(parse_quantumx_message(json.dumps(payload)).valid)
        for change in ({"force_total_n": 0.3}, {"channel_2_status": "overrange"}, {"force_1_n": None}):
            with self.assertRaises(ValueError):
                parse_quantumx_message(json.dumps({**payload, **change}))

    def test_force_mapping_rejects_duplicate_skipped_and_future_data(self):
        import time
        for kind in ("duplicate", "skipped", "future"):
            with self.subTest(kind=kind):
                stream = ForceStream()
                runner = ProbeRunner(Mock(), Mock(), stream, Mock())
                stamp = time.monotonic_ns()
                runner.timeline.add(stamp - 100_000_000, 0)
                runner.timeline.add(stamp + 100_000_000, 1)
                runner.window_start_ns = stamp - 100_000_000
                stream.receive(force_sample(1, stamp + runner.utc_offset_ns))
                runner._forces()
                self.assertEqual(len(runner.force_rows), 1)
                sequence = 1 if kind == "duplicate" else 3 if kind == "skipped" else 2
                next_stamp = stamp + (600_000_000 if kind == "future" else 1_000_000)
                stream.receive(force_sample(sequence, next_stamp + runner.utc_offset_ns))
                with self.assertRaises(MeasurementError):
                    runner._forces()
    def test_five_cycles_statistics_and_partial_recording(self):
        rows = [{"cycle": cycle, "direction": direction, "distance_mm": distance}
                for cycle in range(1, 6) for direction, distance in (("forward", 40.1), ("backward", 39.9))]
        summary = statistics_for(rows)
        self.assertEqual(summary["count"], 10)
        self.assertAlmostEqual(summary["mean_mm"], 40)
        self.assertAlmostEqual(summary["direction_difference_mm"], 0.2)
        with tempfile.TemporaryDirectory() as directory:
            recording = ProbeRecording(directory, ProbeConfig())
            for row in rows:
                recording.add(row)
            recording.finish()
            self.assertTrue(json.loads((recording.path / "session.json").read_text())["apply_allowed"])
            partial = ProbeRecording(directory, ProbeConfig())
            partial.add(rows[0])
            partial.raw(kind="edge", sensor=1, clear=False)
            partial.finish("Abgebrochen")
            saved = json.loads((partial.path / "session.json").read_text())
            self.assertFalse(saved["apply_allowed"])
            self.assertEqual(len(saved["rows"]), 1)
            self.assertIn("edge", (partial.path / "raw.csv").read_text())

    def test_spacing_merge_and_readback_failure_preserve_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"version": 1, "unrelated": 42}))
            plc = Mock()
            plc.read_spacing.return_value = 40.123
            persist_spacing(path, "sensor_spacing_12_mm", 40.123, plc)
            self.assertEqual(json.loads(path.read_text())["unrelated"], 42)
            before = path.read_text()
            plc.read_spacing.return_value = 41
            with self.assertRaises(MeasurementError):
                persist_spacing(path, "sensor_spacing_12_mm", 40.123, plc)
            self.assertEqual(path.read_text(), before)

    def test_nozzle_requires_sufficient_open_duration(self):
        with self.assertRaises(MeasurementError):
            ProbeConfig(mode="nozzle").validate()
        ProbeConfig(mode="nozzle", max_open_s=100).validate()
        for config in (ProbeConfig(travel_mm=76), ProbeConfig(first_sensor=2, second_sensor=1),
                       ProbeConfig(speed_mm_s=1.1), ProbeConfig(mm_per_step=float("nan"))):
            with self.assertRaises(MeasurementError):
                config.validate()

    def test_nozzle_both_distances_use_return_on_reference(self):
        runner = ProbeRunner(Mock(), Mock(), ForceStream(), Mock())
        runner.recording = Mock()
        reference = {"position_mm": -40, "estimated_uncertainty_mm": 0.02}
        for direction, position in (("forward", -9.8), ("backward", -10.2)):
            runner._add_nozzle_row(1, direction, {"force_total_n": {"position_mm": position}}, [], {}, reference, 100)
        distances = [call.args[0]["distance_mm"] for call in runner.recording.add.call_args_list]
        self.assertAlmostEqual(sum(distances)/2, 30)
        for call in runner.recording.add.call_args_list:
            self.assertEqual(call.args[0]["reference_direction"], "backward")
            self.assertEqual(call.args[0]["reference_edge"], reference)

    def test_abort_and_cleanup_close_air_before_serial_stop(self):
        from threading import Event
        cancel = Event()
        cancel.set()
        axis, plc = Mock(), Mock()
        calls = []
        plc.release.side_effect = lambda: calls.append("air")
        axis.stop.side_effect = lambda: calls.append("axis")
        runner = ProbeRunner(axis, plc, ForceStream(), cancel)
        with self.assertRaises(MeasurementError):
            runner.guard()
        self.assertEqual(runner.stop(), "")
        self.assertEqual(calls, ["air", "axis"])

    def test_plc_optional_symbols_fail_locally_without_legacy_poll_changes(self):
        plc = object.__new__(ProbePlc)
        plc.read = Mock(side_effect=RuntimeError("symbol missing"))
        with self.assertRaisesRegex(MeasurementError, "SPS unterstützt"):
            plc.check()

    def test_nozzle_v2_requirement_rejects_old_plc_before_writes(self):
        plc = object.__new__(ProbePlc)
        plc.read = Mock(return_value={"MAIN.ProbeCalibrationInterfaceVersion": 1,
                                      "MAIN.ProbeCalibrationAckSequence": 7})
        plc.write = Mock()
        self.assertEqual(plc.check()["MAIN.ProbeCalibrationInterfaceVersion"], 1)
        with self.assertRaisesRegex(MeasurementError, "Version 2"):
            plc.command(1, ProbeConfig(mode="nozzle"))
        with self.assertRaisesRegex(MeasurementError, "Version 2"):
            plc.command(5)
        plc.write.assert_not_called()
        plc.read.return_value["MAIN.ProbeCalibrationInterfaceVersion"] = 2
        self.assertEqual(plc.check(minimum_version=2)["MAIN.ProbeCalibrationInterfaceVersion"], 2)

    def test_spacing_symbols_cover_all_arrays_and_only_adjacent_pairs(self):
        for array in range(1, 5):
            self.assertEqual(ProbePlc.spacing_symbol(f"sensor_{array*2}_to_array_{array}_spacing_mm"),
                             f"MAIN.GuiSensor{array*2}ToArray{array}SpacingMm")
        with self.assertRaises(MeasurementError):
            ProbePlc.spacing_symbol("sensor_spacing_13_mm")


if __name__ == "__main__":
    unittest.main()
