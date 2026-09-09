"""Regression tests for lost PLC edges and the 15-column raw export."""

import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import ConveyorSetupGUI as setup
import PressureControlGUI as pressure


class EventBuffer:
    """Emulate the published MAIN.LightBarrierEventHistory layout."""

    def __init__(self, sequence=0):
        self.data = [1, sequence, 0] + [0] * (256 * 3)

    def append(self, sensor, timestamp, state):
        sequence = (self.data[1] + 1) & 0xFFFFFFFF
        slot = 3 + (sequence % 256) * 3
        self.data[slot:slot + 3] = [sequence, timestamp, sensor * 2 + int(state)]
        self.data[1:3] = [sequence, timestamp]

    def status(self):
        return {
            "light_barrier_event_counts": [0] * 8,
            "light_barrier_event_times_ms": [0] * 8,
            "light_barriers": [False] * 8,
            "light_barrier_event_history": list(self.data),
        }


class CollectorTests(unittest.TestCase):
    def test_legacy_preserves_selected_edge_when_only_opposite_edge_was_lost(self):
        for selected in (False, True):
            with self.subTest(selected=selected):
                collector = setup.BarrierRunCollector(selected)
                collector.start([10] * 8)
                # OFF+ON (or ON+OFF) happened at every sensor between polls.
                completed, missed = collector.process({
                    "light_barrier_event_counts": [12] * 8,
                    "light_barrier_event_times_ms": list(range(100, 900, 100)),
                    "light_barriers": [selected] * 8,
                    "plc_event_clock_ms": 900,
                })
                self.assertFalse(missed)
                self.assertEqual(completed, [tuple(range(100, 900, 100))])

    def test_legacy_selected_edge_loss_discards_ambiguous_entire_snapshot(self):
        collector = setup.BarrierRunCollector(True)
        collector.start([10] * 8)
        collector.active_runs = [[50]]
        completed, missed = collector.process({
            "light_barrier_event_counts": [11, 12, 10, 10, 10, 10, 10, 10],
            "light_barrier_event_times_ms": [100, 200, 0, 0, 0, 0, 0, 0],
            "light_barriers": [True, False, False, False, False, False, False, False],
        })
        self.assertTrue(missed)
        self.assertEqual(collector.missed_sensors, [2])
        self.assertEqual(completed, [])
        self.assertEqual(collector.active_runs, [])

    def test_buffer_recovers_multiple_overlapping_parts_and_both_edges(self):
        for selected in (True, False):
            with self.subTest(selected=selected):
                buffer = EventBuffer()
                collector = setup.BarrierRunCollector(selected)
                collector.start([0] * 8, buffer.data)
                events = []
                expected = []
                for part in range(4):
                    expected.append(tuple(
                        1000 + part * 50 + sensor * 100 + (0 if selected else 20)
                        for sensor in range(1, 9)
                    ))
                    for sensor in range(1, 9):
                        timestamp = 1000 + part * 50 + sensor * 100
                        events.extend([(timestamp, sensor, True),
                                       (timestamp + 20, sensor, False)])
                for timestamp, sensor, state in sorted(events):
                    buffer.append(sensor, timestamp, state)
                completed, missed = collector.process(buffer.status())
                self.assertFalse(missed)
                self.assertEqual(completed, expected)
                self.assertEqual(collector.process(buffer.status()), ([], False))

    def test_buffer_sequence_and_timestamp_wrap(self):
        buffer = EventBuffer(0xFFFFFFFC)
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8, buffer.data)
        expected = tuple((0xFFFFFF00 + index * 100) & 0xFFFFFFFF for index in range(8))
        for sensor, timestamp in enumerate(expected, 1):
            buffer.append(sensor, timestamp, True)
        self.assertEqual(collector.process(buffer.status()), ([expected], False))

    def test_buffer_overflow_discards_partial_runs_then_recovers(self):
        buffer = EventBuffer()
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8, buffer.data)
        buffer.append(1, 100, True)
        collector.process(buffer.status())
        for index in range(257):
            buffer.append(2, 200 + index, bool(index % 2))
        self.assertEqual(collector.process(buffer.status()), ([], True))
        self.assertEqual(collector.active_runs, [])
        for sensor in range(1, 9):
            buffer.append(sensor, 1000 + sensor * 100, True)
        self.assertEqual(collector.process(buffer.status()),
                         ([tuple(range(1100, 1900, 100))], False))

    def test_buffer_accepts_exactly_256_events(self):
        buffer = EventBuffer()
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8, buffer.data)
        for index in range(256):
            buffer.append(8, index, False)
        self.assertEqual(collector.process(buffer.status()), ([], False))
        self.assertEqual(collector.history_sequence, 256)

    def test_buffer_counter_reset_is_reported(self):
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8, EventBuffer(100).data)
        self.assertEqual(collector.process(EventBuffer().status()), ([], True))

    def test_inconsistent_record_retries_without_advancing_or_partial_mutation(self):
        buffer = EventBuffer()
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8, buffer.data)
        buffer.append(1, 100, True)
        buffer.append(2, 200, True)
        broken = buffer.status()
        broken["light_barrier_event_history"][9] = 123
        self.assertEqual(collector.process(broken), ([], False))
        self.assertEqual(collector.history_sequence, 0)
        self.assertEqual(collector.active_runs, [])
        collector.process(buffer.status())
        self.assertEqual(collector.active_runs, [[100, 200]])

    def test_legacy_does_not_join_events_in_sensor_order_against_time(self):
        collector = setup.BarrierRunCollector(True)
        collector.start([0] * 8)
        collector.process({
            "light_barrier_event_counts": [1] * 8,
            "light_barrier_event_times_ms": [900, 200, 300, 400, 500, 600, 700, 800],
            "light_barriers": [True] * 8,
            "plc_event_clock_ms": 1000,
        })
        self.assertEqual(collector.active_runs, [[900]])


class HistoryReadTests(unittest.TestCase):
    def test_optional_buffer_and_real_errors(self):
        worker = pressure.AdsWorker()
        data = EventBuffer().data
        with patch.object(worker, "read_values", return_value={
            "MAIN.LightBarrierEventHistory": data,
        }):
            self.assertEqual(worker.read_barrier_event_history(), data)
            self.assertTrue(worker.barrier_history_available)
        with patch.object(worker, "read_values", side_effect=pressure.pyads.ADSError(1861)):
            with self.assertRaises(pressure.pyads.ADSError):
                worker.read_barrier_event_history()
        self.assertTrue(worker.barrier_history_available)

    def test_malformed_buffer_does_not_silently_switch_to_legacy(self):
        worker = pressure.AdsWorker()
        for data in ([], [0] * 771):
            with patch.object(worker, "read_values", return_value={
                "MAIN.LightBarrierEventHistory": data,
            }):
                with self.assertRaises(ValueError):
                    worker.read_barrier_event_history()


class RawExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(pressure.AdsController, "start"):
            self.window = setup.ConveyorSetupWindow()
        self.addCleanup(self.window.close)

    def add_samples(self):
        for index in range(2):
            timestamps = tuple(1000 + index * 2000 + sensor * 37 for sensor in range(8))
            sample = {
                **setup.analyze_barrier_run(timestamps, (40.0,) * 7, 50.0),
                "event_times_ms": timestamps,
                "timestamp": "2026-09-08T18:00:00.000",
                "part_number": index + 1,
            }
            # Flagged rows must be exported as well.
            sample["consistent"] = index == 0
            self.window.consistency_samples.append(sample)
            self.window._append_consistency_table_row(sample)
        self.window._update_consistency_summary()

    def test_gui_defaults_to_single_part_and_logs_first_edges_from_buffer(self):
        window = self.window
        self.assertTrue(window.consistency_single_part.isChecked())
        buffer = EventBuffer()
        window.latest_status = buffer.status()
        window.connected = True
        window.have_setup_status = True
        with patch.object(window, "_append_consistency_log"):
            window._start_consistency_monitor()
            self.assertTrue(window.consistency_collector.single_part)
            self.assertEqual(window.consistency_collector.maximum_traversal_ms, 10_000)
            self.assertFalse(window.consistency_collector.edge_state)
            self.assertFalse(window.consistency_edge.currentData())
            self.assertFalse(window.consistency_single_part.isEnabled())
            self.assertFalse(window.consistency_edge.isEnabled())
            timestamps = [228071, 228237, 228630, 228704, 229001, 229055, 229249, 229298]
            events = [(timestamp, sensor) for sensor, timestamp in enumerate(timestamps, 1)]
            events += [(228099, 1), (228209, 1), (228254, 2), (228339, 2),
                       (228692, 3), (228714, 4)]
            for timestamp, sensor in sorted(events):
                buffer.append(sensor, timestamp, False)
            window._process_consistency_monitor(buffer.status())
            self.assertEqual(window.consistency_samples, [])
            buffer.append(8, timestamps[-1] + 50, True)
            window._process_consistency_monitor(buffer.status())
        self.assertEqual(len(window.consistency_samples), 1)
        self.assertEqual(window.consistency_samples[0]["event_times_ms"], tuple(timestamps))
        self.assertEqual(window.consistency_samples[0]["edge"], "OFF")
        self.assertIn("6 repeated edges ignored", window.consistency_acquisition_label.text())
        window._stop_consistency_monitor()
        self.assertTrue(window.consistency_single_part.isEnabled())

    def test_gui_passes_time_limits_and_can_select_overlapping_parts(self):
        window = self.window
        window.latest_status = EventBuffer().status()
        window.consistency_maximum_traversal.setValue(20.0)
        window._start_consistency_monitor()
        self.assertEqual(window.consistency_collector.maximum_traversal_ms, 20_000)
        window._stop_consistency_monitor()
        window.consistency_single_part.setChecked(False)
        self.assertFalse(window.consistency_maximum_traversal.isEnabled())
        self.assertTrue(window.consistency_edge.isEnabled())
        window.consistency_edge.setCurrentIndex(window.consistency_edge.findData(True))
        window._start_consistency_monitor()
        self.assertFalse(window.consistency_collector.single_part)
        self.assertTrue(window.consistency_collector.edge_state)

    def test_start_logging_during_held_plot_waits_for_that_parts_clearance(self):
        window = self.window
        buffer = EventBuffer()
        window.latest_status = buffer.status()
        window.light_barrier_plot.trace.part_active = True
        window.light_barrier_plot.trace.states = (False,) * 8
        with patch.object(window, "_append_consistency_log"):
            window._start_consistency_monitor()
            self.assertTrue(window.consistency_collector.single_part_locked)
            # Repeated LB1 from the part already in the plot is not a new part.
            buffer.append(1, 100, False)
            buffer.append(8, 200, True)
            window._process_consistency_monitor(buffer.status())
            self.assertEqual(window.consistency_samples, [])
            self.assertFalse(window.consistency_collector.single_part_locked)
            expected = tuple(range(1000, 1800, 100))
            for sensor, timestamp in enumerate(expected, 1):
                buffer.append(sensor, timestamp, False)
            buffer.append(8, 1800, True)
            window._process_consistency_monitor(buffer.status())
        self.assertEqual(len(window.consistency_samples), 1)
        self.assertEqual(window.consistency_samples[0]["event_times_ms"], expected)

    def test_exports_all_rows_with_exactly_15_unrounded_numeric_columns(self):
        self.assertFalse(self.window.consistency_export_button.isEnabled())
        self.add_samples()
        self.assertTrue(self.window.consistency_export_button.isEnabled())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            with patch.object(setup.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                self.window.consistency_export_button.click()
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], setup.CONSISTENCY_RAW_HEADER)
        for row, sample in zip(rows[1:], self.window.consistency_samples):
            self.assertEqual(len(row), 15)
            self.assertEqual(tuple(map(int, row[:8])), sample["event_times_ms"])
            self.assertEqual(list(map(float, row[8:])), sample["speeds"])
        self.window._clear_consistency_samples()
        self.assertFalse(self.window.consistency_export_button.isEnabled())

    def test_cancel_leaves_samples_intact(self):
        self.add_samples()
        with patch.object(setup.QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window._export_consistency_raw()
        self.assertEqual(len(self.window.consistency_samples), 2)

    def test_write_failure_is_reported_and_keeps_samples(self):
        self.add_samples()
        with patch.object(setup.QFileDialog, "getSaveFileName", return_value=("raw.csv", "")), \
                patch.object(Path, "open", side_effect=PermissionError("file in use")), \
                patch.object(setup.QMessageBox, "warning") as warning:
            self.window._export_consistency_raw()
        warning.assert_called_once()
        self.assertEqual(len(self.window.consistency_samples), 2)


if __name__ == "__main__":
    unittest.main()
