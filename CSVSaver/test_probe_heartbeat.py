"""Small heartbeat checks with dummy ADS connections; never accesses hardware."""
import queue
import threading
import unittest
from unittest.mock import Mock

from probe_calibration import MeasurementError
from probe_heartbeat import ProbeHeartbeat


class HeartbeatTests(unittest.TestCase):
    def test_writes_continue_while_measurement_worker_is_blocked_and_stop_on_close(self):
        writes = queue.Queue()
        connection = Mock()
        connection.write_by_name.side_effect = lambda name, value: writes.put((name, value))
        task = ProbeHeartbeat(lambda: connection, interval_s=0.01)
        try:
            task.start()
            first = writes.get(timeout=1)
            # No service call: represents time spent waiting for a serial response.
            second = writes.get(timeout=1)
            self.assertEqual(first[0], "MAIN.GuiProbeCalibrationHeartbeat")
            self.assertGreater(second[1], first[1])
            task.service()
        finally:
            task.stop()
        self.assertFalse(task.thread.is_alive())
        connection.close.assert_called_once()

    def test_stalled_worker_and_failed_force_guard_stop_heartbeats(self):
        for failure in ("worker", "force"):
            with self.subTest(failure=failure):
                connection = Mock()
                invalid_force = threading.Event()

                def guard():
                    if invalid_force.is_set():
                        raise MeasurementError("Kraftdaten fehlen")

                task = ProbeHeartbeat(lambda: connection, guard, interval_s=0.005,
                                      progress_timeout_s=0.1 if failure == "worker" else 2.5)
                try:
                    task.start()
                    if failure == "force":
                        invalid_force.set()
                    task.thread.join(timeout=1)
                    self.assertFalse(task.thread.is_alive())
                    with self.assertRaisesRegex(MeasurementError, "reagiert" if failure == "worker" else "Kraftdaten"):
                        task.service()
                finally:
                    task.stop()
                connection.close.assert_called_once()
