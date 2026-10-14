"""HTTP/Qt/ADS integration with a memory PLC; never opens an ADS connection."""

import copy
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PressureControlGUI as gui
from fastapi.testclient import TestClient
from local_api_runtime import ApiError, SessionLease
from pressure_api import Parameters, PressureApi
from PyQt6.QtWidgets import QApplication


def parameters():
    return {
        "conveyor_speed_mm_per_sec": 80.0,
        "arrays": [
            {
                "index": index,
                "enabled": index == 2,
                "nozzles_enabled": [True, True, True, False, False, False],
                "pressure_mbar": 2300,
                "delay_ms": 0,
                "pulse_duration_ms": 50,
                "offset_mm": 10.0,
                "conveyor_velocity_delay": True,
            }
            for index in range(1, 5)
        ],
    }


class MemoryPlc:
    def __init__(self):
        self.values = {"MAIN.GuiConveyorSpeedMmPerSec": 40.0}
        self.writes = []
        self.error = False
        self.mismatch = False

    def read_list_by_name(self, names, **kwargs):
        return {
            name: self.values.get(name, 100 if "PulseDuration" in name else 0) for name in names
        }

    def read_by_name(self, name, *_):
        return self.values.get(name, 0)

    def write_list_by_name(self, values, **kwargs):
        self.writes.append(dict(values))
        for name, value in values.items():
            if not self.mismatch or "Pressure" not in name:
                self.values[name] = value
        return {name: "write failed" if self.error else "no error" for name in values}

    def close(self):
        pass


class TestPressureApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(gui.AdsController, "start"):
            self.window = gui.PressureControlWindow()
        self.plc = MemoryPlc()
        self.window.ads.worker.client.plc = self.plc
        self.window.ads.connected = True
        self.window.selected_roadmap_transition = object()
        self.api = PressureApi(self.window)
        self.api.context = lambda: {"roadmap_transition": {"part_name": "Dk1i"}}
        self.window.ads.thread.started.disconnect(self.window.ads.worker.start)
        self.window.ads.thread.start()
        self.client = TestClient(self.api.app)
        self.pool = ThreadPoolExecutor(1)

    def tearDown(self):
        self.api.lease.invalidate()
        self.api.timer.stop()
        self.window.ads.thread.quit()
        self.assertTrue(self.window.ads.thread.wait(2000))
        self.window.ads.worker.client.plc = None
        self.window.ads.connected = False
        self.window.ur_angle.shutdown()
        self.pool.shutdown()
        self.client.close()
        self.window.deleteLater()
        self.qt.processEvents()

    def request(self, method, path, **kwargs):
        future = self.pool.submit(self.client.request, method, "/api/v1/" + path, **kwargs)
        deadline = time.monotonic() + 4
        while not future.done() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(0.001)
        return future.result(1)

    def operation(self, operation_id):
        for _ in range(100):
            result = self.request("GET", f"operations/{operation_id}").json()
            if result["status"] not in {"pending", "awaiting_user"}:
                return result
            time.sleep(0.001)
        self.fail("operation did not complete")

    def acquire(self):
        response = self.request("POST", "sessions", json={"client": "test"})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(self.operation(data["snapshot_operation_id"])["status"], "succeeded")
        return {"X-Session-Id": data["session_id"]}

    def test_complete_write_readback_locks_and_never_starts_band(self):
        headers = self.acquire()
        self.assertFalse(self.window.rows[0].pressure.isEnabled())
        self.assertTrue(self.window.conveyor_enabled.isEnabled())
        response = self.request("PUT", "parameters", json=parameters(), headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        operation = self.operation(response.json()["operation_id"])
        self.assertEqual(operation["status"], "succeeded", operation)
        self.assertEqual(self.window.rows[1].pressure.value(), 2300)
        self.assertEqual(self.window.conveyor_speed.value(), 80)
        written = self.plc.writes[-1]
        self.assertNotIn("MAIN.GuiConveyorEnabled", written)
        self.assertNotIn("MAIN.GuiConveyorReverse", written)
        state = self.request("GET", "state").json()
        self.assertEqual(state["parameters"]["arrays"][1]["pressure_mbar"], 2300)
        self.assertEqual(self.request("POST", "sessions", json={}).status_code, 409)
        self.assertEqual(
            self.request("DELETE", "sessions/current", headers=headers).status_code, 200
        )
        self.assertTrue(self.window.rows[0].pressure.isEnabled())

    def test_invalid_parameters_have_no_writes(self):
        headers = self.acquire()
        for field, invalid in (
            ("pressure_mbar", 6001),
            ("pulse_duration_ms", 0),
            ("delay_ms", -1),
            ("offset_mm", 5001),
        ):
            candidate = parameters()
            candidate["arrays"][0][field] = invalid
            self.assertEqual(
                self.request("PUT", "parameters", json=candidate, headers=headers).status_code, 422
            )
        for candidate in (parameters(), parameters()):
            candidate["arrays"][0]["index"] = 2
            self.assertEqual(
                self.request("PUT", "parameters", json=candidate, headers=headers).status_code, 422
            )
        candidate = parameters()
        candidate["arrays"][0]["nozzles_enabled"] = [True]
        self.assertEqual(
            self.request("PUT", "parameters", json=candidate, headers=headers).status_code, 422
        )
        self.assertEqual(self.plc.writes, [])

    def test_running_conveyor_partial_write_and_mismatch_fail(self):
        headers = self.acquire()
        for mode in ("running", "partial", "mismatch"):
            self.plc.values.clear()
            self.plc.values["MAIN.GuiConveyorEnabled"] = mode == "running"
            self.plc.error, self.plc.mismatch = mode == "partial", mode == "mismatch"
            response = self.request("PUT", "parameters", json=parameters(), headers=headers)
            result = self.operation(response.json()["operation_id"])
            self.assertEqual(result["status"], "failed", result)
            self.assertIn("error", result)

    def test_expired_queued_operation_does_not_write(self):
        headers = self.acquire()
        self.api.lease.invalidate()
        result = []
        self.window.ads.worker.api_finished.connect(result.append)
        self.window.ads.worker.api_command(
            {
                "id": "expired",
                "kind": "parameters",
                "parameters": parameters(),
                "lease": self.api.lease,
                "token": headers["X-Session-Id"],
            }
        )
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(self.plc.writes, [])

    def test_origin_and_dns_rebinding_rejected(self):
        self.assertEqual(
            self.request(
                "POST", "sessions", json={}, headers={"Origin": "https://example.org"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.request("GET", "health", headers={"Host": "example.org"}).status_code, 403
        )

    def test_save_cancel_and_filesystem_failure_are_distinct(self):
        headers = self.acquire()
        with patch.object(self.window, "save_profile", return_value=None):
            response = self.request("POST", "profile/save-dialog", headers=headers)
            self.assertEqual(self.operation(response.json()["operation_id"])["status"], "cancelled")
        with patch.object(self.window, "save_profile", side_effect=OSError("folder unavailable")):
            response = self.request("POST", "profile/save-dialog", headers=headers)
            result = self.operation(response.json()["operation_id"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("folder unavailable", result["error"])

    def test_offline_heartbeat_invalidates_session_and_schema_is_typed(self):
        headers = self.acquire()
        self.window.ads.connected = False
        self.assertEqual(
            self.request("POST", "sessions/heartbeat", headers=headers).status_code, 503
        )
        self.assertFalse(self.api.lease.active())
        schema = self.client.get("/openapi.json").json()
        self.assertIn("Parameters", schema["components"]["schemas"])
        self.assertIn("Operation", schema["components"]["schemas"])


class TestSessionAndModels(unittest.TestCase):
    def test_expiry_heartbeat_and_new_token(self):
        now = [0]
        lease = SessionLease(clock=lambda: now[0])
        first = lease.acquire()["session_id"]
        now[0] = 10
        lease.heartbeat(first)
        now[0] = 26
        with self.assertRaises(ApiError):
            lease.require(first)
        second = lease.acquire()["session_id"]
        self.assertNotEqual(first, second)
        with self.assertRaises(ApiError):
            lease.require(first)

    def test_parameter_boundaries_and_nonfinite(self):
        candidate = parameters()
        for index, array in enumerate(candidate["arrays"]):
            array.update(
                pressure_mbar=0 if index % 2 else 6000,
                delay_ms=0 if index % 2 else 1000,
                pulse_duration_ms=1 if index % 2 else 500,
                offset_mm=0.0 if index % 2 else 5000.0,
            )
        Parameters.model_validate(candidate)
        for invalid in (float("nan"), float("inf"), "10", True):
            invalid_candidate = copy.deepcopy(candidate)
            invalid_candidate["arrays"][0]["offset_mm"] = invalid
            with self.assertRaises(ValueError):
                Parameters.model_validate(invalid_candidate)
