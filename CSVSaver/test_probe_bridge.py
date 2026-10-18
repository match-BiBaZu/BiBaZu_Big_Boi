"""Bridge deployment checks use dummy files and mocked TCP, never real sensors."""
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from probe_bridge import BUILD_OUTPUT, REQUIRED_DLLS, find_bridge, require_bridge, wait_for_bridge
from probe_calibration import MeasurementError


def make_runtime(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"dummy exe, never executed")
    for name in REQUIRED_DLLS:
        (path.parent / name).write_bytes(b"dummy dll")
    return path


class BridgeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"BIBAZU_QUANTUMX_MONITOR": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_runtime_and_local_build_are_found_without_saved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "workspace/big/CSVSaver"
            build = make_runtime(app / "quantumx_bridge" / BUILD_OUTPUT)
            self.assertEqual(find_bridge(app), build.resolve())
            runtime = make_runtime(app / "quantumx_bridge/runtime/QuantumXMonitor.exe")
            self.assertEqual(find_bridge(app), runtime.resolve())
            self.assertEqual(require_bridge("", app), runtime.resolve())

    def test_side_by_side_mini_stand_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            app = workspace / "BiBaZu_Big_Boi/CSVSaver"
            exe = make_runtime(workspace / "BiBaZu_mini_test_stand/quantumx_bridge" / BUILD_OUTPUT)
            self.assertEqual(find_bridge(app), exe.resolve())

    def test_missing_bridge_explains_transfer_from_old_pc(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MeasurementError, "vollständigen QuantumXMonitor-Programmordner"):
                require_bridge("", Path(directory) / "app")

    def test_copying_only_executable_reports_missing_runtime_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "QuantumXMonitor.exe"
            exe.write_bytes(b"dummy")
            with self.assertRaisesRegex(MeasurementError, "Hbm.Api.Common.dll"):
                require_bridge(str(exe))

    def test_explicit_invalid_path_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "app"
            make_runtime(app / "quantumx_bridge/runtime/QuantumXMonitor.exe")
            with self.assertRaisesRegex(MeasurementError, "nicht gefunden"):
                require_bridge(str(app / "missing/QuantumXMonitor.exe"), app)

    def test_environment_path_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = make_runtime(Path(directory) / "elsewhere/QuantumXMonitor.exe")
            with patch.dict(os.environ, {"BIBAZU_QUANTUMX_MONITOR": str(exe)}):
                self.assertEqual(find_bridge(Path(directory) / "app"), exe.resolve())

    def test_startup_wait_accepts_listening_server(self):
        process = Mock()
        process.poll.return_value = None
        context = Mock()
        context.__enter__ = Mock()
        context.__exit__ = Mock()
        with patch("probe_bridge.socket.create_connection", return_value=context):
            wait_for_bridge("127.0.0.1", 5500, process, Event())

    def test_startup_exit_cancel_and_timeout_are_reported(self):
        process = Mock()
        process.poll.return_value = 23
        with self.assertRaisesRegex(MeasurementError, "Exitcode 23"):
            wait_for_bridge("127.0.0.1", 5500, process, Event())
        process.poll.return_value = None
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(MeasurementError, "abgebrochen"):
            wait_for_bridge("127.0.0.1", 5500, process, cancel)
        with patch("probe_bridge.socket.create_connection", side_effect=OSError("refused")):
            with self.assertRaisesRegex(MeasurementError, "keinen Server"):
                wait_for_bridge("127.0.0.1", 5500, process, Event(), timeout_s=0.01)


if __name__ == "__main__":
    unittest.main()
