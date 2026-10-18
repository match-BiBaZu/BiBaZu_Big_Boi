"""PLC heartbeat on its own ADS connection, bounded by measurement-worker health."""
import threading
import time

from probe_calibration import MASK, MeasurementError


class ProbeHeartbeat:
    def __init__(self, connection_factory, safety_check=None, initial_value=0,
                 interval_s=0.1, progress_timeout_s=2.5):
        self.connection_factory = connection_factory
        self.safety_check = safety_check
        self.value = initial_value
        self.interval_s = interval_s
        self.progress_timeout_s = progress_timeout_s
        self.last_progress = time.monotonic()
        self.failure = None
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="probe-heartbeat", daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            self.stop()
            raise MeasurementError("SPS-Heartbeat-Verbindung startet nicht.")
        self.check()

    def check(self):
        if self.failure:
            raise MeasurementError(f"SPS-Heartbeat unterbrochen: {self.failure}")

    def service(self):
        self.check()
        self.last_progress = time.monotonic()

    def _run(self):
        connection = None
        try:
            connection = self.connection_factory()
            connection.open()
            connection.set_timeout(250)
            while not self.stopped.is_set():
                if time.monotonic() - self.last_progress > self.progress_timeout_s:
                    raise MeasurementError(f"Messablauf reagiert seit mehr als {self.progress_timeout_s:g} s nicht.")
                if self.safety_check:
                    self.safety_check()
                self.value = (self.value + 1) & MASK
                connection.write_by_name("MAIN.GuiProbeCalibrationHeartbeat", self.value)
                self.ready.set()
                if self.stopped.wait(self.interval_s):
                    break
        except Exception as exc:
            self.failure = str(exc)
        finally:
            self.ready.set()
            if connection:
                connection.close()

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

