"""Locate and validate the local QuantumX bridge without connecting any hardware."""
from pathlib import Path
import os
import socket
import time

from probe_calibration import MeasurementError

APP_DIR = Path(__file__).resolve().parent
REQUIRED_DLLS = ("Hbm.Api.Common.dll", "Hbm.Api.QuantumX.dll", "Newtonsoft.Json.dll")
BUILD_OUTPUT = Path("src/QuantumXMonitor/bin/x86/Release/net48/QuantumXMonitor.exe")


def find_bridge(application_dir=None):
    app_dir = Path(application_dir) if application_dir is not None else APP_DIR
    bridge_dir = app_dir / "quantumx_bridge"
    candidates = []
    configured = os.environ.get("BIBAZU_QUANTUMX_MONITOR", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates += [bridge_dir / "runtime/QuantumXMonitor.exe", bridge_dir / BUILD_OUTPUT]
    # A side-by-side clone of the original mini stand is supported as well.
    if len(app_dir.parents) > 1:
        mini = app_dir.parents[1] / "BiBaZu_mini_test_stand/quantumx_bridge"
        candidates += [mini / BUILD_OUTPUT, mini / "runtime/QuantumXMonitor.exe"]
    return next((path.resolve() for path in candidates if path.is_file()), None)


def require_bridge(executable, application_dir=None):
    app_dir = Path(application_dir) if application_dir is not None else APP_DIR
    path = Path(executable).expanduser() if executable else find_bridge(app_dir)
    if path is None:
        raise MeasurementError(
            "Auf diesem PC läuft keine QuantumX-Bridge und es wurde kein Bridge-Programm gefunden.\n\n"
            "Vom bisherigen Mess-PC den vollständigen QuantumXMonitor-Programmordner kopieren "
            "(EXE, DLLs und Unterordner) und über „Bridge wählen …“ die QuantumXMonitor.exe auswählen.\n\n"
            f"Automatisch erkannter Zielordner: {app_dir / 'quantumx_bridge/runtime'}\n\n"
            "Alternativ: quantumx_bridge/Build-QuantumXBridge.ps1 ausführen; dafür werden "
            "die HBM Common API und das .NET SDK benötigt. Siehe docs/COLIBRI_CALIBRATION.md."
        )
    if not path.is_file() or path.name.lower() != "quantumxmonitor.exe":
        raise MeasurementError(f"QuantumXMonitor.exe nicht gefunden: {path}. Über „Bridge wählen …“ den aktuellen Pfad auswählen.")
    missing = [name for name in REQUIRED_DLLS if not (path.parent / name).is_file()]
    if missing:
        raise MeasurementError(
            "Der QuantumXMonitor-Ordner ist unvollständig. Es fehlen: " + ", ".join(missing)
            + ". Den gesamten Programmordner vom alten Mess-PC übernehmen, nicht nur die EXE."
        )
    return path.resolve()


def wait_for_bridge(host, port, process, cancel, timeout_s=15):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise MeasurementError("Start der QuantumX-Bridge abgebrochen.")
        code = process.poll()
        if code is not None:
            raise MeasurementError(f"QuantumXMonitor wurde beim Start beendet (Exitcode {code}). Abhängigkeiten und Bridge-Protokoll prüfen.")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            cancel.wait(0.1)
    raise MeasurementError(f"QuantumXMonitor stellt nach {timeout_s:g} s keinen Server auf {host}:{port} bereit. Bridge-Protokoll prüfen.")
