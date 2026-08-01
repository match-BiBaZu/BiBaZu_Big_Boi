import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFileDialog

import PressureControlGUI as gui


class SignalBridge(QObject):
    write = pyqtSignal(object, str)


class FakePlc:
    def __init__(self, write_delay=0.0):
        self.read_calls = []
        self.write_calls = []
        self.write_delay = write_delay

    def read_list_by_name(self, names, cache_symbol_info=True):
        self.read_calls.append(list(names))
        return {name: 315.0 if name.endswith("MarkerDistanceMm") else 0 for name in names}

    def write_list_by_name(self, values, cache_symbol_info=True):
        self.write_calls.append(dict(values))
        time.sleep(self.write_delay)
        return {name: "no error" for name in values}


class FakeClient:
    def __init__(self, plc):
        self.plc = plc

    @property
    def is_connected(self):
        return self.plc is not None

    def close(self):
        self.plc = None


class AdsThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def spin(self, milliseconds):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def test_debounce_keeps_only_latest_value(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(lambda values, context: writes.append((values, context)))

        controller.queue_write("MAIN.GuiPressureMbar1", 100, "pressure")
        controller.queue_write("MAIN.GuiPressureMbar1", 200, "pressure")
        controller.queue_write("MAIN.GuiPressureMbar1", 300, "pressure")
        self.spin(gui.ADS_WRITE_DEBOUNCE_MS + 50)

        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], {"MAIN.GuiPressureMbar1": 300})
        controller.shutdown()

    def test_stop_bypasses_debounce(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(lambda values, context: writes.append((values, context)))

        controller.queue_write("MAIN.GuiPressureMbar1", 300, "pressure")
        controller.stop_calibration_move()

        self.assertEqual(
            writes[0],
            ({"MAIN.GuiCalibrationStop": True}, "calibration_stop"),
        )
        controller.shutdown()

    def test_disconnect_discards_debounced_writes(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(lambda values, context: writes.append((values, context)))

        controller.queue_write("MAIN.GuiPressureMbar1", 300, "pressure")
        controller.on_connection_changed(False, "test disconnect")
        self.spin(gui.ADS_WRITE_DEBOUNCE_MS + 50)

        self.assertEqual(writes, [])
        controller.shutdown()

    def test_calibration_status_uses_one_sum_read(self):
        plc = FakePlc()
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)

        status = worker.read_calibration_snapshot()

        self.assertEqual(len(plc.read_calls), 1)
        self.assertEqual(len(plc.read_calls[0]), 14)
        self.assertEqual(status["marker_distance_mm"], 315.0)

    def test_normal_status_uses_one_sum_read(self):
        plc = FakePlc()
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)

        snapshot = worker.read_live_snapshot()

        self.assertEqual(len(plc.read_calls), 1)
        self.assertEqual(snapshot["shot_counter"], 0)

    def test_leaving_calibration_writes_safe_state_together(self):
        plc = FakePlc()
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)

        worker.set_calibration_mode(False)

        self.assertEqual(plc.write_calls, [gui.AdsWorker.SAFE_STOP_VALUES])

    def test_mm_jog_is_rounded_to_full_steps(self):
        full_steps, actual_distance, full_steps_per_sec = gui.calculate_conveyor_jog(
            1.0, 10.0, 0.3296
        )

        self.assertEqual(full_steps, 3)
        self.assertAlmostEqual(actual_distance, 0.9888)
        self.assertAlmostEqual(full_steps_per_sec, 10.0 / 0.3296)

    def test_jog_dialog_sends_calibrated_relative_move(self):
        controller = gui.AdsController()
        controller.connected = True
        controller.calibration_cache.update(
            {
                "valid": True,
                "mm_per_full_step": 0.3296,
                "jog_speed_full_steps_per_sec": 32.0,
            }
        )
        writes = []
        controller.write_requested.connect(lambda values, context: writes.append((values, context)))
        dialog = gui.ConveyorJogDialog(controller)
        dialog.refresh_status(
            {
                "busy": False,
                "error": False,
                "ready_to_execute": True,
                "status_code": 0,
            }
        )
        dialog.distance.setValue(1.0)

        dialog._move("right")

        self.assertEqual(writes[0][0]["MAIN.GuiCalibrationJogSteps"], 3)
        self.assertTrue(writes[0][0]["MAIN.GuiCalibrationMoveRight"])
        self.assertAlmostEqual(
            writes[0][0]["MAIN.GuiCalibrationJogSpeedFullStepsPerSec"],
            dialog.speed.value() / 0.3296,
        )
        dialog.close()
        controller.shutdown()

    def test_slow_ads_write_does_not_block_gui_thread(self):
        plc = FakePlc(write_delay=0.5)
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)
        thread = QThread()
        worker.moveToThread(thread)
        bridge = SignalBridge()
        bridge.write.connect(worker.write_values)
        thread.start()

        ticks = []
        tick_timer = QTimer()
        tick_timer.setInterval(20)
        tick_timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        tick_timer.start()
        loop = QEventLoop()
        worker.write_finished.connect(loop.quit)
        QTimer.singleShot(1000, loop.quit)
        bridge.write.emit({"MAIN.Test": 1}, "slow write")
        loop.exec()
        tick_timer.stop()

        thread.quit()
        self.assertTrue(thread.wait(1000))
        self.assertGreaterEqual(len(ticks), 10)
        self.assertEqual(plc.write_calls, [{"MAIN.Test": 1}])


class ProfileCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def load_profile(self, profile):
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            with (
                patch.object(gui.AdsController, "start"),
                patch.object(
                    QFileDialog,
                    "getOpenFileName",
                    return_value=(str(profile_path), "JSON Profile (*.json)"),
                ),
            ):
                window = gui.PressureControlWindow()
                window.load_profile()
                result = dict(window.conveyor_calibration)
                window.close()
                return result

    def test_version_1_profile_loads_uncalibrated(self):
        result = self.load_profile({"version": 1, "arrays": []})
        self.assertFalse(result["valid"])
        self.assertEqual(result["mm_per_full_step"], 0.0)

    def test_version_2_profile_preserves_calibration(self):
        result = self.load_profile(
            {
                "version": 2,
                "arrays": [],
                "conveyor_calibration": {
                    "marker_distance_mm": 315.0,
                    "mm_per_full_step": 0.05,
                    "valid": True,
                },
            }
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["mm_per_full_step"], 0.05)


if __name__ == "__main__":
    unittest.main()
