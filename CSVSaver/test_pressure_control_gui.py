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
import ConveyorSetupGUI as setup_gui


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

    def test_first_light_barrier_spacing_default_is_calibrated_value(self):
        self.assertEqual(gui.SENSOR_SPACING_12_DEFAULT_MM, 23.54)
        self.assertEqual(gui.SENSOR_SPACING_34_DEFAULT_MM, 38.33)
        self.assertEqual(gui.SENSOR_SPACING_56_DEFAULT_MM, 64.69)

    def test_pressure_inputs_use_ten_mbar_steps(self):
        row = gui.ArrayRow(1)

        self.assertEqual(row.pressure.singleStep(), 10)

    def test_provisional_conveyor_calibration_is_the_gui_default(self):
        controller = gui.AdsController()

        self.assertTrue(controller.calibration_cache["valid"])
        self.assertAlmostEqual(
            controller.calibration_cache["mm_per_full_step"],
            0.32960026,
        )
        self.assertEqual(controller.force_response_delays_ms, [25.8, 0.0, 0.0, 0.0])
        self.assertEqual(
            controller.force_single_nozzle_response_delays_ms,
            [34.0, 0.0, 0.0, 0.0],
        )
        controller.shutdown()

    def test_invalid_plc_calibration_is_replaced_on_initial_snapshot(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        snapshots = []
        controller.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )
        controller.initial_snapshot_ready.connect(snapshots.append)
        snapshot = {
            "calibration": {
                "marker_distance_mm": 315.0,
                "jog_steps": 100,
                "jog_speed_full_steps_per_sec": 10.0,
                "mm_per_full_step": 0.0,
                "valid": False,
            }
        }

        controller.on_initial_snapshot(snapshot)

        self.assertTrue(snapshots[0]["calibration"]["valid"])
        self.assertAlmostEqual(
            snapshots[0]["calibration"]["mm_per_full_step"], 0.32960026
        )
        self.assertEqual(writes[0][1], "default_conveyor_calibration")
        self.assertTrue(writes[0][0]["MAIN.GuiConveyorCalibrationValid"])
        controller.shutdown()

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
        self.assertFalse(
            any("MeasuredValveTriggerDelay" in name for name in plc.read_calls[0])
        )

    def test_force_delay_status_uses_one_sum_read(self):
        plc = FakePlc()
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)

        status = worker.read_force_delay_snapshot()

        self.assertEqual(len(plc.read_calls), 1)
        self.assertEqual(len(plc.read_calls[0]), 34)
        self.assertEqual(status["result_counter"], 0)
        self.assertEqual(status["current_signal"], 0.0)

    def test_force_delay_start_is_one_configuration_batch(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        controller.start_force_delay_measurement(4, 2, 2500, 0.125)

        values, context = writes[0]
        self.assertEqual(context, "force_delay_start")
        self.assertTrue(values["MAIN.GuiForceDelayMeasurementEnabled"])
        self.assertEqual(values["MAIN.GuiForceDelayLightBarrier"], 4)
        self.assertEqual(values["MAIN.GuiForceDelaySensor"], 2)
        self.assertEqual(values["MAIN.GuiForceDelayWindowMs"], 2500)
        self.assertEqual(values["MAIN.GuiForceDelayMinRise"], 0.125)
        controller.shutdown()

    def test_force_response_delay_write_targets_selected_array(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        controller.set_force_response_delays(3, 36.2, 31.4)

        self.assertEqual(
            writes[0],
            (
                {
                    "MAIN.GuiForceSingleNozzleResponseDelayMs3": 36.2,
                    "MAIN.GuiForceResponseDelayMs3": 31.4,
                },
                "force_response_delays_array_3",
            ),
        )
        controller.shutdown()

    def test_force_response_delay_interpolates_nozzle_count(self):
        self.assertEqual(gui.calculate_force_response_delay(34.0, 25.8, 1), 34.0)
        self.assertAlmostEqual(
            gui.calculate_force_response_delay(34.0, 25.8, 2), 31.2666667
        )
        self.assertAlmostEqual(
            gui.calculate_force_response_delay(34.0, 25.8, 3), 28.5333333
        )
        self.assertEqual(gui.calculate_force_response_delay(34.0, 25.8, 4), 25.8)

    def test_force_delay_statistics_report_consistency(self):
        result = gui.calculate_force_delay_statistics([230.0, 237.0, 244.0])

        self.assertEqual(result["mean"], 237.0)
        self.assertAlmostEqual(
            result["standard_deviation"], (98.0 / 3.0) ** 0.5
        )
        self.assertEqual(result["minimum"], 230.0)
        self.assertEqual(result["maximum"], 244.0)

    def test_force_delay_dialog_logs_valid_and_rejected_results(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )
        base_status = {
            "enabled": False,
            "light_barrier": 2,
            "sensor": 1,
            "window_ms": 2000,
            "minimum_rise": 0.05,
            "busy": False,
            "status_code": 1,
            "result_counter": 5,
            "valid_count": 0,
            "invalid_count": 0,
            "last_valid": False,
            "light_barrier_time_ms": 1000,
            "peak_time_ms": 1237,
            "peak_delay_ms": 237,
            "baseline": 0.1,
            "peak": 0.8,
            "peak_rise": 0.7,
            "current_signal": 0.12,
        }

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "force_delay.csv"
            with patch.object(gui, "FORCE_DELAY_LOG_FILE", log_path):
                dialog = gui.ForceDelayDialog(controller)
                dialog._apply_status(base_status)
                dialog._start()
                dialog._apply_status(
                    {
                        **base_status,
                        "enabled": True,
                        "status_code": 3,
                        "result_counter": 6,
                        "last_valid": True,
                    }
                )
                dialog._apply_status(
                    {
                        **base_status,
                        "enabled": True,
                        "status_code": 4,
                        "result_counter": 7,
                        "peak_delay_ms": 400,
                        "peak": 0.12,
                        "peak_rise": 0.02,
                    }
                )

                self.assertEqual(len(dialog.measurements), 2)
                self.assertEqual(dialog.mean_label.text(), "237.0 ms")
                self.assertEqual(dialog.count_label.text(), "1 valid / 1 invalid")
                self.assertEqual(len(log_path.read_text().splitlines()), 3)
                dialog.close()

        self.assertTrue(
            any(
                values.get("MAIN.GuiForceDelayMeasurementEnabled") is False
                and context == "force_delay_stop"
                for values, context in writes
            )
        )
        controller.shutdown()

    def test_setup_status_uses_one_sum_read(self):
        plc = FakePlc()
        worker = gui.AdsWorker()
        worker.client = FakeClient(plc)

        snapshot = worker.read_setup_snapshot()

        self.assertEqual(len(plc.read_calls), 1)
        self.assertEqual(len(plc.read_calls[0]), 56)
        self.assertEqual(snapshot["light_barriers"], [False] * 6)
        self.assertEqual(snapshot["raw_light_barriers"], [False] * 6)
        self.assertEqual(snapshot["light_barrier_event_counts"], [0] * 6)
        self.assertEqual(snapshot["light_barrier_event_times_ms"], [0] * 6)
        self.assertEqual(snapshot["debounce_ms"], 0)

    def test_barrier_calibration_start_is_one_safe_batch(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(lambda values, context: writes.append((values, context)))

        controller.start_barrier_calibration(1, 2, 3000, 30.0, 20)

        values, context = writes[0]
        self.assertEqual(context, "barrier_calibration_start")
        self.assertFalse(values["MAIN.GuiConveyorEnabled"])
        self.assertTrue(values["MAIN.GuiConveyorCalibrationMode"])
        self.assertEqual(values["MAIN.GuiBarrierCalibrationFirstSensor"], 1)
        self.assertEqual(values["MAIN.GuiBarrierCalibrationSecondSensor"], 2)
        self.assertEqual(values["MAIN.GuiBarrierCalibrationDebounceMs"], 20)
        self.assertEqual(values["MAIN.GuiCalibrationJogSteps"], 3000)
        self.assertTrue(values["MAIN.GuiBarrierCalibrationStart"])
        self.assertTrue(values["MAIN.GuiCalibrationMoveRight"])
        controller.shutdown()

    def test_velocity_check_starts_constant_safe_conveyor_motion(self):
        controller = gui.AdsController()
        controller.connected = True
        writes = []
        controller.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        controller.start_velocity_check(12.5)

        values, context = writes[0]
        self.assertEqual(context, "velocity_check_start")
        self.assertTrue(values["MAIN.GuiVelocityCheckMode"])
        self.assertTrue(values["MAIN.GuiResetVelocityEstimates"])
        self.assertTrue(values["MAIN.GuiConveyorEnabled"])
        self.assertFalse(values["MAIN.GuiConveyorCalibrationMode"])
        self.assertEqual(values["MAIN.GuiConveyorSpeedMmPerSec"], 12.5)
        self.assertNotIn("MAIN.GuiCalibrationMoveRight", values)
        controller.shutdown()

    def test_velocity_plausibility_uses_spacing_and_switch_time(self):
        result = setup_gui.calculate_velocity_plausibility(20.0, 2000, 10.0, 5.0)

        self.assertAlmostEqual(result["measured_speed"], 10.0)
        self.assertAlmostEqual(result["deviation_percent"], 0.0)
        self.assertTrue(result["plausible"])

    def test_velocity_dialog_accepts_only_a_fresh_measurement(self):
        controller = gui.AdsController()
        controller.connected = True
        dialog = setup_gui.VelocityPlausibilityDialog(controller)
        dialog.target_speed_input.setValue(10.0)
        dialog._start()
        waiting = {
            "sensor_spacings": (20.0, 100.0, 100.0),
            "velocity_valid": (False, False, False),
            "velocity_times_ms": (0, 0, 0),
        }
        complete = {
            **waiting,
            "velocity_valid": (True, False, False),
            "velocity_times_ms": (2000, 0, 0),
        }

        dialog._on_status(waiting)
        dialog._on_status(complete)

        self.assertEqual(dialog.measured_speed_label.text(), "10.000 mm/s")
        self.assertEqual(dialog.verdict_label.text(), "Plausible")
        dialog._stop()
        controller.shutdown()

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
                result["force_response_delays_ms"] = list(
                    window.ads.force_response_delays_ms
                )
                result["force_single_nozzle_response_delays_ms"] = list(
                    window.ads.force_single_nozzle_response_delays_ms
                )
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
        self.assertEqual(result["force_response_delays_ms"], [25.8, 0.0, 0.0, 0.0])
        self.assertEqual(
            result["force_single_nozzle_response_delays_ms"],
            [34.0, 0.0, 0.0, 0.0],
        )

    def test_version_3_profile_preserves_force_response_delays(self):
        result = self.load_profile(
            {
                "version": 3,
                "arrays": [],
                "conveyor_calibration": {
                    "marker_distance_mm": 315.0,
                    "mm_per_full_step": 0.05,
                    "valid": True,
                },
                "force_response_delays_ms": [25.8, 26.1, 27.2, 28.3],
            }
        )

        self.assertEqual(
            result["force_response_delays_ms"], [25.8, 26.1, 27.2, 28.3]
        )
        self.assertEqual(
            result["force_single_nozzle_response_delays_ms"],
            [34.0, 0.0, 0.0, 0.0],
        )

    def test_version_4_profile_preserves_both_response_delay_endpoints(self):
        result = self.load_profile(
            {
                "version": 4,
                "arrays": [],
                "conveyor_calibration": {
                    "marker_distance_mm": 315.0,
                    "mm_per_full_step": 0.05,
                    "valid": True,
                },
                "force_response_delays_ms": [25.8, 26.1, 27.2, 28.3],
                "force_single_nozzle_response_delays_ms": [
                    34.0,
                    35.0,
                    36.0,
                    37.0,
                ],
            }
        )

        self.assertEqual(
            result["force_single_nozzle_response_delays_ms"],
            [34.0, 35.0, 36.0, 37.0],
        )


class ConveyorSetupWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_ur_pose_distance_uses_xyz_translation(self):
        distance, delta = setup_gui.calculate_ur_pose_distance(
            (0.1, 0.2, 0.3, 0.0, 0.0, 0.0),
            (0.122, 0.2, 0.3, 1.0, 2.0, 3.0),
        )

        self.assertAlmostEqual(distance, 22.0)
        self.assertAlmostEqual(delta[0], 22.0)
        self.assertAlmostEqual(delta[1], 0.0)
        self.assertAlmostEqual(delta[2], 0.0)

    def test_speed_statistics_report_mean_spread_and_target_error(self):
        result = setup_gui.calculate_speed_statistics([14.0, 15.0, 16.0], 15.0)

        self.assertAlmostEqual(result["mean"], 15.0)
        self.assertAlmostEqual(result["standard_deviation"], (2.0 / 3.0) ** 0.5)
        self.assertEqual(result["minimum"], 14.0)
        self.assertEqual(result["maximum"], 16.0)
        self.assertAlmostEqual(result["mean_error_percent"], 0.0)

    def test_ur_speed_monitor_uses_plc_event_timestamps_in_both_directions(self):
        with patch.object(gui.AdsController, "start"):
            window = setup_gui.ConveyorSetupWindow()
        window.ads.connected = True
        window.connected = True
        window.have_setup_status = True
        window.ur_connected = True
        window.latest_ur_pose = (1.0, (0.1, 0.2, 0.3, 0.0, 0.0, 0.0))
        status = {
            "light_barriers": [False] * 6,
            "light_barrier_event_counts": [0] * 6,
            "light_barrier_event_times_ms": [0] * 6,
            "sensor_spacings": (58.356, 27.13, 39.254),
        }
        window.latest_status = status
        window.ur_target_speed.setValue(15.0)
        window._append_ur_speed_log = lambda sample: None

        window._start_ur_speed_monitor()
        status["light_barriers"][0] = True
        status["light_barrier_event_counts"][0] = 1
        status["light_barrier_event_times_ms"][0] = 1000
        window._process_ur_speed_monitor(status)
        status["light_barriers"][1] = True
        status["light_barrier_event_counts"][1] = 1
        status["light_barrier_event_times_ms"][1] = 4890
        window._process_ur_speed_monitor(status)

        status["light_barriers"][1] = False
        status["light_barrier_event_counts"][1] = 2
        status["light_barrier_event_times_ms"][1] = 6000
        window._process_ur_speed_monitor(status)
        status["light_barriers"][0] = False
        status["light_barrier_event_counts"][0] = 2
        status["light_barrier_event_times_ms"][0] = 9890
        window._process_ur_speed_monitor(status)

        self.assertEqual(len(window.ur_monitor_samples), 2)
        self.assertAlmostEqual(window.ur_monitor_samples[0]["speed"], 15.0, places=2)
        self.assertEqual(window.ur_monitor_samples[0]["direction"], "LB 1 -> LB 2")
        self.assertEqual(window.ur_monitor_samples[1]["direction"], "LB 2 -> LB 1")
        self.assertIn("error +0.01%", window.ur_monitor_mean_label.text())
        window.ads.connected = False
        window.close()

    def test_ur_speed_monitor_rejects_short_pair_and_recovers(self):
        with patch.object(gui.AdsController, "start"):
            window = setup_gui.ConveyorSetupWindow()
        window.ur_target_speed.setValue(100.0)
        window.ur_first_sensor.setCurrentIndex(4)
        window.ur_second_sensor.setCurrentIndex(5)
        window._append_ur_speed_log = lambda sample: None
        status = {"sensor_spacings": (22.34, 39.254, 64.69)}
        window.ur_monitor_pending_edges[True] = (1000, 5)

        window._accept_ur_monitor_event(1120, 6, True, status)

        self.assertEqual(window.ur_monitor_samples, [])
        self.assertEqual(window.ur_monitor_pending_edges[True], (1120, 6))
        self.assertIn("Ignored implausible", window.ur_monitor_state_label.text())

        window._accept_ur_monitor_event(1773, 5, True, status)

        self.assertEqual(len(window.ur_monitor_samples), 1)
        self.assertEqual(window.ur_monitor_samples[0]["direction"], "LB 6 -> LB 5")
        self.assertAlmostEqual(window.ur_monitor_samples[0]["speed"], 99.07, places=2)
        window.ads.connected = False
        window.close()

    def test_ur_capture_uses_debounced_barriers_and_applies_spacing(self):
        with patch.object(gui.AdsController, "start"):
            window = setup_gui.ConveyorSetupWindow()
        window.ads.connected = True
        window.connected = True
        window.have_setup_status = True
        window.ur_connected = True
        window.latest_status = {"light_barriers": [False] * 6}
        window.latest_ur_pose = (1.0, (0.1, 0.2, 0.3, 0.0, 0.0, 0.0))
        writes = []
        window.ads.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        window._start_ur_capture()
        window._process_ur_capture([True, False, False, False, False, False])
        window.latest_ur_pose = (2.0, (0.122, 0.2, 0.3, 0.0, 0.0, 0.0))
        window._process_ur_capture([True, True, False, False, False, False])
        window._apply_ur_measurement()

        self.assertFalse(window.ur_capture_active)
        self.assertAlmostEqual(window.ur_distance_mm, 22.0)
        self.assertEqual(window.ur_distance_label.text(), "22.000 mm")
        self.assertAlmostEqual(writes[0][0]["MAIN.GuiSensorSpacing12Mm"], 22.0)
        self.assertEqual(writes[0][1], "ur_sensor_spacing_12")
        window.ads.connected = False
        window.close()

    def test_setup_window_displays_barriers_and_applies_known_pair(self):
        with patch.object(gui.AdsController, "start"):
            window = setup_gui.ConveyorSetupWindow()
        window.ads.connected = True
        window._on_connection_changed(True, "")
        status = {
            "light_barriers": [True, False, True, False, True, False],
            "internal_position": 1000,
            "ready_to_execute": True,
            "drive_busy": False,
            "drive_error": False,
            "active": False,
            "first_captured": True,
            "second_captured": True,
            "valid": True,
            "first_position": 1000,
            "second_position": 2920,
            "difference_increments": 1920,
            "distance_mm": 10.0,
            "status_code": 3,
            "first_sensor": 1,
            "second_sensor": 2,
            "debounce_ms": 20,
            "mm_per_full_step": 1.0 / 3.0,
            "conveyor_calibration_valid": True,
            "full_steps_per_sec": 30.0,
            "velocity_raw": 150,
            "sensor_spacings": (100.0, 110.0, 120.0),
        }
        writes = []
        window.ads.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        window._on_setup_status(status)
        window.first_sensor.setCurrentIndex(2)
        window.second_sensor.setCurrentIndex(3)
        window._apply_measurement()

        self.assertEqual(window.barrier_labels[0].text(), "ON")
        self.assertEqual(window.barrier_labels[1].text(), "OFF")
        self.assertEqual(window.measured_distance.text(), "10.000 mm")
        self.assertEqual(window.debounce_time.value(), 20)
        self.assertEqual(
            writes[0][0], {"MAIN.GuiSensorSpacing12Mm": 10.0}
        )
        window.ads.connected = False
        window.close()

    def test_measurement_enables_drive_before_starting_when_not_ready(self):
        with patch.object(gui.AdsController, "start"):
            window = setup_gui.ConveyorSetupWindow()
        window.ads.connected = True
        window.connected = True
        window.mm_per_full_step = 0.32960026
        window.latest_status = {"ready_to_execute": False}
        writes = []
        window.ads.write_requested.connect(
            lambda values, context: writes.append((values, context))
        )

        window._start_measurement()

        self.assertEqual(writes[0][1], "setup_enable_drive")
        self.assertTrue(writes[0][0]["MAIN.GuiConveyorCalibrationMode"])
        self.assertIsNotNone(window.pending_measurement)

        ready_status = {
            "light_barriers": [True] * 6,
            "internal_position": 0,
            "ready_to_execute": True,
            "drive_busy": False,
            "drive_error": False,
            "active": False,
            "first_captured": False,
            "second_captured": False,
            "valid": False,
            "first_position": 0,
            "second_position": 0,
            "difference_increments": 0,
            "distance_mm": 0.0,
            "status_code": 0,
            "first_sensor": 1,
            "second_sensor": 2,
            "debounce_ms": 20,
            "mm_per_full_step": 0.32960026,
            "conveyor_calibration_valid": True,
            "full_steps_per_sec": 0.0,
            "velocity_raw": 0,
            "sensor_spacings": (100.0, 100.0, 100.0),
        }
        window._on_setup_status(ready_status)

        self.assertEqual(writes[1][1], "barrier_calibration_start")
        self.assertEqual(writes[1][0]["MAIN.GuiBarrierCalibrationDebounceMs"], 20)
        self.assertIsNone(window.pending_measurement)
        window.ads.connected = False
        window.close()


if __name__ == "__main__":
    unittest.main()
