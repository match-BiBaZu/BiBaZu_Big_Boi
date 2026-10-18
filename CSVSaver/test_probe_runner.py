"""Complete calibration runs against a simulated serial axis, optics and force source."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probe_calibration import MeasurementError, ProbeConfig
from probe_hardware import ForceStream, ProbeRunner
from probe_vendor.colibri import ColibriController
from test_probe_calibration import force_sample, profile, ring


class Clock:
    def __init__(self):
        self.now = 100.0
        self.offset = 1_800_000_000_000_000_000

    def monotonic(self):
        return self.now

    def monotonic_ns(self):
        return round(self.now * 1e9)

    def time_ns(self):
        return self.offset + self.monotonic_ns()

    def sleep(self, seconds):
        self.now += seconds


class Cancel:
    def __init__(self, clock):
        self.clock = clock
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.clock.sleep(seconds)
        return self.cancelled


class Axis:
    def __init__(self, clock, forces):
        self.clock, self.forces = clock, forces
        self.position = self.target = 0.0
        self.speed = 5.0
        self.sequence = 0
        self.referenced = False
        self.stops = 0
        self.moves = []
        self.air_available = None
        self.sample()

    def sample(self):
        total = 0.1 * math.exp(-((self.position + 10) / 2) ** 2)
        if self.air_available is not None and not self.air_available():
            total = 0.0
        self.sequence += 1
        self.forces.receive(force_sample(self.sequence, self.clock.time_ns(), total * 0.6, total * 0.4))

    def position_steps(self):
        self.clock.sleep(0.02)
        distance = min(abs(self.target - self.position), self.speed * 0.025)
        self.position += distance if self.target > self.position else -distance
        self.sample()
        return round(self.position / 0.005)

    def status(self):
        motor_status = 0x68 | (0x10 if self.referenced else 0)
        if abs(self.target - self.position) > 0.001:
            motor_status |= 0x01
        return ColibriController._decode_status(self, motor_status, 0, 0)

    def set_parameter(self, index, subindex, value, size):
        if (index, subindex) == (3, 2):
            self.speed = value * 0.005

    def parameter(self, index, subindex):
        if (index, subindex) == (3, 2):
            return round(self.speed / 0.005)
        raise ValueError("Unexpected simulated parameter")

    def set_remote(self):
        pass

    def enable(self):
        pass

    def configure_negative_reference(self):
        pass

    def reference(self):
        self.target = self.position = 0.0
        self.referenced = True

    def move_absolute_steps(self, steps):
        self.target = steps * 0.005
        self.moves.append(self.target)

    def stop(self):
        self.target = self.position
        self.stops += 1


class Plc:
    def __init__(self, clock, axis):
        self.clock, self.axis = clock, axis
        self.history = ring()
        self.clear = {s: True for s in range(1, 9)}
        self.active = self.armed = self.open = False
        self.opened_ms = 0
        self.open_count = 0
        self.open_positions = []
        self.edges = []
        self.config = None
        self.on_snapshot = None
        self.fault = 0
        self.releases = 0

    def command(self, command, config=None, cancel=None):
        if command == 1:
            self.active, self.config = True, config
        elif command == 2:
            self.armed = True
        elif command == 3:
            self.open = self.armed = False
        elif command == 5:
            self.open = True
            self.opened_ms = round(self.clock.now * 1000) & 0xFFFFFFFF
            self.open_count += 1
            self.open_positions.append(self.axis.position)

    def pulse_heartbeat(self):
        pass

    def snapshot(self):
        stamp = round(self.clock.now * 1000) & 0xFFFFFFFF
        self.history[2] = stamp
        for sensor, start in ((1, -50), (2, -30)):
            clear = not start <= self.axis.position <= start + 2
            if clear != self.clear[sensor]:
                self.clear[sensor] = clear
                self.history[1] += 1
                slot = 3 + (self.history[1] % 256) * 3
                self.history[slot:slot + 3] = [self.history[1], stamp, sensor * 2 + int(clear)]
                self.edges.append((sensor, clear, self.axis.position))
                if sensor == 2 and not clear and self.armed:
                    self.open = True
                    self.armed = False
                    self.opened_ms = stamp
                    self.open_count += 1
        if self.on_snapshot:
            self.on_snapshot(self)
        return {"history": list(self.history), "sampled_ns": self.clock.monotonic_ns(), "roundtrip_ns": 1_000_000,
                "clear": dict(self.clear), "active": self.active, "fault": self.fault,
                "belt_stopped": True, "open": self.open, "opened_ms": self.opened_ms}

    def release(self):
        self.active = self.open = self.armed = False
        self.releases += 1


class CompleteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for method in ("monotonic", "monotonic_ns", "time_ns", "sleep"):
            patcher = patch(f"probe_hardware.time.{method}", getattr(self.clock, method))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.forces = ForceStream()
        self.forces.profile = profile()
        self.forces.tare = {"noise_total_n": 0.0001}
        self.forces.noise_n = 0.0001
        self.axis = Axis(self.clock, self.forces)
        self.plc = Plc(self.clock, self.axis)
        self.axis.air_available = lambda: self.plc.open
        self.cancel = Cancel(self.clock)
        self.updates = []
        self.runner = ProbeRunner(self.axis, self.plc, self.forces, self.cancel, update=self.updates.append)

    def test_five_barrier_cycles_terminate_at_home_with_ten_distances(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.runner.run(ProbeConfig(speed_mm_s=5, travel_mm=65), directory)
            self.assertTrue(result["apply_allowed"])
            self.assertEqual(len(result["rows"]), 10)
            self.assertAlmostEqual(result["summary"]["mean_mm"], 20, delta=0.02)
            self.assertEqual([r["direction"] for r in result["rows"]], ["forward", "backward"] * 5)
            self.assertAlmostEqual(self.axis.position, 0)
            self.assertFalse(self.plc.open)
            self.assertEqual(self.plc.open_count, 0)
            self.assertEqual(result["geometry"]["outward_target_mm"], -65)
            self.assertEqual(result["geometry"]["outward_sensor_order"], [2, 1])
            self.assertTrue(all(target <= 0 for target in self.axis.moves))
            self.assertAlmostEqual(min(self.axis.moves), -65)
            on_sensors = [sensor for sensor, clear, position in self.plc.edges if not clear]
            self.assertEqual(on_sensors, [2, 1, 1, 2] * 5)
            # Auto-turn follows the more negative (last) exit, then runout.
            self.assertTrue(any(abs(target + 52) < 0.2 for target in self.axis.moves))

    def test_negative_encoder_positions_are_accepted(self):
        self.plc.command(1, ProbeConfig())
        for position in (-75, -10, -0.265, 0, 75):
            with self.subTest(position=position):
                self.axis.position = self.axis.target = position
                self.runner.tick()
                self.assertAlmostEqual(self.runner.last_position, position)

    def test_positive_direction_runs_barrier_and_nozzle_series(self):
        for mode in ("barriers", "nozzle"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.setUpScenario()
                self.forces.profile = profile()
                self.forces.tare = {"noise_total_n": 0.0001}
                self.forces.noise_n = 0.0001
                read_steps = self.axis.position_steps
                move_steps = self.axis.move_absolute_steps
                # Same physical fixture, opposite sign of encoder coordinates.
                with patch.object(self.axis, "position_steps", side_effect=lambda: -read_steps()), \
                     patch.object(self.axis, "move_absolute_steps", side_effect=lambda steps: move_steps(-steps)):
                    config = ProbeConfig(mode=mode, outward_direction=1, speed_mm_s=5,
                                         travel_mm=65, max_open_s=40)
                    result = self.runner.run(config, directory)
                self.assertTrue(result["apply_allowed"])
                self.assertAlmostEqual(result["summary"]["mean_mm"], 20, delta=0.15)
                self.assertEqual(result["geometry"]["outward_target_mm"], 65)
                self.assertEqual(result["geometry"]["outward_sensor_order"], [2, 1] if mode == "barriers" else [2])
                if mode == "nozzle":
                    self.assertGreater(result["rows"][0]["peaks"]["force_total_n"]["position_mm"], 0)

    def test_positions_outside_signed_range_still_fail(self):
        self.plc.command(1, ProbeConfig())
        for position in (-75.2, 75.2):
            with self.subTest(position=position):
                self.axis.position = self.axis.target = position
                with self.assertRaisesRegex(MeasurementError, "außerhalb"):
                    self.runner.tick()

    def test_home_can_pass_negative_positions_before_controller_sets_zero(self):
        self.plc.command(1, ProbeConfig())
        started = self.clock.now

        def start_reference():
            self.axis.position = self.axis.target = -0.265
            self.axis.referenced = False

        def finish_reference(plc):
            if self.clock.now - started >= 0.4:
                self.axis.position = self.axis.target = 0
                self.axis.referenced = True

        self.plc.on_snapshot = finish_reference
        with patch.object(self.axis, "reference", side_effect=start_reference):
            self.runner.home()
        self.assertIn(-0.265, self.runner.timeline.positions)
        self.assertEqual(self.runner.last_position, 0)
        self.assertTrue(self.runner.last_status["referenced"])

    def test_negative_position_does_not_bypass_controller_faults(self):
        self.plc.command(1, ProbeConfig())
        self.axis.position = self.axis.target = -0.265
        for fault, message in (({"error_byte": 1}, "Fehler oder Software-Endlage"),
                               ({"software_limit": True}, "Fehler oder Software-Endlage"),
                               ({"rotation_error": True, "system_status_byte": 0x29}, "Drehüberwachung")):
            with self.subTest(fault=fault):
                status = {**self.axis.status(), **fault}
                with patch.object(self.axis, "status", return_value=status):
                    with self.assertRaisesRegex(MeasurementError, message):
                        self.runner.tick()

    def test_completed_home_still_requires_controller_zero(self):
        self.plc.command(1, ProbeConfig())

        def incomplete_reference():
            self.axis.position = self.axis.target = -0.265
            self.axis.referenced = True

        with patch.object(self.axis, "reference", side_effect=incomplete_reference):
            with self.assertRaisesRegex(MeasurementError, "nicht bei 0 mm"):
                self.runner.home()

    def test_five_nozzle_cycles_use_return_reference_and_air_from_home(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
            self.assertTrue(result["apply_allowed"])
            self.assertEqual(self.plc.open_count, 5)
            self.assertEqual(self.plc.open_positions, [0] * 5)
            self.assertEqual(len(result["rows"]), 10)
            self.assertAlmostEqual(result["summary"]["mean_mm"], 20, delta=0.15)
            for forward, backward in zip(result["rows"][::2], result["rows"][1::2]):
                self.assertEqual(forward["reference_position_mm"], backward["reference_position_mm"])
                for row in (forward, backward):
                    self.assertEqual(row["reference_direction"], "backward")
                    self.assertAlmostEqual(row["reference_position_mm"], -30, delta=0.15)
                    self.assertLess(abs(row["peaks"]["force_total_n"]["position_mm"] + 10), 0.15)
                self.assertGreater(forward["reference_edge"]["time_ns"],
                                   forward["peaks"]["force_total_n"]["time_ns"])
                self.assertLess(backward["reference_edge"]["time_ns"],
                                backward["peaks"]["force_total_n"]["time_ns"])
                # Outbound entry is the other optical edge, near -28, and must
                # not become the reference shared by the two nozzle peaks.
                self.assertAlmostEqual(forward["edges"][2]["on"]["position_mm"], -28, delta=0.15)
            self.assertFalse(self.plc.open)
            self.assertAlmostEqual(self.axis.position, 0)
            saved = json.loads((self.runner.recording.path / "session.json").read_text())
            self.assertTrue(saved["apply_allowed"])
            self.assertFalse(saved["accuracy_validated"])
            self.assertNotIn("pending_nozzle_cycle", saved)

    def test_nozzle_series_without_profile_still_requires_tare_and_records_peaks(self):
        self.forces.profile = None
        self.forces.tare = None
        config = ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "tarieren"):
                self.runner.run(config, directory)
            self.assertEqual(self.axis.moves, [])
            self.assertEqual(self.plc.open_count, 0)
            self.forces.tare = {"force_1_n": 0, "force_2_n": 0, "noise_total_n": 0.0001,
                                "zeroed_utc": "2026-10-18T00:00:00+00:00"}
            result = self.runner.run(config, directory)
            self.assertTrue(result["apply_allowed"])
            self.assertIsNone(result["profile"])
            self.assertEqual(result["force_processing"], "tare_only")
            self.assertEqual(len(result["rows"]), 10)
            self.assertAlmostEqual(result["summary"]["mean_mm"], 20, delta=0.15)
            self.assertEqual(self.plc.open_count, 5)
            self.assertFalse(self.plc.open)
            saved = json.loads((self.runner.recording.path / "session.json").read_text())
            self.assertIsNone(saved["profile"])
            self.assertEqual(saved["tare"], self.forces.tare)

    def test_abort_in_home_forward_and_backward_keeps_partial_data(self):
        for phase in ("home", "forward", "backward"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.setUpScenario()
                def abort(plc):
                    if self.runner.direction == phase and (phase == "home" or plc.axis.position < -15):
                        self.cancel.cancelled = True
                self.plc.on_snapshot = abort
                with self.assertRaises(MeasurementError):
                    self.runner.run(ProbeConfig(speed_mm_s=5, travel_mm=65), directory)
                saved = json.loads((self.runner.recording.path / "session.json").read_text())
                self.assertFalse(saved["apply_allowed"])
                self.assertEqual(saved["status"], "failed")
                self.assertFalse(self.plc.open)
                self.assertGreater(self.axis.stops, 0)
                if phase == "backward":
                    self.assertEqual(len(saved["rows"]), 1)

    def test_nozzle_abort_on_return_preserves_provisional_outbound_curve(self):
        def abort(plc):
            if self.runner.direction == "backward" and plc.axis.position > -45:
                self.cancel.cancelled = True

        self.plc.on_snapshot = abort
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "Abgebrochen"):
                self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
            saved = json.loads((self.runner.recording.path / "session.json").read_text())
            self.assertFalse(saved["apply_allowed"])
            self.assertEqual(saved["rows"], [])
            pending = saved["pending_nozzle_cycle"]
            self.assertEqual(pending["reference_direction"], "backward")
            self.assertTrue(pending["forward_curve"])
            self.assertAlmostEqual(pending["forward_peaks"]["force_total_n"]["position_mm"], -10, delta=0.15)
            self.assertFalse(self.plc.open)
            self.assertGreater(self.axis.stops, 0)

    def test_abort_after_home_open_closes_before_first_axis_move(self):
        def abort(plc):
            if plc.open:
                self.cancel.cancelled = True

        self.plc.on_snapshot = abort
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "Abgebrochen"):
                self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
            self.assertEqual(self.plc.open_count, 1)
            self.assertEqual(self.axis.moves, [])
            self.assertFalse(self.plc.open)
            self.assertGreater(self.axis.stops, 0)

    def test_nozzle_requires_air_on_both_traversals_and_does_not_resume(self):
        for phase in ("forward", "backward"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.setUpScenario()
                self.forces.profile = profile()
                self.forces.tare = {"noise_total_n": 0.0001}
                self.forces.noise_n = 0.0001

                def close_early(plc):
                    if self.runner.direction == phase and plc.axis.position < -20:
                        plc.open = False

                self.plc.on_snapshot = close_early
                with self.assertRaisesRegex(MeasurementError, "vor Abschluss"):
                    self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
                self.assertEqual(self.plc.open_count, 1)
                self.assertFalse(self.plc.open)
                self.assertGreater(self.axis.stops, 0)

    def test_nozzle_missing_return_edge_cannot_use_outbound_reference(self):
        # Simulate a dead LB2 on return while preserving a valid outbound signal.
        original_snapshot = self.plc.snapshot

        def snapshot_without_return_event():
            if self.runner.direction == "backward":
                saved = self.plc.history[1]
                result = original_snapshot()
                self.plc.history[1] = saved
                result["history"][1] = saved
                return result
            return original_snapshot()

        with patch.object(self.plc, "snapshot", side_effect=snapshot_without_return_event):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(MeasurementError, "Verfahrgrenze"):
                    self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
                self.assertEqual(self.runner.recording.document["rows"], [])
                self.assertFalse(self.runner.recording.document["apply_allowed"])
                self.assertFalse(self.plc.open)

    def setUpScenario(self):
        self.forces = ForceStream()
        self.axis = Axis(self.clock, self.forces)
        self.plc = Plc(self.clock, self.axis)
        self.axis.air_available = lambda: self.plc.open
        self.cancel = Cancel(self.clock)
        self.runner = ProbeRunner(self.axis, self.plc, self.forces, self.cancel)

    def test_failure_to_pass_both_barriers_stops_at_travel_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "Verfahrgrenze"):
                self.runner.run(ProbeConfig(speed_mm_s=5, travel_mm=20), directory)
            self.assertGreaterEqual(self.axis.position, -20.01)
            self.assertLess(self.axis.position, 0)
            self.assertFalse(self.plc.active)

    def test_runout_cannot_exceed_negative_travel_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "Nachlauf"):
                self.runner.run(ProbeConfig(speed_mm_s=5, travel_mm=51, runout_mm=2), directory)
            self.assertGreaterEqual(self.axis.position, -51.01)
            self.assertTrue(all(target >= -51 for target in self.axis.moves))
            self.assertFalse(self.plc.active)


if __name__ == "__main__":
    unittest.main()
