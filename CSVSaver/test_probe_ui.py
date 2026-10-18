import gc
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from probe_calibration_ui import ProbeCalibrationUi, ProbeTab
import test_probe_calibration as test_data


class ProbeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_constructing_and_disposing_controls_never_opens_devices_or_starts_thread(self):
        with patch("probe_hardware.ColibriController") as serial, patch("probe_hardware.ProbePlc") as plc:
            for _ in range(4):
                ui = ProbeCalibrationUi("test", "127.0.0.1", 851, "unused.json")
                self.assertIsNone(ui.devices.thread)
                self.assertFalse(ui.barrier_tab.start.isEnabled())
                self.assertFalse(ui.nozzle_tab.start.isEnabled())
                ui.shutdown()
                del ui
                gc.collect()
                self.app.processEvents()
            serial.assert_not_called()
            plc.assert_not_called()

    def test_nozzle_duration_can_be_adjusted_to_predicted_round_trip(self):
        tab = ProbeTab("nozzle")
        self.assertLess(tab.open_time.value(), tab.required_duration())
        tab.suggest_duration.click()
        self.assertGreaterEqual(tab.open_time.value(), tab.required_duration())
        tab.config(0.005, 1)

    def test_travel_direction_toggle_and_free_sensor_order(self):
        for mode in ("barriers", "nozzle"):
            with self.subTest(mode=mode):
                tab = ProbeTab(mode)
                tab.travel.setValue(40)
                if mode == "nozzle":
                    tab.suggest_duration.click()
                for sign in (-1, 1):
                    tab.direction.setCurrentIndex(tab.direction.findData(sign))
                    config = tab.config(0.005, 1)
                    self.assertEqual(config.travel_mm, 40)
                    self.assertEqual(config.outward_target_mm, sign * 40)
                    self.assertIn("−40 mm" if sign < 0 else "+40 mm", tab.path_hint.text())
                if mode == "barriers":
                    for first, second in ((2, 1), (1, 2)):
                        tab.first.setCurrentIndex(tab.first.findData(first))
                        tab.second.setCurrentIndex(tab.second.findData(second))
                        config = tab.config(0.005, 1)
                        self.assertEqual(config.sensors, (first, second))

    def test_manual_move_submits_negative_target(self):
        ui = ProbeCalibrationUi("test", "127.0.0.1", 851, "unused.json")
        try:
            ui.move_target.setValue(-12.5)
            with patch.object(ui, "_remember"), patch.object(ui.devices, "submit") as submit:
                ui._manual("move")
            kind, (config, target) = submit.call_args.args
            self.assertEqual(kind, "move")
            self.assertEqual(target, -12.5)
            self.assertEqual(config.travel_mm, 75)
        finally:
            ui.shutdown()

    def test_completed_session_populates_signed_force_peaks_and_plot(self):
        from probe_calibration import force_peak
        tab = ProbeTab("nozzle")
        peaks, curve = force_peak(test_data.ForceAndRecordingTests.curve(sign=-1), 0.0001)
        document = {"config": {"mode": "nozzle", "array": 1, "nozzle": 3}, "status": "complete", "apply_allowed": True,
                    "rows": [{"cycle": 1, "direction": "forward", "distance_mm": 20, "reference_position_mm": 10,
                              "peaks": peaks, "curve": curve, "estimated_uncertainty_mm": 0.15}],
                    "summary": {"count": 1, "mean_mm": 20, "std_mm": 0}}
        tab.available = True
        tab.receive({"path": "example", "document": document})
        self.assertTrue(tab.apply.isEnabled())
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertIn("-", tab.table.item(0, 5).text())
        self.assertIn("0,1 mm", tab.summary.text())
        tab.resize(1100, 850)
        tab.show()
        self.app.processEvents()
        self.assertFalse(tab.plot.grab().isNull())
        tab.close()

    def test_partial_session_or_nonadjacent_pair_cannot_be_applied(self):
        tab = ProbeTab("barriers")
        tab.available = True
        tab.document = {"apply_allowed": False}
        tab.update_controls()
        self.assertFalse(tab.apply.isEnabled())
        tab.document = {"apply_allowed": True, "config": {"first_sensor": 1, "second_sensor": 3}}
        tab.update_controls()
        self.assertFalse(tab.apply.isEnabled())
        tab.document = {"apply_allowed": True, "config": {"first_sensor": 2, "second_sensor": 1}}
        tab.update_controls()
        self.assertTrue(tab.apply.isEnabled())


if __name__ == "__main__":
    unittest.main()
