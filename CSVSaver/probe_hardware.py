"""Serialized device I/O and cooperative calibration runner (never runs on Qt thread)."""
from __future__ import annotations

import json
import math
import queue
import socket
import statistics
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from probe_calibration import (
    MASK, CYCLES, AXIS_POSITION_MIN_MM, AXIS_POSITION_MAX_MM,
    BarrierHistory, EdgeCapture, MeasurementError, PositionTimeline,
    ProbeConfig, ProbeRecording, barrier_row, force_peak, persist_spacing,
)
from probe_vendor.colibri import ColibriController
from probe_vendor.force_sources import QuantumXTcpClient
from probe_vendor.platform_calibration import PlatformCalibration
from probe_bridge import require_bridge, wait_for_bridge

PREFIX = "MAIN."
PROBE_FIELDS = ("ProbeCalibrationInterfaceVersion", "ProbeCalibrationAckSequence",
                "ProbeCalibrationActive", "ProbeCalibrationArmed", "ProbeCalibrationValveOpen",
                "ProbeCalibrationStatus", "ProbeCalibrationFaultCode", "ProbeCalibrationOpenedAtMs")


class ProbePlc:
    def __init__(self, net_id, ip, port):
        import pyads
        self.connection = pyads.Connection(net_id, port, ip)
        self.connection.open()
        self.connection.set_timeout(500)
        self.sequence = 0
        self.heartbeat = 0
        self.owned = False
        self.last_heartbeat = 0.0

    def read(self, names):
        return self.connection.read_list_by_name(names, cache_symbol_info=True)

    def write(self, values):
        errors = self.connection.write_list_by_name(values, cache_symbol_info=True)
        failed = {k: v for k, v in errors.items() if v and str(v).lower() != "no error"}
        if failed:
            raise MeasurementError(f"ADS-Schreiben fehlgeschlagen: {failed}")

    def check(self, minimum_version=1):
        try:
            values = self.read([PREFIX + f for f in PROBE_FIELDS])
        except Exception as exc:
            raise MeasurementError("SPS unterstützt Colibri-Kalibrierung noch nicht; neuen SPS-Quellstand bauen und laden.") from exc
        version = values[PREFIX + "ProbeCalibrationInterfaceVersion"]
        if version not in (1, 2):
            raise MeasurementError("Unbekannte SPS-Colibri-Schnittstellenversion.")
        if version < minimum_version:
            raise MeasurementError(
                "Düsenmessung mit Luft ab Home benötigt SPS-Colibri-Schnittstelle Version 2; "
                "geänderte MAIN.TcPOU bauen und laden. Lichtschrankenmessung bleibt mit Version 1 nutzbar.")
        self.sequence = int(values[PREFIX + "ProbeCalibrationAckSequence"])
        return values

    def pulse_heartbeat(self, force=False):
        if force or (self.owned and time.monotonic() - self.last_heartbeat >= 0.1):
            self.heartbeat = (self.heartbeat + 1) & MASK
            self.write({PREFIX + "GuiProbeCalibrationHeartbeat": self.heartbeat})
            self.last_heartbeat = time.monotonic()

    def command(self, command, config=None, cancel=None):
        if command == 1:
            state = self.check(minimum_version=2 if config.mode == "nozzle" else 1)
            if state[PREFIX + "ProbeCalibrationActive"]:
                raise MeasurementError("SPS-Colibri-Kalibrierung bereits von einem anderen Lauf belegt.")
            owners = self.read(["MAIN.GuiReorientationControlActive", "MAIN.GuiConveyorCalibrationMode",
                                "MAIN.GuiVelocityCheckMode", "MAIN.BarrierCalibrationActive",
                                "MAIN.GuiForceDelayMeasurementEnabled"])
            if any(owners.values()):
                raise MeasurementError("Andere Steuerung/Kalibrierung zuerst beenden.")
            if state[PREFIX + "ProbeCalibrationFaultCode"]:
                self.command(4, cancel=cancel)
            self.write({PREFIX + "GuiProbeCalibrationMode": 1 if config.mode == "barriers" else 2,
                        PREFIX + "GuiProbeCalibrationArray": config.array,
                        PREFIX + "GuiProbeCalibrationNozzle": config.nozzle,
                        PREFIX + "GuiProbeCalibrationTriggerSensor": config.array * 2,
                        PREFIX + "GuiProbeCalibrationPressureMbar": config.pressure_mbar,
                        PREFIX + "GuiProbeCalibrationMaxOpenMs": round(config.max_open_s * 1000)})
            self.pulse_heartbeat(True)
            # Includes a potentially partially completed entry in cleanup.
            self.owned = True
        elif command == 5:
            self.check(minimum_version=2)
        self.sequence = (self.sequence + 1) & MASK
        self.write({PREFIX + "GuiProbeCalibrationCommand": command})
        self.write({PREFIX + "GuiProbeCalibrationCommandSequence": self.sequence})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise MeasurementError("Abgebrochen.")
            self.pulse_heartbeat()
            values = self.read([PREFIX + f for f in PROBE_FIELDS])
            if int(values[PREFIX + "ProbeCalibrationAckSequence"]) == self.sequence:
                fault = int(values[PREFIX + "ProbeCalibrationFaultCode"])
                if fault:
                    raise MeasurementError(f"SPS-Kalibrierfehler {fault} (siehe COLIBRI_CALIBRATION.md).")
                if command == 4:
                    self.owned = False
                return values
            time.sleep(0.01)
        raise MeasurementError("SPS hat Kalibrierbefehl nicht quittiert.")

    def snapshot(self):
        started = time.monotonic_ns()
        history = self.read([PREFIX + "LightBarrierEventHistory"])[PREFIX + "LightBarrierEventHistory"]
        finished = time.monotonic_ns()
        values = self.read([PREFIX + f for f in PROBE_FIELDS]
                           + [f"MAIN.LightBarrierStable{s}" for s in range(1, 9)]
                           + ["MAIN.StepperPosBusy", "MAIN.ConveyorServo1TargetVelocity", "MAIN.ConveyorServo2TargetVelocity"])
        return {"history": history, "sampled_ns": (started + finished) // 2,
                "roundtrip_ns": finished - started,
                "clear": {s: bool(values[f"MAIN.LightBarrierStable{s}"]) for s in range(1, 9)},
                "fault": int(values[PREFIX + "ProbeCalibrationFaultCode"]),
                "active": bool(values[PREFIX + "ProbeCalibrationActive"]),
                "open": bool(values[PREFIX + "ProbeCalibrationValveOpen"]),
                "opened_ms": int(values[PREFIX + "ProbeCalibrationOpenedAtMs"]),
                "belt_stopped": not values["MAIN.StepperPosBusy"] and
                values["MAIN.ConveyorServo1TargetVelocity"] == 0 and values["MAIN.ConveyorServo2TargetVelocity"] == 0}

    @staticmethod
    def spacing_symbol(key):
        import re
        pair = re.fullmatch(r"sensor_spacing_([1-7])([2-8])_mm", key)
        array = re.fullmatch(r"sensor_([2468])_to_array_([1-4])_spacing_mm", key)
        if pair and int(pair[2]) == int(pair[1]) + 1:
            return f"MAIN.GuiSensorSpacing{pair[1]}{pair[2]}Mm"
        if array and int(array[1]) == 2 * int(array[2]):
            return f"MAIN.GuiSensor{array[1]}ToArray{array[2]}SpacingMm"
        raise MeasurementError("Dieses Lichtschrankenpaar hat kein direktes SPS-Abstandsfeld.")

    def write_spacing(self, key, value):
        self.write({self.spacing_symbol(key): value})

    def read_spacing(self, key):
        symbol = self.spacing_symbol(key)
        return float(self.read([symbol])[symbol])

    def global_settings(self):
        keys = [f"sensor_spacing_{s}{s+1}_mm" for s in range(1, 8)]
        keys += [f"sensor_{2*a}_to_array_{a}_spacing_mm" for a in range(1, 5)]
        names = [self.spacing_symbol(k) for k in keys]
        names += ["MAIN.GuiBarrierCalibrationDebounceMs"]
        names += [f"MAIN.GuiLightBarrierInvert{s}" for s in range(1, 9)]
        names += [f"MAIN.GuiLightBarrierDebounceEnabled{s}" for s in range(1, 9)]
        values = self.read(names)
        return {"version": 1, **{k: float(values[self.spacing_symbol(k)]) for k in keys},
                "light_barrier_debounce_ms": int(values["MAIN.GuiBarrierCalibrationDebounceMs"]),
                "light_barrier_inverted": [bool(values[f"MAIN.GuiLightBarrierInvert{s}"]) for s in range(1, 9)],
                "light_barrier_debounce_enabled": [bool(values[f"MAIN.GuiLightBarrierDebounceEnabled{s}"]) for s in range(1, 9)]}

    def release(self):
        if self.owned:
            try:
                self.write({PREFIX + "GuiProbeCalibrationStop": True})
                self.command(4)
            finally:
                self.owned = False

    def close(self):
        self.connection.close()


class ForceStream:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = deque(maxlen=250_000)
        self.latest = None
        self.received_at = 0.0
        self.profile = None
        self.tare = None
        self.noise_n = 0.0
        self.fault = None
        self.overflows = 0

    def receive(self, sample):
        with self.lock:
            if len(self.samples) == self.samples.maxlen:
                self.overflows += 1
            self.samples.append((sample, time.monotonic_ns()))
            self.latest = sample
            self.received_at = time.monotonic()
            self.fault = None if sample.valid else f"QuantumX: {sample.status}"

    def calibrated(self, sample):
        if self.profile:
            return self.profile.apply(sample)
        return sample

    def check(self, limit_n):
        with self.lock:
            if self.latest is None or time.monotonic() - self.received_at > 0.25:
                raise MeasurementError("QuantumX-Messdaten fehlen oder sind älter als 250 ms.")
            if self.fault or not self.latest.valid:
                raise MeasurementError(self.fault or "Ungültige QuantumX-Messdaten.")
            if self.overflows:
                raise MeasurementError("Kraftdatenpuffer übergelaufen.")
            sample = self.calibrated(self.latest)
            raw = (sample.force_1_raw_n, sample.force_2_raw_n)
            if any(v is None or not math.isfinite(v) for v in raw):
                raise MeasurementError("QuantumX-Bridge liefert keine gültigen Rohkräfte.")
            if max(abs(v) for v in raw) >= limit_n:
                raise MeasurementError(f"Kraftgrenzwert {limit_n:g} N an einer Wägezelle überschritten.")
            return sample

    def drain(self):
        with self.lock:
            result = list(self.samples)
            self.samples.clear()
            return result

    def load_profile(self, path):
        profile = PlatformCalibration.load(path)
        if profile.device_ip != "192.168.10.20" or tuple(
            (c.get("logical_name"), c.get("mx_channel")) for c in profile.channels
        ) != (("force_1", 3), ("force_2", 4)):
            raise MeasurementError("Kraftprofil muss zum MX440B 192.168.10.20 und TAL221 auf Kanal 3/4 passen.")
        with self.lock:
            self.profile = profile
            self.tare = None

    def zero(self, cancel):
        if not self.profile:
            raise MeasurementError("Zuerst das TAL221-Kraftkalibrierprofil laden.")
        self.drain()
        deadline = time.monotonic() + 1.0
        samples = {}
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise MeasurementError("Tara abgebrochen.")
            self.check(4.0)
            for sample, received in self.drain():
                if sample.valid and sample.force_1_raw_n is not None and sample.force_2_raw_n is not None:
                    samples[sample.sample_id] = sample
            time.sleep(0.01)
        if len(samples) < 20:
            raise MeasurementError("Tara benötigt mindestens 20 gültige verschiedene Messwerte.")
        values = list(samples.values())
        tare1 = statistics.mean(s.force_1_raw_n for s in values)
        tare2 = statistics.mean(s.force_2_raw_n for s in values)
        noise = statistics.stdev((s.force_1_raw_n + s.force_2_raw_n) * self.profile.gain for s in values)
        with self.lock:
            self.profile = self.profile.with_tare(tare1, tare2)
            self.noise_n = noise
            self.tare = {"force_1_n": tare1, "force_2_n": tare2, "noise_total_n": noise,
                         "sample_count": len(values), "zeroed_utc": self.profile.zeroed_utc}
        return self.tare


class ProbeRunner:
    """All commands are on one I/O thread; cancellation is an independent Event."""
    def __init__(self, axis, plc, forces, cancel, progress=lambda x: None, update=lambda x: None):
        self.axis, self.plc, self.forces = axis, plc, forces
        self.cancel, self.progress, self.update = cancel, progress, update
        self.config = ProbeConfig()
        self.recording = None
        self.timeline = PositionTimeline()
        self.history = BarrierHistory()
        self.pending_events = []
        self.events = []
        self.force_rows = []
        self.pending_force = []
        self.cycle, self.direction = 0, "idle"
        self.utc_offset_ns = time.time_ns() - time.monotonic_ns()
        self.last_received_ns = None
        self.last_force_source_ns = None
        self.last_force_sequence = None
        self.last_status = None
        self.last_position = 0.0
        self.last_ui = 0.0
        self.window_start_ns = 0
        self.max_uncertainty_mm = 0.0
        self.require_open = False

    def guard(self):
        if self.cancel.is_set():
            raise MeasurementError("Abgebrochen; keine automatische Weiterfahrt.")
        return self.forces.check(self.config.force_limit_n)

    def tick(self, capture=None):
        self.guard()
        self.plc.pulse_heartbeat()
        before = time.monotonic_ns()
        steps = self.axis.position_steps()
        after = time.monotonic_ns()
        position = steps * self.config.mm_per_step
        if not AXIS_POSITION_MIN_MM - 0.1 <= position <= AXIS_POSITION_MAX_MM + 0.1:
            raise MeasurementError(
                f"Colibri-Position {position:.3f} mm außerhalb "
                f"{AXIS_POSITION_MIN_MM:g} bis {AXIS_POSITION_MAX_MM:g} mm.")
        self.last_position = position
        self.timeline.add((before + after) // 2, position, after - before)
        if self.window_start_ns == 0:
            self.window_start_ns = self.timeline.times[0]
        self.last_status = self.axis.status()
        self.guard()
        if self.last_status["error_byte"] or self.last_status["software_limit"]:
            raise MeasurementError("Colibri meldet Fehler oder Software-Endlage.")
        snapshot = self.plc.snapshot()
        if snapshot["fault"] or not snapshot["active"]:
            raise MeasurementError(f"SPS-Kalibrierung gestoppt (Fehler {snapshot['fault']}).")
        if not snapshot["belt_stopped"]:
            raise MeasurementError("Förderband ist während der Colibri-Kalibrierung nicht im Stillstand.")
        if self.require_open and not snapshot["open"]:
            raise MeasurementError("Düsenventil vor Abschluss der Kraftmessfahrten geschlossen.")
        new = self.history.consume(snapshot["history"], snapshot["sampled_ns"], snapshot["roundtrip_ns"])
        self.pending_events.extend(e for e in new if e["time_ns"] >= self.window_start_ns)
        ready = []
        remaining = []
        for event in self.pending_events:
            if event["time_ns"] > self.timeline.times[-1]:
                remaining.append(event)
                continue
            pos, uncertainty = self.timeline.at(event["time_ns"])
            event = {**event, "position_mm": pos,
                     "estimated_uncertainty_mm": (uncertainty + event["time_uncertainty_s"]) * self.config.speed_mm_s}
            ready.append(event)
            self.events.append(event)
            if capture:
                capture.add(event)
            if self.recording:
                self.recording.raw(kind="edge", cycle=self.cycle, direction=self.direction, **event)
        self.pending_events = remaining
        self._forces()
        if self.recording:
            self.recording.raw(kind="position", cycle=self.cycle, direction=self.direction,
                               time_ns=(before + after) // 2, position_mm=position, roundtrip_ns=after - before)
        if time.monotonic() - self.last_ui >= 0.1:
            self.last_ui = time.monotonic()
            sample = self.forces.check(self.config.force_limit_n)
            self.update({"position_mm": position, "force_1_n": sample.force_1_n,
                         "force_2_n": sample.force_2_n, "force_total_n": sample.force_total_n,
                         "cycle": self.cycle, "direction": self.direction,
                         "open": snapshot["open"], "force_rows": self.force_rows[-1000:]})
        return snapshot, ready

    def _forces(self):
        if abs((time.time_ns() - time.monotonic_ns()) - self.utc_offset_ns) > 50_000_000:
            raise MeasurementError("PC-Systemzeit während der Messung verändert.")
        self.pending_force.extend(self.forces.drain())
        remaining = []
        for source, received_ns in self.pending_force:
            sample_ns = source.timestamp_utc_ns - self.utc_offset_ns
            if not source.valid:
                raise MeasurementError(f"Ungültiger Kraftmesswert: {source.status}.")
            if sample_ns < self.window_start_ns:
                continue
            if sample_ns > self.timeline.times[-1]:
                if sample_ns - time.monotonic_ns() > 250_000_000:
                    raise MeasurementError("QuantumX-Zeit liegt mehr als 250 ms in der Zukunft.")
                remaining.append((source, received_ns))
                continue
            if self.last_force_source_ns is not None:
                if source.timestamp_utc_ns <= self.last_force_source_ns or source.sequence <= self.last_force_sequence:
                    raise MeasurementError("Kraftzeit/Sequenz rückläufig oder doppelt.")
                if source.sequence != self.last_force_sequence + 1:
                    raise MeasurementError("Lücke in der QuantumX-Sequenz.")
                if source.timestamp_utc_ns - self.last_force_source_ns > 250_000_000:
                    raise MeasurementError("Kraftmessdatenlücke größer als 250 ms.")
            self.last_force_source_ns = source.timestamp_utc_ns
            self.last_force_sequence = source.sequence
            pos, uncertainty = self.timeline.at(sample_ns)
            sample = self.forces.calibrated(source)
            values = (sample.force_1_raw_n, sample.force_2_raw_n)
            if any(v is None or not math.isfinite(v) for v in values):
                raise MeasurementError("QuantumX-Rohkräfte fehlen.")
            if max(abs(v) for v in values) >= self.config.force_limit_n:
                raise MeasurementError("Kraftgrenze innerhalb des Messdatenstroms überschritten.")
            latency = abs(received_ns - sample_ns) / 1e9
            if latency > 0.25:
                raise MeasurementError("QuantumX-Kraftdaten sind mehr als 250 ms verzögert.")
            error_mm = (uncertainty + latency) * self.config.speed_mm_s
            self.max_uncertainty_mm = max(self.max_uncertainty_mm, error_mm)
            row = {"kind": "force", "cycle": self.cycle, "direction": self.direction,
                   "time_ns": sample_ns, "source_utc_ns": source.timestamp_utc_ns,
                   "sequence": source.sequence, "position_mm": pos,
                   "force_1_n": values[0], "force_2_n": values[1], "force_total_n": sum(values),
                   "source_force_1_n": source.force_1_raw_n, "source_force_2_n": source.force_2_raw_n,
                   "live_force_1_n": sample.force_1_n, "live_force_2_n": sample.force_2_n,
                   "estimated_uncertainty_mm": error_mm, "status": sample.status}
            self.force_rows.append(row)
            if self.recording:
                self.recording.raw(**row)
        self.pending_force = remaining

    def wait(self, duration, capture=None):
        until = time.monotonic() + duration
        while time.monotonic() < until:
            self.tick(capture)
            self.cancel.wait(0.005)

    def enter(self, config):
        self.config = config
        self.guard()
        self.plc.command(1, config, self.cancel)
        deadline = time.monotonic() + 8
        while not self.plc.snapshot()["belt_stopped"]:
            self.guard()
            self.plc.pulse_heartbeat()
            if time.monotonic() > deadline:
                raise MeasurementError("Förderband erreicht keinen Stillstand.")
            self.cancel.wait(0.03)
        self.axis.set_remote()
        self.axis.enable()
        self.set_speed(config.speed_mm_s)

    def set_speed(self, speed):
        self.plc.pulse_heartbeat()
        setting = round(speed / (100 * self.config.mm_per_step))
        self.axis.set_parameter(3, 2, setting, 2)

    def home(self):
        self.progress("Referenzfahrt nach Home …")
        self.direction = "home"
        self.axis.configure_negative_reference()
        self.plc.pulse_heartbeat()
        self.axis.reference()
        started = time.monotonic()
        deadline = started + 180
        while time.monotonic() < deadline:
            self.tick()
            if time.monotonic() - started > 0.3 and self.last_status["referenced"] and not self.last_status["moving"]:
                if abs(self.last_position) > self.config.mm_per_step * 2:
                    raise MeasurementError("Referenzfahrt beendet, aber Achse steht nicht bei 0 mm.")
                self.wait(0.3)
                return
            self.cancel.wait(0.005)
        raise MeasurementError("Referenzfahrt hat Zeitlimit überschritten.")

    def reset_window(self, direction):
        self.direction = direction
        self.force_rows = []
        self.events = []
        self.pending_events = []
        self.pending_force = []
        self.forces.drain()
        self.window_start_ns = time.monotonic_ns()
        self.last_force_source_ns = None
        self.last_force_sequence = None
        # Prime history cursor from a fresh snapshot without replaying earlier motion.
        self.history = BarrierHistory()
        snapshot = self.plc.snapshot()
        self.history.consume(snapshot["history"], snapshot["sampled_ns"], snapshot["roundtrip_ns"])
        return snapshot

    def move(self, target, capture, auto_turn=False):
        self.guard()
        self.axis.move_absolute_steps(round(target / self.config.mm_per_step))
        deadline = time.monotonic() + abs(target - self.last_position) / self.config.speed_mm_s * 1.5 + 5
        turning = False
        final_target = target
        while time.monotonic() < deadline:
            self.tick(capture)
            if auto_turn and capture.complete and not turning:
                # Find exit using measured position; command a bounded final absolute move.
                exit_positions = [self.timeline.at(e["off"]["time_ns"])[0] for e in capture.edges.values()]
                final_target = min(exit_positions) - self.config.runout_mm
                if final_target < self.config.outward_target_mm:
                    raise MeasurementError("Nachlauf hinter Lichtschranken überschreitet maximalen Verfahrweg.")
                if self.last_position <= final_target:
                    self.axis.stop()
                    final_target = self.last_position
                else:
                    self.axis.stop()
                    self._wait_stopped(capture)
                    if self.last_position < self.config.outward_target_mm:
                        raise MeasurementError("Achse überschreitet maximalen Verfahrweg beim Abbremsen.")
                    final_target = min(final_target, self.last_position)
                    self.axis.move_absolute_steps(round(final_target / self.config.mm_per_step))
                turning = True
            if not self.last_status["moving"] and abs(self.last_position - final_target) <= self.config.mm_per_step * 2:
                # Drain late events/force packets while holding the same direction.
                self.wait(0.35, capture)
                if not capture.complete:
                    raise MeasurementError("Verfahrgrenze erreicht, bevor alle Lichtschrankenflanken erfasst wurden.")
                return
            self.cancel.wait(0.005)
        raise MeasurementError("Achsfahrt hat Zeitlimit überschritten.")

    def _wait_stopped(self, capture=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.tick(capture)
            if not self.last_status["moving"]:
                return
        raise MeasurementError("Colibri stoppt nicht.")

    def run(self, config, root):
        config.validate()
        if config.mode == "nozzle" and (not self.forces.profile or not self.forces.tare):
            raise MeasurementError("Düsenmessung benötigt geladenes Kraftprofil und frische Tara.")
        self.recording = ProbeRecording(root, config,
                                        self.forces.profile.to_dict() if self.forces.profile else None,
                                        self.forces.tare)
        self.update({"document": self.recording.document, "path": str(self.recording.path)})
        error = None
        try:
            self.enter(config)
            self.home()
            for cycle in range(1, CYCLES + 1):
                self.cycle = cycle
                self.progress(f"Zyklus {cycle}/{CYCLES}: Hinfahrt")
                forward = EdgeCapture(config.sensors)
                snapshot = self.reset_window("forward")
                if not all(snapshot["clear"][s] for s in config.sensors):
                    raise MeasurementError("Lichtschranken müssen an Home frei sein.")
                if config.mode == "nozzle":
                    self.plc.command(5, cancel=self.cancel)
                    opened = self.plc.snapshot()
                    if not opened["open"]:
                        raise MeasurementError("Düsenventil wurde an Home nicht geöffnet.")
                    self.require_open = True
                    self.wait(0.2)
                self.move(config.outward_target_mm, forward, config.mode == "barriers")
                forward_edges = forward.positioned(self.timeline)
                if config.mode == "barriers":
                    self.recording.add(barrier_row(cycle, "forward", forward_edges,
                                                  config.first_sensor, config.second_sensor, config.speed_mm_s))
                    forward_peaks = None
                else:
                    forward_peaks, forward_curve = force_peak(self.force_rows, self.forces.noise_n)
                    # The return reference is not known yet. Keep the outbound result
                    # provisional so an abort still retains its peaks and curve.
                    self.recording.document["pending_nozzle_cycle"] = {
                        "cycle": cycle, "forward_peaks": forward_peaks,
                        "forward_curve": forward_curve[::max(1, len(forward_curve) // 500)],
                        "forward_edges": forward_edges, "opened_ms": opened["opened_ms"],
                        "reference_direction": "backward"}
                    self.recording.save()
                self.update({"document": self.recording.document, "path": str(self.recording.path)})
                self.progress(f"Zyklus {cycle}/{CYCLES}: Rückfahrt")
                backward = EdgeCapture(config.sensors)
                self.reset_window("backward")
                self.move(0.0, backward)
                backward_edges = backward.positioned(self.timeline)
                if config.mode == "barriers":
                    self.recording.add(barrier_row(cycle, "backward", backward_edges,
                                                  config.first_sensor, config.second_sensor, config.speed_mm_s))
                else:
                    self.require_open = False
                    self.plc.command(3, cancel=self.cancel)
                    backward_peaks, backward_curve = force_peak(self.force_rows, self.forces.noise_n)
                    reference = backward_edges[config.array * 2]["on"]
                    self._add_nozzle_row(cycle, "forward", forward_peaks, forward_curve,
                                         forward_edges, reference, opened["opened_ms"])
                    self._add_nozzle_row(cycle, "backward", backward_peaks, backward_curve,
                                         backward_edges, reference, opened["opened_ms"])
                    self.recording.document.pop("pending_nozzle_cycle", None)
                    self.recording.save()
                    self.wait(0.5)
                self.update({"document": self.recording.document, "path": str(self.recording.path)})
        except Exception as exc:
            error = str(exc)
        finally:
            cleanup_errors = self.stop()
            if cleanup_errors:
                error = "; ".join(filter(None, (error, cleanup_errors)))
            self.recording.document["estimated_uncertainty_mm"] = max(
                [self.max_uncertainty_mm] + [r.get("estimated_uncertainty_mm", 0) for r in self.recording.document["rows"]])
            self.recording.finish(error)
            self.update({"document": self.recording.document, "path": str(self.recording.path)})
        if error:
            raise MeasurementError(error)
        return self.recording.document

    def _add_nozzle_row(self, cycle, direction, peaks, curve, edges, reference, opened_ms):
        distance = peaks["force_total_n"]["position_mm"] - reference["position_mm"]
        if distance <= 0:
            raise MeasurementError("Kraftpeak muss hinter der auslösenden Lichtschranke liegen.")
        self.recording.add({"cycle": cycle, "direction": direction, "distance_mm": distance,
                            "reference_position_mm": reference["position_mm"], "peaks": peaks,
                            "reference_direction": "backward", "reference_sensor": self.config.array * 2,
                            "reference_edge": dict(reference),
                            "edges": edges, "opened_plc_ms": opened_ms,
                            "curve": curve[::max(1, len(curve) // 500)],
                            "estimated_uncertainty_mm": self.max_uncertainty_mm + reference["estimated_uncertainty_mm"]})

    def stop(self):
        errors = []
        self.require_open = False
        # Close air first; serial timeout must never delay valve closure.
        try:
            self.plc.release()
        except Exception as exc:
            errors.append(f"SPS-Abschluss fehlgeschlagen (Watchdog aktiv): {exc}")
        try:
            self.axis.stop()
        except Exception as exc:
            errors.append(f"Colibri-Stopp nicht bestätigt: {exc}")
        return "; ".join(errors)


class ProbeDevices(QObject):
    status = pyqtSignal(str)
    telemetry = pyqtSignal(object)
    result = pyqtSignal(object)
    busy_changed = pyqtSignal(bool)
    connected = pyqtSignal(bool)
    failed = pyqtSignal(str)
    applied = pyqtSignal(str, float)

    def __init__(self, net_id, ip, port, parent=None):
        super().__init__(parent)
        self.target = (net_id, ip, port)
        self.commands = queue.Queue()
        self.cancel = threading.Event()
        self.closing = threading.Event()
        self.forces = ForceStream()
        self.force_client = None
        self.bridge_process = None
        self.bridge_log_file = None
        self.axis = None
        self.plc = None
        self.last_result = None
        self.results = {}
        self.running = False
        self.last_config = ProbeConfig()
        self.thread = None
        # Creating the window is read-only: device threads start on the first command.

    def submit(self, kind, payload=None):
        if self.running:
            return
        self.running = True
        self.cancel.clear()
        self.busy_changed.emit(True)
        self.commands.put((kind, payload))
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="colibri-calibration", daemon=True)
            self.thread.start()

    def abort(self):
        self.cancel.set()

    def _loop(self):
        while not self.closing.is_set():
            try:
                kind, payload = self.commands.get(timeout=0.1)
            except queue.Empty:
                self._idle_telemetry()
                continue
            try:
                self._execute(kind, payload)
            except Exception as exc:
                self.failed.emit(str(exc))
            finally:
                self.running = False
                self.busy_changed.emit(False)
        if self.axis:
            try:
                self.axis.close()
            except Exception:
                pass
        if self.plc:
            try:
                self.plc.close()
            except Exception:
                pass
        self._disconnect_force()

    def _execute(self, kind, payload):
        if kind == "connect":
            if self.axis:
                self.axis.close()
                self.axis = None
            if self.plc:
                self.plc.close()
                self.plc = None
            self.connected.emit(False)
            try:
                self.axis = ColibriController(payload)
                self.plc = ProbePlc(*self.target)
                # Missing symbols affect only the new controls, never legacy AdsWorker polling.
                self.plc.check()
            except Exception:
                if self.axis:
                    self.axis.close()
                    self.axis = None
                if self.plc:
                    self.plc.close()
                    self.plc = None
                raise
            self.connected.emit(True)
            self.status.emit("Colibri und SPS-Kalibrierschnittstelle verbunden.")
            self.telemetry.emit({"position_mm": self.axis.position_steps() * self.last_config.mm_per_step})
        elif kind == "disconnect":
            try:
                if self.plc:
                    self.plc.release()
                if self.axis:
                    self.axis.stop()
            finally:
                if self.axis:
                    self.axis.close()
                    self.axis = None
                if self.plc:
                    self.plc.close()
                    self.plc = None
                self.connected.emit(False)
            self.status.emit("Colibri getrennt.")
        elif kind == "force_connect":
            if self.force_client:
                self.force_client.stop()
                self.force_client = None
            host, port, executable = payload
            try:
                with socket.create_connection((host, port), timeout=0.3):
                    pass
            except OSError:
                if host not in {"127.0.0.1", "localhost"}:
                    raise MeasurementError(f"QuantumX-Bridge auf {host}:{port} nicht erreichbar. Bridge auf dem Zielrechner starten.")
                executable = require_bridge(executable)
                log_path = Path(__file__).resolve().parent / "quantumx_bridge/logs/bridge.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if self.bridge_log_file:
                    self.bridge_log_file.close()
                self.bridge_log_file = log_path.open("ab")
                self.status.emit("QuantumXMonitor wird gestartet …")
                try:
                    self.bridge_process = subprocess.Popen(
                        [str(executable), "--server-only"], cwd=executable.parent,
                        stdout=self.bridge_log_file, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    wait_for_bridge(host, port, self.bridge_process, self.cancel)
                except Exception as exc:
                    self._disconnect_force()
                    raise MeasurementError(f"{exc}\nBridge-Protokoll: {log_path}") from exc
            self.forces.drain()
            self.forces.overflows = 0
            self.forces.tare = None
            self.forces.latest = None
            self.forces.received_at = 0
            self.force_client = QuantumXTcpClient(host, port, self.forces.receive, self.status.emit)
            self.force_client.start()
            self.status.emit("QuantumX-Verbindung gestartet (TAL221, Kanal 3/4).")
        elif kind == "force_disconnect":
            self._disconnect_force()
            self.status.emit("QuantumX getrennt; Tara verworfen.")
        elif kind == "profile":
            self.forces.load_profile(payload)
            self.status.emit(f"Kraftprofil geladen: {self.forces.profile.name}; jetzt tarieren.")
        elif kind == "tare":
            tare = self.forces.zero(self.cancel)
            self.status.emit(f"Tara abgeschlossen; Summenrauschen σ={tare['noise_total_n']:.5f} N.")
        elif kind == "run":
            config, root = payload
            self._require_connected()
            self.last_result = None
            self.last_config = config
            runner = self._runner()
            runner.run(config, root)
        elif kind in {"home", "move"}:
            self._require_connected()
            config, target = payload
            config.validate()
            if kind == "move" and not AXIS_POSITION_MIN_MM <= target <= AXIS_POSITION_MAX_MM:
                raise MeasurementError(
                    f"Manuelle Zielposition muss zwischen {AXIS_POSITION_MIN_MM:g} "
                    f"und {AXIS_POSITION_MAX_MM:g} mm liegen.")
            self.last_config = config
            runner = self._runner()
            try:
                runner.enter(config)
                if kind == "home":
                    runner.home()
                else:
                    runner.tick()
                    if not runner.last_status["referenced"]:
                        raise MeasurementError("Vor manuellem Verfahren zuerst Home ausführen.")
                    self.axis.move_absolute_steps(round(target / config.mm_per_step))
                    deadline = time.monotonic() + abs(target - runner.last_position) / config.speed_mm_s * 1.5 + 5
                    while True:
                        runner.tick()
                        if not runner.last_status["moving"] and abs(runner.last_position - target) <= config.mm_per_step * 2:
                            break
                        if time.monotonic() > deadline:
                            raise MeasurementError("Manuelle Achsfahrt hat Zeitlimit überschritten.")
            finally:
                errors = runner.stop()
                if errors:
                    self.failed.emit(errors)
        elif kind == "apply":
            self._require_connected()
            settings_path, session_path = payload
            document = self.results.get(session_path)
            if not document or not document.get("apply_allowed"):
                raise MeasurementError("Keine vollständig gültige Messserie zur Übernahme.")
            config = ProbeConfig(**document["config"])
            key = (f"sensor_spacing_{config.first_sensor}{config.second_sensor}_mm" if config.mode == "barriers"
                   else f"sensor_{config.array*2}_to_array_{config.array}_spacing_mm")
            value = persist_spacing(settings_path, key, document["summary"]["mean_mm"], self.plc)
            document["applied"] = {"setting": key, "value_mm": value}
            session_file = Path(session_path) / "session.json"
            temporary = session_file.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(session_file)
            self.applied.emit(key, value)
            self.status.emit(f"Abstand {value:.3f} mm übernommen, gespeichert und aus SPS zurückgelesen.")

    def _require_connected(self):
        if not self.axis or not self.plc:
            raise MeasurementError("Zuerst Colibri und SPS verbinden.")

    def _runner(self):
        return ProbeRunner(self.axis, self.plc, self.forces, self.cancel, self.status.emit, self._update)

    def _update(self, values):
        if "document" in values:
            # Signals cross threads: hand over an immutable-by-convention snapshot.
            snapshot = json.loads(json.dumps(values))
            self.last_result = snapshot["document"]
            self.results[snapshot["path"]] = snapshot["document"]
            self.result.emit(snapshot)
        else:
            self.telemetry.emit(values)

    def _idle_telemetry(self):
        values = {}
        try:
            if self.bridge_process is not None and self.bridge_process.poll() is not None:
                code = self.bridge_process.returncode
                self._disconnect_force()
                self.status.emit(f"QuantumXMonitor wurde beendet (Exitcode {code}). Bridge-Protokoll unter quantumx_bridge/logs prüfen.")
            if self.axis:
                values["position_mm"] = self.axis.position_steps() * self.last_config.mm_per_step
            with self.forces.lock:
                source = self.forces.latest
                if source and source.valid and time.monotonic() - self.forces.received_at <= 0.25:
                    sample = self.forces.calibrated(source)
                    values.update(force_1_n=sample.force_1_n, force_2_n=sample.force_2_n,
                                  force_total_n=sample.force_total_n)
                else:
                    values.update(force_1_n=None, force_2_n=None, force_total_n=None)
                values["tare_ready"] = self.forces.tare is not None
                values["profile_name"] = self.forces.profile.name if self.forces.profile else "Kein Profil"
                self.forces.samples.clear()
                self.forces.overflows = 0
            self.telemetry.emit(values)
        except Exception as exc:
            self.connected.emit(False)
            self.status.emit(f"Colibri-Verbindung unterbrochen: {exc}")
            if self.axis:
                self.axis.close()
                self.axis = None

    def shutdown(self):
        self.abort()
        self.closing.set()
        # Active runner observes cancellation; PLC watchdog is independent of this wait.
        if self.thread is not None:
            self.thread.join(timeout=4)
            if self.thread.is_alive():
                return False
        self._disconnect_force()
        return True

    def _disconnect_force(self):
        if self.force_client:
            self.force_client.stop()
            self.force_client = None
        with self.forces.lock:
            self.forces.latest = None
            self.forces.tare = None
            self.forces.samples.clear()
        # Only the child started by this instance is stopped, never an existing bridge.
        if self.bridge_process and self.bridge_process.poll() is None:
            self.bridge_process.terminate()
        self.bridge_process = None
        if self.bridge_log_file:
            self.bridge_log_file.close()
            self.bridge_log_file = None
