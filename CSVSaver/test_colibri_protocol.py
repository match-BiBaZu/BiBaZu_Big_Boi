"""Regression checks using status telegrams captured on the actual controller."""
import unittest
from unittest.mock import Mock

from probe_vendor.colibri import ColibriController, ColibriProtocolError


class ColibriStatusTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
