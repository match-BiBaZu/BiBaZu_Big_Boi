"""Hardware-free checks for live signal acquisition and rendering."""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from light_barrier_plot import BarrierTrace, PlcTriggerTrace, SensorRecording, LightBarrierPlot


class Buffer:
    def __init__(self, clock=0, sequence=0):
        self.data = [1, sequence, clock] + [0] * 768
        self.states = [True] * 8

    def append(self, sensor, timestamp, state):
        sequence = (self.data[1] + 1) & 0xFFFFFFFF
        slot = 3 + (sequence % 256) * 3
        self.data[slot:slot + 3] = [sequence, timestamp, sensor * 2 + int(state)]
        self.data[1:3] = [sequence, timestamp]
        self.states[sensor - 1] = state

    def status(self):
        return {"light_barrier_event_history": list(self.data),
                "light_barriers": list(self.states)}


class TraceTests(unittest.TestCase):
    def test_lb8_freezes_display_until_next_lb1_in_both_modes(self):
        for single_part in (False, True):
            with self.subTest(single_part=single_part):
                trace, buffer = BarrierTrace(single_part=single_part), Buffer()
                trace.process(buffer.status())
                buffer.append(1, 100, False)
                buffer.append(1, 110, True)
                buffer.append(8, 400, False)
                # Release and a delayed poll must not move the visible endpoint.
                buffer.append(8, 450, True)
                buffer.data[2] = 900
                trace.process(buffer.status())
                frozen = tuple(trace.display_points)
                self.assertEqual(trace.display_elapsed_ms, 300)
                self.assertFalse(frozen[-1][1][7])
                self.assertEqual(trace.elapsed_ms, 800)
                self.assertTrue(trace.states[7])
                buffer.append(8, 1000, False)
                buffer.append(8, 1100, True)
                buffer.data[2] = 1500
                trace.process(buffer.status())
                self.assertEqual(tuple(trace.display_points), frozen)
                self.assertEqual(trace.display_elapsed_ms, 300)
                buffer.append(1, 1600, False)
                buffer.data[2] = 1700
                trace.process(buffer.status())
                self.assertIsNone(trace.frozen_elapsed_ms)
                self.assertEqual(trace.display_elapsed_ms, 100)
                self.assertEqual(trace.display_points[0][0], 0)

    def test_polled_lb8_freeze_does_not_stop_timeout_clock(self):
        trace = BarrierTrace(single_part=True, maximum_traversal_ms=1000)
        states = [True] * 8
        trace.process({"light_barriers": states, "plc_event_clock_ms": 0})
        for timestamp, sensor in [(100, 0), (400, 7)]:
            states[sensor] = False
            trace.process({"light_barriers": list(states), "plc_event_clock_ms": timestamp})
        trace.process({"light_barriers": list(states), "plc_event_clock_ms": 2000})
        self.assertEqual(trace.display_elapsed_ms, 300)
        self.assertFalse(trace.display_points[-1][1][7])
        self.assertFalse(trace.part_active)
        self.assertEqual(trace.states, (True,) * 8)

    def test_short_pulses_replayed_and_lb1_falling_replaces_previous_cycle(self):
        trace, buffer = BarrierTrace(), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        buffer.append(2, 120, False)
        buffer.append(2, 125, True)
        buffer.append(1, 150, True)
        trace.process(buffer.status())
        self.assertEqual([p[0] for p in trace.points], [0, 0, 20, 25, 50])
        self.assertFalse(trace.points[2][1][1])
        buffer.append(1, 200, False)
        buffer.append(8, 210, False)
        trace.process(buffer.status())
        self.assertEqual([p[0] for p in trace.points], [0, 0, 10])
        self.assertFalse(trace.points[-1][1][7])
        points = list(trace.points)
        buffer.data[2] = 250
        trace.process(buffer.status())
        self.assertEqual(list(trace.points), points)
        self.assertEqual(trace.elapsed_ms, 50)

    def test_polled_low_does_not_repeatedly_reset(self):
        trace = BarrierTrace()
        for clock, state in [(100, True), (200, False), (300, False)]:
            trace.process({"light_barriers": [state] * 8, "plc_event_clock_ms": clock})
        self.assertEqual(trace.elapsed_ms, 100)
        self.assertEqual(trace.points[0][0], 0)
        self.assertIn("Polled", trace.message)

    def test_clock_and_sequence_wrap(self):
        trace, buffer = BarrierTrace(), Buffer(0xFFFFFFF0, 0xFFFFFFFF)
        trace.process(buffer.status())
        buffer.append(1, 0xFFFFFFF5, False)
        buffer.append(2, 10, False)
        trace.process(buffer.status())
        self.assertEqual(trace.elapsed_ms, 21)
        self.assertEqual(trace.sequence, 1)

    def test_overflow_restarts_without_inventing_edges(self):
        trace, buffer = BarrierTrace(), Buffer()
        trace.process(buffer.status())
        for index in range(257):
            buffer.append(2, index + 1, bool(index % 2))
        trace.process(buffer.status())
        self.assertEqual(len(trace.points), 1)
        self.assertIn("Event gap", trace.message)

    def test_inconsistent_buffer_retries_without_advancing(self):
        trace, buffer = BarrierTrace(), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        bad_status = buffer.status()
        bad_status["light_barrier_event_history"][6] = 999
        trace.process(bad_status)
        self.assertEqual(trace.sequence, 0)
        trace.process(buffer.status())
        self.assertEqual(trace.sequence, 1)
        self.assertFalse(trace.states[0])


class HeldTraceTests(unittest.TestCase):
    def test_first_activation_held_despite_repeated_edges_until_lb8_clears(self):
        trace, buffer = BarrierTrace(single_part=True), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        buffer.append(1, 110, True)
        buffer.append(1, 900, False)  # Beyond the collector's repeat window.
        buffer.append(2, 950, False)
        buffer.append(2, 970, True)
        buffer.append(2, 980, False)
        trace.process(buffer.status())
        self.assertEqual([p[0] for p in trace.points], [0, 0, 850])
        self.assertEqual(trace.states, (False, False) + (True,) * 6)
        for sensor in range(3, 9):
            buffer.append(sensor, 1000 + sensor * 100, False)
        trace.process(buffer.status())
        self.assertEqual(trace.states, (False,) * 8)
        self.assertTrue(trace.part_active)
        buffer.append(8, 1900, True)
        trace.process(buffer.status())
        self.assertEqual(trace.states, (True,) * 8)
        self.assertFalse(trace.part_active)
        self.assertEqual(trace.points[-1][0], 1800)
        self.assertEqual(trace.points[-2][1], (False,) * 8)

    def test_next_part_replaces_plot_and_ignores_events_before_lb1(self):
        trace, buffer = BarrierTrace(single_part=True), Buffer()
        trace.process(buffer.status())
        buffer.append(2, 50, False)
        buffer.append(8, 60, False)
        buffer.append(8, 70, True)
        trace.process(buffer.status())
        self.assertEqual(len(trace.points), 1)
        for sensor, timestamp, state in [
            (1, 100, False), (1, 110, True), (8, 200, False),
            (8, 210, True), (8, 220, False), (8, 230, True),
            (1, 300, False), (3, 320, False),
        ]:
            buffer.append(sensor, timestamp, state)
        trace.process(buffer.status())
        self.assertEqual([p[0] for p in trace.points], [0, 0, 20])
        self.assertEqual(trace.states, (False, True, False) + (True,) * 5)

    def test_polled_signals_use_actual_states_to_detect_edges(self):
        trace = BarrierTrace(single_part=True)
        states = [True] * 8
        trace.process({"light_barriers": states, "plc_event_clock_ms": 0})
        for timestamp, sensor, state in [
            (100, 0, False), (110, 0, True), (120, 0, False),
            (200, 7, False), (210, 7, True),
        ]:
            states[sensor] = state
            trace.process({"light_barriers": list(states), "plc_event_clock_ms": timestamp})
        self.assertEqual([p[0] for p in trace.points], [0, 0, 100, 110])
        self.assertEqual(trace.states, (True,) * 8)

    def test_timeout_releases_latches_and_waits_for_new_lb1(self):
        trace, buffer = BarrierTrace(single_part=True, maximum_traversal_ms=1000), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        trace.process(buffer.status())
        buffer.data[2] = 1101
        trace.process(buffer.status())
        self.assertFalse(trace.part_active)
        self.assertEqual(trace.states, (True,) * 8)
        self.assertIn("Maximum traversal", trace.message)
        buffer.append(2, 1200, False)
        trace.process(buffer.status())
        self.assertEqual(trace.states, (True,) * 8)

    def test_complete_buffered_part_is_replayed_before_timeout_check(self):
        trace, buffer = BarrierTrace(single_part=True, maximum_traversal_ms=1000), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        buffer.append(8, 200, False)
        buffer.append(8, 220, True)
        buffer.data[2] = 5000
        trace.process(buffer.status())
        self.assertEqual(trace.points[-1][0], 120)
        self.assertNotIn("Maximum traversal", trace.message)

    def test_buffer_overflow_clears_held_states(self):
        trace, buffer = BarrierTrace(single_part=True), Buffer()
        trace.process(buffer.status())
        buffer.append(1, 100, False)
        trace.process(buffer.status())
        for index in range(257):
            buffer.append(2, 200 + index, bool(index % 2))
        trace.process(buffer.status())
        self.assertEqual(trace.states, (True,) * 8)
        self.assertFalse(trace.part_active)


class FilteredBuffer(Buffer):
    def __init__(self, clock=0, sequence=0):
        super().__init__(clock, sequence)
        self.states = [False] * 8

    def status(self):
        return {"light_barrier_filtered_event_history": list(self.data),
                "light_barriers": [True] * 8}


class SensorRecordingTests(unittest.TestCase):
    def test_window_aligns_across_clock_rollover(self):
        trace, buffer = SensorRecording(), Buffer(0xFFFFFFF0)
        trace.process(buffer.status())
        buffer.append(1, 0xFFFFFFF5, False)
        buffer.append(1, 2, True)
        buffer.append(8, 10, False)
        trace.process(buffer.status())
        status, points = trace.window(0xFFFFFFF5, 10)
        self.assertEqual(status, "ready")
        self.assertEqual([p[0] for p in points], [0, 0, 13, 21])
        self.assertTrue(points[0][1][0])
        self.assertFalse(points[1][1][0])

    def test_overflow_does_not_invent_missing_recording(self):
        trace, buffer = SensorRecording(), Buffer()
        trace.process(buffer.status())
        for index in range(257):
            buffer.append(2, index + 1, bool(index % 2))
        trace.process(buffer.status())
        self.assertEqual(trace.window(100, 250), ("unavailable", ()))


class PlcTriggerTraceTests(unittest.TestCase):
    def test_exact_plc_pulses_are_replayed_without_another_filter(self):
        trace, buffer = PlcTriggerTrace(single_part=True), FilteredBuffer()
        trace.process(buffer.status())
        for sensor, timestamp, state in [(1, 100, True), (1, 101, False),
                                         (2, 200, True), (2, 201, False),
                                         (2, 250, True), (2, 251, False),
                                         (8, 300, True), (8, 301, False)]:
            buffer.append(sensor, timestamp, state)
        trace.process(buffer.status())
        self.assertEqual([p[0] for p in trace.points], [0, 0, 1, 100, 101, 150, 151, 200, 201])
        self.assertEqual(trace.states, (False,) * 8)
        self.assertEqual(trace.display_elapsed_ms, 200)
        self.assertTrue(trace.display_points[-1][1][7])
        self.assertTrue(trace.feed_available)

    def test_missing_filtered_feed_never_falls_back_to_raw_history(self):
        trace, raw = PlcTriggerTrace(single_part=True), Buffer()
        raw.append(1, 100, False)
        raw.append(8, 200, False)
        trace.process(raw.status())
        self.assertFalse(trace.feed_available)
        self.assertEqual(len(trace.points), 0)
        self.assertIsNone(trace.last_completed_display)
        self.assertIn("unavailable", trace.message)

    def test_clock_and_sequence_wrap(self):
        trace, buffer = PlcTriggerTrace(single_part=True), FilteredBuffer(0xFFFFFFF0, 0xFFFFFFFF)
        trace.process(buffer.status())
        buffer.append(1, 0xFFFFFFF5, True)
        buffer.append(1, 0xFFFFFFF6, False)
        buffer.append(8, 10, True)
        trace.process(buffer.status())
        self.assertEqual(trace.display_elapsed_ms, 21)
        self.assertEqual(trace.sequence, 2)

    def test_inconsistent_buffer_retries_and_overflow_restarts_capture(self):
        trace, buffer = PlcTriggerTrace(single_part=True), FilteredBuffer()
        trace.process(buffer.status())
        buffer.append(1, 100, True)
        broken = buffer.status()
        broken["light_barrier_filtered_event_history"][6] = 999
        trace.process(broken)
        self.assertEqual(trace.sequence, 0)
        trace.process(buffer.status())
        self.assertEqual(trace.sequence, 1)
        for index in range(257):
            buffer.append(2, 200 + index, bool(index % 2))
        trace.process(buffer.status())
        self.assertEqual(len(trace.points), 1)
        self.assertIn("Event gap", trace.message)

    def test_plc_buffer_records_gate_outputs_before_trigger_consumers(self):
        import xml.etree.ElementTree as ET
        source = Path(__file__).parent / "TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/Untitled1/POUs/MAIN.TcPOU"
        root = ET.parse(source).getroot()
        st = root.find(".//ST").text
        for sensor in range(1, 9):
            gate = "PairedFirstBarrierFalling" if sensor % 2 else "PairedSecondBarrierFalling"
            self.assertIn(f"LightBarrierFilteredSignal[{sensor}] := {gate}[{(sensor + 1) // 2}];", st)
        self.assertLess(st.index("LightBarrierFilteredSignal[1] :="),
                        st.index("IF NOT GuiReorientationControlActive AND PairedSecondBarrierFalling[1]"))
        self.assertIn("<> LastLightBarrierFilteredSignal[LightBarrierFilteredSensor]", st)


class PlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_overlay_preserves_suppressed_edges_and_freezes_both_layers(self):
        widget = LightBarrierPlot(live_updates=False, plc_filtered=True)
        self.addCleanup(widget.close)
        raw, filtered = Buffer(), FilteredBuffer()
        widget.process_status({**filtered.status(), **raw.status()})
        for sensor, timestamp, state in [(1, 100, False), (1, 110, True),
                                         (1, 120, False), (1, 130, True),
                                         (2, 200, False), (8, 300, False),
                                         (8, 320, True)]:
            raw.append(sensor, timestamp, state)
        for sensor, timestamp, state in [(1, 100, True), (1, 101, False),
                                         (2, 200, True), (2, 201, False),
                                         (8, 300, True), (8, 301, False)]:
            filtered.append(sensor, timestamp, state)
        widget.process_status({**filtered.status(), **raw.status()})
        self.assertEqual(widget.completed_display[0], 200)
        self.assertEqual([p[0] for p in widget.completed_overlay], [0, 0, 10, 20, 30, 100, 200])
        self.assertEqual([p[0] for p in widget.completed_display[1]], [0, 0, 1, 100, 101, 200])
        self.assertFalse(widget.completed_overlay[-1][1][7])
        self.assertTrue(widget.completed_display[1][-1][1][7])
        frozen = widget.grab().toImage()
        overlay = widget.completed_overlay
        raw.append(1, 400, False)
        filtered.append(1, 400, True)
        widget.process_status({**filtered.status(), **raw.status()})
        self.assertIs(widget.completed_overlay, overlay)
        self.assertEqual(widget.grab().toImage(), frozen)
        widget.set_connected(True)
        self.assertEqual(widget.completed_overlay, ())

    def test_overlay_waits_for_earlier_read_buffer_to_reach_lb8(self):
        widget = LightBarrierPlot(live_updates=False, plc_filtered=True)
        self.addCleanup(widget.close)
        raw, filtered = Buffer(), FilteredBuffer()
        widget.process_status({**filtered.status(), **raw.status()})
        raw.append(1, 100, False)
        raw.data[2] = 299
        filtered.append(1, 100, True)
        filtered.append(1, 101, False)
        filtered.append(8, 300, True)
        widget.process_status({**filtered.status(), **raw.status()})
        self.assertIsNone(widget.completed_display)
        raw.append(8, 300, False)
        widget.process_status({**filtered.status(), **raw.status()})
        self.assertEqual(widget.completed_display[0], 200)
        self.assertEqual(widget.completed_overlay[-1][0], 200)
        self.assertFalse(widget.completed_overlay[-1][1][7])

    def test_missing_sensor_recording_keeps_actual_pulses_with_explanation(self):
        widget = LightBarrierPlot(live_updates=False, plc_filtered=True)
        self.addCleanup(widget.close)
        filtered = FilteredBuffer()
        widget.process_status(filtered.status())
        filtered.append(1, 100, True)
        filtered.append(8, 300, True)
        widget.process_status(filtered.status())
        self.assertEqual(widget.completed_display[0], 200)
        self.assertEqual(widget.completed_overlay, ())
        self.assertIn("sensor recording unavailable", widget.completed_message)

    def test_render_and_connection_lifecycle(self):
        widget = LightBarrierPlot()
        self.addCleanup(widget.close)
        widget.resize(560, 340)
        buffer = Buffer()
        widget.process_status(buffer.status())
        buffer.append(1, 100, False)
        buffer.append(2, 150, False)
        widget.process_status(buffer.status())
        self.assertFalse(widget.grab().isNull())
        widget.set_connected(False)
        self.assertEqual(widget.trace.elapsed_ms, 50)
        self.assertIn("Disconnected", widget.trace.message)
        widget.set_connected(True)
        self.assertIsNone(widget.trace.states)
        self.assertEqual(len(set(widget.COLORS)), 8)

    def test_render_stays_unchanged_after_lb8_release_and_idle_poll(self):
        widget, buffer = LightBarrierPlot(), Buffer()
        self.addCleanup(widget.close)
        widget.resize(560, 340)
        widget.process_status(buffer.status())
        buffer.append(1, 100, False)
        buffer.append(8, 400, False)
        widget.process_status(buffer.status())
        frozen_image = widget.grab().toImage()
        buffer.append(8, 450, True)
        buffer.data[2] = 5000
        widget.process_status(buffer.status())
        self.assertEqual(widget.grab().toImage(), frozen_image)

    def test_plot_is_to_right_of_consistency_table(self):
        import ConveyorSetupGUI as setup
        with patch.object(setup.AdsController, "start"):
            window = setup.ConveyorSetupWindow()
        self.addCleanup(window.close)
        self.assertIs(window.consistency_splitter.widget(0), window.consistency_table)
        self.assertIs(window.consistency_splitter.widget(1), window.light_barrier_plot)
        self.assertTrue(window.light_barrier_plot.trace.single_part)
        self.assertNotIsInstance(window.light_barrier_plot.trace, PlcTriggerTrace)
        window.consistency_single_part.setChecked(False)
        self.assertFalse(window.light_barrier_plot.trace.single_part)
        window.consistency_single_part.setChecked(True)
        window.consistency_maximum_traversal.setValue(3.0)
        window.light_barrier_plot.set_connected(True)
        self.assertTrue(window.light_barrier_plot.trace.single_part)
        self.assertEqual(window.light_barrier_plot.trace.maximum_traversal_ms, 3000)


class PressurePlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import PressureControlGUI as gui
        self.gui = gui
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.global_path = Path(directory.name) / "light_barrier_settings.json"
        settings_patch = patch.object(gui, "LIGHT_BARRIER_SETTINGS_FILE", self.global_path)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        with patch.object(gui.AdsController, "start"):
            self.window = gui.PressureControlWindow()
        self.addCleanup(self.window.close)

    def test_live_feed_preserves_pressure_updates_and_logging(self):
        window, buffer = self.window, FilteredBuffer()
        snapshot = dict(buffer.status(), velocities=[100.0] * 4,
                        delays=[20.0] * 4, shot_counter=0,
                        avg_pressure_n1=12.0, avg_pressure_n2=13.0)
        window.ads.live_snapshot_ready.emit(snapshot)
        buffer.append(1, 100, True)
        buffer.append(1, 120, False)
        buffer.append(2, 200, True)
        buffer.append(8, 400, True)
        snapshot.update(buffer.status(), shot_counter=1)
        with patch.object(window, "append_pressure_log") as log:
            window.ads.live_snapshot_ready.emit(snapshot)
        log.assert_called_once_with(12.0, 13.0, 100.0, 100.0, 100.0, 100.0)
        self.assertEqual(window.rows[0].estimated_velocity.text(), "100.0 mm/s")
        self.assertEqual(window.light_barrier_plot.trace.display_elapsed_ms, 300)
        self.assertEqual([p[0] for p in window.light_barrier_plot.trace.display_points],
                         [0, 0, 20, 100, 300])
        self.assertTrue(window.light_barrier_plot.isHidden())
        self.assertFalse(window.online_status_table.isAncestorOf(window.light_barrier_plot))

    def test_settings_dialog_has_no_filter_checkbox_and_keeps_plot_after_reopen(self):
        window = self.window
        window.ads.connected = True
        self.assertIsInstance(window.light_barrier_plot.trace, PlcTriggerTrace)
        def inspect_dialog(dialog):
            self.assertFalse(hasattr(dialog, "single_part_control"))
            self.assertIs(dialog.light_barrier_plot, window.light_barrier_plot)
            dialog.show()
            self.app.processEvents()
            self.assertTrue(window.light_barrier_plot.isVisible())
            dialog.accept()
            return 0
        with patch.object(self.gui.LightBarrierSettingsDialog, "exec", inspect_dialog):
            for _ in range(2):
                window.open_light_barrier_settings()
                self.assertTrue(window.light_barrier_plot.isHidden())
                self.assertIs(window.light_barrier_plot.parentWidget(), window)
        window.on_connection_changed(False, "test")
        window.on_connection_changed(True, "")
        self.assertIsInstance(window.light_barrier_plot.trace, PlcTriggerTrace)

    def test_setup_feed_used_while_camera_is_open(self):
        buffer = FilteredBuffer()
        self.window.ads.setup_status_ready.emit(buffer.status())
        buffer.append(1, 100, True)
        buffer.append(8, 200, True)
        self.window.ads.setup_status_ready.emit(buffer.status())
        self.assertEqual(self.window.light_barrier_plot.trace.display_elapsed_ms, 100)

    def test_missing_plc_feed_shows_unavailable_instead_of_old_or_raw_plot(self):
        plot, buffer = self.window.light_barrier_plot, FilteredBuffer()
        plot.process_status(buffer.status())
        buffer.append(1, 100, True)
        buffer.append(8, 200, True)
        plot.process_status(buffer.status())
        self.assertIsNotNone(plot.completed_display)
        plot.process_status(Buffer().status())
        self.assertIsNone(plot.completed_display)
        self.assertIn("unavailable", plot.completed_message)
        self.assertFalse(plot.grab().isNull())

    def test_legacy_global_filter_preference_is_ignored(self):
        settings = self.window._current_light_barrier_settings()
        settings["light_barrier_single_part"] = False
        self.global_path.write_text(json.dumps(settings), encoding="utf-8")
        self.window._load_global_light_barrier_settings()
        self.assertIsInstance(self.window.light_barrier_plot.trace, PlcTriggerTrace)
        self.assertTrue(self.window.light_barrier_plot.trace.single_part)
        self.assertNotIn("light_barrier_single_part", self.window._current_light_barrier_settings())

    def test_save_changes_writes_global_settings_and_restores_after_restart(self):
        window = self.window
        window.ads.connected = True
        with tempfile.TemporaryDirectory() as directory:
            path = self.global_path

            def edit_and_save(dialog):
                dialog.spacing_controls[0].setKeyboardTracking(False)
                dialog.spacing_controls[0].lineEdit().setText("31.2 mm")
                dialog.sensor_to_array_controls[0].setValue(185.0)
                dialog.debounce_ms_control.setValue(17)
                dialog.invert_controls[1].setChecked(False)
                dialog.debounce_controls[2].setChecked(False)
                dialog.save_changes_button.click()
                self.assertIn("Saved globally", dialog.save_status.text())
                return 0

            with (patch.object(self.gui.LightBarrierSettingsDialog, "exec", edit_and_save),
                  patch.object(self.gui.QFileDialog, "getSaveFileName") as chooser,
                  patch.object(window.ads, "write_now"),
                  patch.object(window.ads, "queue_write")):
                window.open_light_barrier_settings()
            chooser.assert_not_called()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["sensor_spacing_12_mm"], 31.2)
            self.assertEqual(saved["sensor_2_to_array_1_spacing_mm"], 185.0)
            self.assertEqual(saved["light_barrier_debounce_ms"], 17)
            self.assertFalse(saved["light_barrier_inverted"][1])
            self.assertFalse(saved["light_barrier_debounce_enabled"][2])
            self.assertNotIn("light_barrier_single_part", saved)
            self.assertIsNone(window.profile_path)

            with patch.object(self.gui.AdsController, "start"):
                restored = self.gui.PressureControlWindow()
            self.addCleanup(restored.close)
            self.assertEqual(restored._current_light_barrier_settings(), saved)
            from test_pressure_control_gui import FakeClient, FakePlc
            worker = self.gui.AdsWorker()
            worker.client = FakeClient(FakePlc())
            snapshot = worker.read_initial_snapshot()
            for _ in range(2):  # Initial connection and reconnection.
                with patch.object(restored.ads, "write_now") as write:
                    restored.apply_initial_snapshot(snapshot)
                self.assertEqual(restored._current_light_barrier_settings(), saved)
                self.assertEqual(write.call_args.args[1], "global_light_barrier_settings")
                values = write.call_args.args[0]
                self.assertEqual(len(values), 25)
                self.assertEqual(values["MAIN.GuiSensorSpacing12Mm"], 31.2)
                self.assertEqual(values["MAIN.GuiSensor2ToArray1SpacingMm"], 185.0)

    def test_global_save_keeps_existing_profile_file(self):
        window = self.window
        window.ads.connected = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.json"
            original = '{"title": "Existing profile"}'
            path.write_text(original, encoding="utf-8")
            window.profile_path = path
            window.profile_directory = path.parent

            def cancel_save(dialog):
                dialog.save_changes_button.click()
                return 0

            with (patch.object(self.gui.LightBarrierSettingsDialog, "exec", cancel_save),
                  patch.object(self.gui.QFileDialog, "getSaveFileName", return_value=("", ""))):
                window.open_light_barrier_settings()
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertTrue(self.global_path.exists())

    def test_profile_load_cannot_override_any_global_light_barrier_settings(self):
        window = self.window
        original = window._current_light_barrier_settings()
        conflicting = {key: "obsolete profile value" for key in original if key != "version"}
        path = self.global_path.parent / "pressure.json"
        path.write_text(json.dumps({"version": 12, "arrays": [], **conflicting}), encoding="utf-8")
        with (patch.object(self.gui.QFileDialog, "getOpenFileName", return_value=(str(path), "")),
              patch.object(window, "write_all_values"),
              patch.object(self.gui.QMessageBox, "critical") as error):
            window.load_profile()
        error.assert_not_called()
        self.assertEqual(window._current_light_barrier_settings(), original)

    def test_failed_global_save_preserves_previous_file_and_cleans_temporary_file(self):
        self.assertTrue(self.window.save_global_light_barrier_settings())
        previous = self.global_path.read_text(encoding="utf-8")
        previous_settings = self.window.global_light_barrier_settings.copy()
        self.window.light_barrier_debounce.setValue(17)
        with (patch.object(Path, "replace", side_effect=OSError("write denied")),
              patch.object(self.gui.QMessageBox, "critical") as error):
            self.assertFalse(self.window.save_global_light_barrier_settings())
        error.assert_called_once()
        self.assertEqual(self.global_path.read_text(encoding="utf-8"), previous)
        self.assertEqual(self.window.global_light_barrier_settings, previous_settings)
        self.assertEqual(list(self.global_path.parent.glob("*.tmp")), [])

    def test_invalid_global_file_is_rejected_before_changing_settings(self):
        original = self.window._current_light_barrier_settings()
        invalid = dict(original, light_barrier_debounce_enabled=[True] * 7)
        self.global_path.write_text(json.dumps(invalid), encoding="utf-8")
        with patch.object(self.gui.QMessageBox, "warning") as warning:
            self.window._load_global_light_barrier_settings()
        warning.assert_called_once()
        self.assertIsNone(self.window.global_light_barrier_settings)
        self.assertEqual(self.window._current_light_barrier_settings(), original)

    def test_pressure_plot_only_redraws_completed_passes(self):
        plot, buffer = self.window.light_barrier_plot, FilteredBuffer()
        self.assertFalse(plot.live_updates)
        plot.process_status(buffer.status())
        empty_image = plot.grab().toImage()
        buffer.append(1, 100, True)
        buffer.append(1, 110, False)
        buffer.append(2, 150, True)
        plot.process_status(buffer.status())
        self.assertIsNone(plot.completed_display)
        self.assertEqual(plot.grab().toImage(), empty_image)
        buffer.append(8, 400, True)
        buffer.append(8, 420, False)
        # Preserve this completed pass even if the next one starts in this poll.
        buffer.append(1, 600, True)
        plot.process_status(buffer.status())
        self.assertEqual(plot.completed_display[0], 300)
        completed_image = plot.grab().toImage()
        self.assertNotEqual(completed_image, empty_image)
        buffer.append(3, 650, True)
        plot.process_status(buffer.status())
        self.assertEqual(plot.grab().toImage(), completed_image)
        buffer.append(7, 700, True)
        buffer.append(8, 800, True)
        plot.process_status(buffer.status())
        self.assertEqual(plot.completed_display[0], 200)
        self.assertNotEqual(plot.grab().toImage(), completed_image)

    def test_main_window_only_shows_status_table(self):
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.centralWidget().horizontalScrollBar().maximum(), 0)
        self.assertTrue(self.window.online_status_table.isVisible())
        self.assertFalse(self.window.light_barrier_plot.isVisible())

    def test_live_worker_supplies_buffer_without_switching_polling_mode(self):
        worker = self.gui.AdsWorker()
        worker.client = type("ConnectedClient", (), {"is_connected": True})()
        buffer = FilteredBuffer()
        buffer.append(1, 100, True)
        received = []
        worker.live_snapshot_ready.connect(received.append)
        with (patch.object(worker, "read_barrier_event_history", return_value=None),
              patch.object(worker, "read_filtered_barrier_event_history", return_value=buffer.data),
              patch.object(worker, "read_values", side_effect=lambda names: {
                  name: 100 if name == "MAIN.LightBarrierEventClockMs" else 0
                  for name in names
              }),
              patch.object(worker, "read_setup_snapshot") as setup):
            worker.poll()
        setup.assert_not_called()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["light_barrier_filtered_event_history"], buffer.data)
        self.assertEqual(received[0]["plc_event_clock_ms"], 100)
        self.assertEqual(received[0]["light_barriers"], [False] * 8)


if __name__ == "__main__":
    unittest.main()
