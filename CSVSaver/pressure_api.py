"""Pressure Control's public API; all adapter methods execute on the Qt thread."""

from __future__ import annotations

import copy
import os
import subprocess
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Header
from local_api_runtime import (
    ApiError,
    LocalApiServer,
    QtExecutor,
    SessionLease,
    StrictModel,
    create_app,
)
from pydantic import BaseModel, Field, model_validator
from PyQt6.QtCore import QObject, QSignalBlocker, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QMessageBox, QPushButton
from roadmap_transition_dialog import roadmap_transition_metadata


class ArrayParameters(StrictModel):
    index: Annotated[int, Field(ge=1, le=4)]
    enabled: bool
    nozzles_enabled: Annotated[list[bool], Field(min_length=6, max_length=6)]
    pressure_mbar: Annotated[int, Field(ge=0, le=6000)]
    pulse_duration_ms: Annotated[int, Field(ge=1, le=500)]
    delay_ms: Annotated[int, Field(ge=0, le=1000)]
    offset_mm: Annotated[float, Field(ge=0, le=5000)]
    conveyor_velocity_delay: bool


class Parameters(StrictModel):
    arrays: Annotated[list[ArrayParameters], Field(min_length=4, max_length=4)]
    conveyor_speed_mm_per_sec: Annotated[float, Field(ge=0, le=200)]

    @model_validator(mode="after")
    def check_arrays(self):
        if sorted(a.index for a in self.arrays) != [1, 2, 3, 4]:
            raise ValueError("Genau die Arrays 1 bis 4 sind erforderlich.")
        if self.conveyor_speed_mm_per_sec == 0 and any(
            a.enabled and a.conveyor_velocity_delay for a in self.arrays
        ):
            raise ValueError("Offset mit Bandgeschwindigkeit benötigt mehr als 0 mm/s.")
        return self


class PressureContext(BaseModel):
    roadmap_transition: dict | None = None
    profile_path: str | None = None


class PressureState(PressureContext):
    api_version: int
    connected: bool
    session_active: bool
    parameters: Parameters | None = None
    telemetry: dict | None = None


class Operation(BaseModel):
    operation_id: str
    status: Literal["pending", "succeeded", "failed", "awaiting_user", "cancelled"]
    kind: Literal["parameters", "read", "save"]
    error: str | None = None
    snapshot: dict | None = None
    profile_path: str | None = None
    previous_profile_path: str | None = None


def snapshot_parameters(snapshot):
    return {
        "arrays": snapshot["arrays"],
        "conveyor_speed_mm_per_sec": snapshot["conveyor"]["speed_mm_per_sec"],
    }


class PressureApi(QObject):
    work_requested = pyqtSignal(object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.executor = QtExecutor(self)
        self.lease = SessionLease()
        self.owned = False
        self.operations = {}
        self.pending = None
        self.disabled = {}
        self.snapshot = None
        self.live = None
        self.server = None
        window.ads.initial_snapshot_ready.connect(self.remember)
        window.ads.live_snapshot_ready.connect(self.remember_live)
        window.ads.write_finished.connect(lambda _: self.refresh())
        window.ads.connection_changed.connect(self.connection_changed)
        window.ads.worker.api_finished.connect(self.finished)
        self.work_requested.connect(window.ads.worker.api_command)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.expire)
        self.timer.start(250)
        self.app = create_app(self, "BiBaZu Pressure Control API", state_model=PressureState)
        self.routes()

    def invoke(self, callback):
        return self.executor.call(callback)

    def health(self):
        return {"api_version": 1, "connected": self.window.ads.is_connected}

    @pyqtSlot(bool, str)
    def connection_changed(self, connected, message):
        if not connected:
            self.lease.invalidate()
            self.expire()

    def heartbeat(self, token):
        if not self.window.ads.is_connected:
            self.lease.invalidate()
            raise ApiError(503, "ADS offline.")
        return self.lease.heartbeat(token)

    def capabilities(self):
        return {
            "api_version": 1,
            "array_count": 4,
            "nozzles_per_array": 6,
            "limits": {
                "pressure_mbar": [0, 6000],
                "pulse_duration_ms": [1, 500],
                "delay_ms": [0, 1000],
                "offset_mm": [0, 5000],
                "conveyor_speed_mm_per_sec": [0, 200],
            },
            "parameters_schema": Parameters.model_json_schema(),
            "conveyor_start": False,
        }

    def context(self):
        selection = self.window.selected_roadmap_transition
        return {
            "roadmap_transition": roadmap_transition_metadata(selection) if selection else None,
            "profile_path": str(self.window.profile_path) if self.window.profile_path else None,
        }

    def state(self):
        # The cached settings are explicitly distinguished from current live telemetry.
        return {
            **self.health(),
            **self.context(),
            "session_active": self.lease.active(),
            "parameters": snapshot_parameters(copy.deepcopy(self.snapshot))
            if self.snapshot
            else None,
            "telemetry": copy.deepcopy(self.live),
        }

    @pyqtSlot(object)
    def remember(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)

    @pyqtSlot(object)
    def remember_live(self, snapshot):
        self.live = copy.deepcopy(snapshot)

    def acquire(self, client):
        self.expire()
        if self.pending:
            raise ApiError(409, "Eine Operation läuft noch.")
        if not self.window.ads.is_connected:
            raise ApiError(503, "ADS offline.")
        if not self.window.selected_roadmap_transition:
            raise ApiError(409, "Zuerst eine Roadmap-Transition auswählen.")
        worker = self.window.ads.worker
        if self.window.ur_angle.busy:
            raise ApiError(409, "Die UR-Winkelverstellung läuft noch.")
        if worker.calibration_polling or worker.setup_polling or worker.force_delay_polling:
            raise ApiError(409, "Eine Kalibrierung oder Messung läuft.")
        result = self.lease.acquire()
        self.window.ads.flush_writes()
        self.owned = True
        w = self.window
        controls = [
            w.load_button,
            w.save_button,
            w.write_all_button,
            w.load_roadmap_button,
            w.calibrate_conveyor_button,
            w.jog_conveyor_button,
            w.force_delay_settings_button,
            w.light_barrier_settings_button,
            w.conveyor_speed,
            w.set_conveyor_speed_button,
            w.conveyor_reverse,
            w.ur_angle_input,
            w.apply_ur_angle_button,
        ]
        for row in w.rows:
            controls.extend(
                [
                    row.enabled,
                    row.nozzle_controls,
                    row.pressure,
                    row.delay,
                    row.pulse_duration,
                    row.offset,
                    row.conveyor_velocity_delay,
                ]
            )
        self.disabled = {c: c.isEnabled() for c in controls}
        for control in controls:
            control.setEnabled(False)
        w.statusBar().showMessage(f"AutoTune-Sitzung: {client} · Band weiterhin manuell")
        operation = self.submit(result["session_id"], "read")
        result["snapshot_operation_id"] = operation["operation_id"]
        return result

    def refresh(self):
        if self.window.ads.is_connected and not self.pending:
            self.work_requested.emit({"id": "refresh", "kind": "read", "lease": None})

    def expire(self):
        if self.owned and not self.lease.active():
            self.owned = False
            for control, enabled in self.disabled.items():
                control.setEnabled(enabled)
            self.disabled.clear()
            self.window.statusBar().showMessage("AutoTune-Sitzung beendet")
            if self.pending and self.operations[self.pending]["status"] == "awaiting_user":
                from PyQt6.QtWidgets import QApplication, QFileDialog

                dialog = QApplication.activeModalWidget()
                if isinstance(dialog, QFileDialog):
                    dialog.reject()

    def submit(self, token, kind, parameters=None):
        self.lease.require(token)
        if self.pending:
            raise ApiError(409, "Eine Operation läuft bereits.")
        operation_id = uuid.uuid4().hex
        operation = {"operation_id": operation_id, "status": "pending", "kind": kind}
        self.operations[operation_id] = operation
        # Bound completed operation history without discarding the active one.
        if len(self.operations) > 256:
            self.operations.pop(next(iter(self.operations)))
        self.pending = operation_id
        self.work_requested.emit(
            {
                "id": operation_id,
                "kind": kind,
                "parameters": parameters,
                "lease": self.lease,
                "token": token,
            }
        )
        return dict(operation)

    @pyqtSlot(object)
    def finished(self, result):
        if result.get("snapshot"):
            self.remember(result["snapshot"])
        operation = self.operations.get(result["operation_id"])
        if operation is None:
            return
        operation.update(result)
        self.pending = None
        if result.get("snapshot"):
            for row, values in zip(self.window.rows, result["snapshot"]["arrays"], strict=True):
                row.set_values(values)
            with QSignalBlocker(self.window.conveyor_speed):
                self.window.conveyor_speed.setValue(
                    result["snapshot"]["conveyor"]["speed_mm_per_sec"]
                )
        if operation["kind"] == "save" and operation["status"] == "succeeded":
            operation["status"] = "awaiting_user"
            self.pending = operation["operation_id"]
            QTimer.singleShot(0, lambda: self.save_dialog(operation))

    def save_dialog(self, operation):
        if not self.lease.active():
            operation.update(status="failed", error="Sitzung vor Speicherdialog beendet.")
            self.pending = None
            return
        before = self.window.profile_path
        try:
            saved = self.window.save_profile()
        except Exception as exc:
            operation.update(status="failed", error=str(exc))
            self.pending = None
            return
        if getattr(self.window, "profile_save_error", None):
            operation.update(status="failed", error=self.window.profile_save_error)
            self.pending = None
            return
        operation.update(
            status="succeeded" if saved else "cancelled",
            profile_path=(str(self.window.profile_path) if self.window.profile_path else None),
        )
        # A cancelled dialog leaves the previous profile unchanged.
        operation["previous_profile_path"] = str(before) if before else None
        self.pending = None

    def routes(self):
        app, invoke = self.app, self.app.state.invoke

        @app.get("/api/v1/context", response_model=PressureContext)
        def context():
            return invoke(self.context)

        @app.put("/api/v1/parameters", status_code=202, response_model=Operation)
        def parameters(body: Parameters, x_session_id: str = Header()):
            return invoke(lambda: self.submit(x_session_id, "parameters", body.model_dump()))

        @app.get("/api/v1/operations/{operation_id}", response_model=Operation)
        def operation(operation_id: str):
            def get():
                if operation_id not in self.operations:
                    raise ApiError(404, "Operation nicht gefunden.")
                return copy.deepcopy(self.operations[operation_id])

            return invoke(get)

        @app.post("/api/v1/profile/save-dialog", status_code=202, response_model=Operation)
        def save(x_session_id: str = Header()):
            return invoke(lambda: self.submit(x_session_id, "save"))

    def start(self):
        if os.environ.get("BIBAZU_API_DISABLED") == "1":
            return
        try:
            self.server = LocalApiServer(
                self.app, int(os.environ.get("BIBAZU_PRESSURE_API_PORT", "8765"))
            )
            self.server.start()
        except (ValueError, OSError) as exc:
            self.window.statusBar().showMessage(f"Pressure API konnte nicht starten: {exc}")

    def stop(self):
        self.lease.invalidate()
        self.timer.stop()
        self.expire()
        if self.server:
            self.server.stop()


def launch_autotune(window):
    api = window.control_api
    if not window.selected_roadmap_transition:
        QMessageBox.information(window, "AutoTune", "Zuerst eine Roadmap-Transition auswählen.")
        return
    if not api.server or not api.server.thread or not api.server.thread.is_alive():
        QMessageBox.warning(window, "AutoTune", "Die Pressure-Control-API läuft nicht.")
        return
    repo = Path(__file__).resolve().parents[2] / "BiBaZu_AutoTune"
    python = Path(os.environ.get("BIBAZU_AUTOTUNE_PYTHON", str(repo / ".venv/Scripts/pythonw.exe")))
    if not python.is_file():
        QMessageBox.warning(
            window, "AutoTune", f"AutoTune zuerst installieren: {repo / 'Setup.ps1'}"
        )
        return
    try:
        subprocess.Popen(
            [
                str(python),
                "-m",
                "bibazu_autotune",
                "--pressure-url",
                f"http://127.0.0.1:{api.server.port}",
                "--vision-url",
                f"http://127.0.0.1:{os.environ.get('BIBAZU_VISION_API_PORT', '8766')}",
            ],
            cwd=repo,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        QMessageBox.warning(window, "AutoTune", str(exc))


def install(window):
    window.control_api = PressureApi(window)
    button = QPushButton("AutoTune öffnen", window)
    button.clicked.connect(lambda: launch_autotune(window))
    window.statusBar().addPermanentWidget(button)
    window.control_api.start()
