"""Complete calibration runs against a simulated serial axis, optics and force source."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probe_calibration import MeasurementError, ProbeConfig
from probe_hardware import ForceStream, ProbeRunner
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
        self.sample()

    def sample(self):
        total = 0.1 * math.exp(-((self.position - 50) / 2) ** 2)
        self.sequence += 1
        self.forces.receive(force_sample(self.sequence, self.clock.time_ns(), total * 0.6, total * 0.4))

    def position_steps(self):
        self.clock.sleep(0.02)
        distance = min(abs(self.target - self.position), self.speed * 0.025)
        self.position += distance if self.target > self.position else -distance
        self.sample()
        return round(self.position / 0.005)

    def status(self):
        return {"moving": abs(self.target - self.position) > 0.001,
                "referenced": self.referenced, "error_byte": 0, "software_limit": False}

    def set_parameter(self, index, subindex, value, size):
        if (index, subindex) == (3, 2):
            self.speed = value * 0.5

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

    def pulse_heartbeat(self):
        pass

    def snapshot(self):
        stamp = round(self.clock.now * 1000) & 0xFFFFFFFF
        self.history[2] = stamp
        for sensor, start in ((1, 10), (2, 30)):
            clear = not start <= self.axis.position <= start + 2
            if clear != self.clear[sensor]:
                self.clear[sensor] = clear
                self.history[1] += 1
                slot = 3 + (self.history[1] % 256) * 3
                self.history[slot:slot + 3] = [self.history[1], stamp, sensor * 2 + int(clear)]
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

    def test_negative_encoder_positions_are_accepted(self):
        self.plc.command(1, ProbeConfig())
        for position in (-75, -10, -0.265, 0, 75):
            with self.subTest(position=position):
                self.axis.position = self.axis.target = position
                self.runner.tick()
                self.assertAlmostEqual(self.runner.last_position, position)

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
        for fault in ({"error_byte": 1}, {"software_limit": True}):
            with self.subTest(fault=fault):
                status = {**self.axis.status(), **fault}
                with patch.object(self.axis, "status", return_value=status):
                    with self.assertRaisesRegex(MeasurementError, "Fehler oder Software-Endlage"):
                        self.runner.tick()

    def test_completed_home_still_requires_controller_zero(self):
        self.plc.command(1, ProbeConfig())

        def incomplete_reference():
            self.axis.position = self.axis.target = -0.265
            self.axis.referenced = True

        with patch.object(self.axis, "reference", side_effect=incomplete_reference):
            with self.assertRaisesRegex(MeasurementError, "nicht bei 0 mm"):
                self.runner.home()

    def test_five_nozzle_cycles_keep_same_forward_reference_and_close_each_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.runner.run(ProbeConfig(mode="nozzle", speed_mm_s=5, travel_mm=65, max_open_s=40), directory)
            self.assertTrue(result["apply_allowed"])
            self.assertEqual(self.plc.open_count, 5)
            self.assertEqual(len(result["rows"]), 10)
            self.assertAlmostEqual(result["summary"]["mean_mm"], 20, delta=0.15)
            for forward, backward in zip(result["rows"][::2], result["rows"][1::2]):
                self.assertEqual(forward["reference_position_mm"], backward["reference_position_mm"])
                self.assertLess(abs(forward["peaks"]["force_total_n"]["position_mm"] - 50), 0.15)
            self.assertFalse(self.plc.open)
            self.assertAlmostEqual(self.axis.position, 0)
            saved = json.loads((self.runner.recording.path / "session.json").read_text())
            self.assertTrue(saved["apply_allowed"])
            self.assertFalse(saved["accuracy_validated"])

    def test_abort_in_home_forward_and_backward_keeps_partial_data(self):
        for phase in ("home", "forward", "backward"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.setUpScenario()
                def abort(plc):
                    if self.runner.direction == phase and (phase == "home" or plc.axis.position > 15):
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

    def setUpScenario(self):
        self.forces = ForceStream()
        self.axis = Axis(self.clock, self.forces)
        self.plc = Plc(self.clock, self.axis)
        self.cancel = Cancel(self.clock)
        self.runner = ProbeRunner(self.axis, self.plc, self.forces, self.cancel)

    def test_failure_to_pass_both_barriers_stops_at_travel_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "Verfahrgrenze"):
                self.runner.run(ProbeConfig(speed_mm_s=5, travel_mm=20), directory)
            self.assertLessEqual(self.axis.position, 20.01)
            self.assertFalse(self.plc.active)


if __name__ == "__main__":
    unittest.main()
