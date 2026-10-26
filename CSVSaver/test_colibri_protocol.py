"""Regression checks using status telegrams captured on the actual controller."""
import unittest
import threading
from unittest.mock import Mock, patch

from probe_vendor.colibri import ColibriController, ColibriProtocolError


class TimedSerial:
    def __init__(self, respond):
        self.now = 0.0
        self.respond = respond
        self.incoming = bytearray()
        self.writes = []

    def reset_input_buffer(self):
        self.incoming.clear()

    def write(self, frame):
        self.writes.append(frame)
        self.incoming.extend(self.respond(len(self.writes), frame))

    def flush(self):
        pass

    def read(self, count):
        self.now += 0.001 if self.incoming else 0.02
        value = bytes(self.incoming[:count])
        del self.incoming[:count]
        return value

    def sleep(self, seconds):
        self.now += seconds


class ColibriStatusTests(unittest.TestCase):
    def axis_with_serial(self, respond):
        axis = ColibriController.__new__(ColibriController)
        axis.slave_address = 0xFF
        axis.lock = threading.Lock()
        axis.debug_logger = None
        axis.serial_port = TimedSerial(respond)
        return axis

    @staticmethod
    def status_frame():
        raw = bytes.fromhex("ff 05 80 08 78 00 04")
        axis = ColibriController.__new__(ColibriController)
        return bytes([4]) + axis._escape(raw) + bytes([5])

    def test_captured_motion_reference_and_rotation_fault_frames(self):
        axis = ColibriController.__new__(ColibriController)
        for frame, moving, referenced, rotation_error in (
            ("ff 05 80 08 79 00 05", True, True, False),
            ("ff 05 80 2c 69 00 19", True, False, False),
            ("ff 05 80 08 78 00 04", False, True, False),
            ("ff 05 80 29 78 00 25", False, True, True),
        ):
            with self.subTest(frame=frame):
                response = bytes.fromhex(frame)
                axis._validate_response(response)
                axis._request = Mock(return_value=response)
                status = axis.status()
                self.assertEqual(status["moving"], moving)
                self.assertEqual(status["referenced"], referenced)
                self.assertEqual(status["rotation_error"], rotation_error)
                self.assertTrue(status["remote"])
                self.assertTrue(status["enabled"])

    def test_truncated_status_is_rejected(self):
        axis = ColibriController.__new__(ColibriController)
        axis._request = Mock(return_value=bytes.fromhex("ff 04 80 78 00 fb"))
        with self.assertRaises(ColibriProtocolError):
            axis.status()

    def test_lost_poll_response_is_retried_without_a_one_second_stall(self):
        axis = self.axis_with_serial(lambda attempt, frame: b"" if attempt == 1 else self.status_frame())
        with patch("probe_vendor.colibri.time.monotonic", side_effect=lambda: axis.serial_port.now), \
                patch("probe_vendor.colibri.time.sleep", side_effect=axis.serial_port.sleep):
            self.assertTrue(axis.status()["referenced"])
        self.assertEqual(len(axis.serial_port.writes), 2)
        self.assertLess(axis.serial_port.now, 0.25)

    def test_motion_command_is_never_repeated_after_missing_ack(self):
        axis = self.axis_with_serial(lambda attempt, frame: b"")
        with patch("probe_vendor.colibri.time.monotonic", side_effect=lambda: axis.serial_port.now), \
                patch("probe_vendor.colibri.time.sleep", side_effect=axis.serial_port.sleep):
            with self.assertRaises(TimeoutError):
                axis.move_relative_steps(200)
        self.assertEqual(len(axis.serial_port.writes), 1)

    def test_echo_and_invalid_length_do_not_acknowledge_a_move(self):
        malformed = bytes.fromhex("ff 04 80 08 78 00 03")
        encoder = ColibriController.__new__(ColibriController)
        invalid_frame = bytes([4]) + encoder._escape(malformed) + bytes([5])
        axis = self.axis_with_serial(lambda attempt, frame: frame + invalid_frame + self.status_frame())
        with patch("probe_vendor.colibri.time.monotonic", side_effect=lambda: axis.serial_port.now):
            response = axis.move_relative_steps(200)
        self.assertEqual(response, bytes.fromhex("ff 05 80 08 78 00 04"))


if __name__ == "__main__":
    unittest.main()
