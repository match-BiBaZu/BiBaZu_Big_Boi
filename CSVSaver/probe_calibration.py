"""Position-based probe calibration and durable recording, independent of Qt/I/O."""
from __future__ import annotations

import bisect
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

MASK = 0xFFFFFFFF
CYCLES = 5
# Accepted encoder coordinates, independent of a calibration's travel distance.
AXIS_POSITION_MIN_MM = -75.0
AXIS_POSITION_MAX_MM = 75.0


class MeasurementError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeConfig:
    mode: str = "barriers"
    first_sensor: int = 2
    second_sensor: int = 1
    array: int = 1
    nozzle: int = 1
    speed_mm_s: float = 2.0
    travel_mm: float = 75.0
    runout_mm: float = 2.0
    pressure_mbar: int = 1000
    max_open_s: float = 30.0
    force_limit_n: float = 1.0
    mm_per_step: float = 0.005
    outward_direction: int = -1
    force_smoothing_samples: int = 100

    @property
    def sensors(self):
        return (self.first_sensor, self.second_sensor) if self.mode == "barriers" else (self.array * 2,)

    @property
    def outward_target_mm(self):
        return self.outward_direction * self.travel_mm

    @property
    def required_open_s(self):
        # Conservative: entire round trip, acceleration, turnaround and communication.
        return 2.0 * self.travel_mm / self.speed_mm_s * 1.2 + 2.0

    def validate(self):
        numbers = (self.speed_mm_s, self.travel_mm, self.runout_mm, self.max_open_s,
                   self.force_limit_n, self.mm_per_step)
        if not all(math.isfinite(v) for v in numbers):
            raise MeasurementError("Messparameter müssen endlich sein.")
        if self.mode not in {"barriers", "nozzle"}:
            raise MeasurementError("Unbekannter Messmodus.")
        if (type(self.force_smoothing_samples) is not int
                or not 20 <= self.force_smoothing_samples <= 2400):
            raise MeasurementError("Kraftglättung muss 20–2400 Rohmesswerte umfassen.")
        if not (1 <= self.first_sensor <= 8 and 1 <= self.second_sensor <= 8
                and self.first_sensor != self.second_sensor):
            raise MeasurementError("Zwei unterschiedliche Lichtschranken von LB1 bis LB8 auswählen.")
        if self.outward_direction not in (-1, 1):
            raise MeasurementError("Hinfahrt zu positiven oder negativen Positionen auswählen.")
        if not (1 <= self.array <= 4 and 1 <= self.nozzle <= 6):
            raise MeasurementError("Array/Düse außerhalb des gültigen Bereichs.")
        if not (0.5 <= self.speed_mm_s <= 5.0 and 0 < self.travel_mm <= 75):
            raise MeasurementError("Geschwindigkeit 0,5–5 mm/s, Verfahrweg höchstens 75 mm.")
        if not (0.005 <= self.runout_mm < self.travel_mm):
            raise MeasurementError("Nachlauf muss positiv und kleiner als der Verfahrweg sein.")
        if not (0.0001 <= self.mm_per_step <= 0.1 and 0.01 <= self.force_limit_n <= 4.0):
            raise MeasurementError("Ungültiger Achsmaßstab oder Kraftgrenzwert.")
        speed_hz = self.speed_mm_s / self.mm_per_step
        if not 1 <= speed_hz <= 20000 or abs(speed_hz - round(speed_hz)) > 1e-6:
            raise MeasurementError("Geschwindigkeit muss einer ganzzahligen Colibri-Schrittfrequenz von 1–20000 Hz entsprechen.")
        if not (0 <= self.pressure_mbar <= 6000 and 1 <= self.max_open_s <= 300):
            raise MeasurementError("Druck 0–6000 mbar, maximale Öffnung 1–300 s.")
        if self.mode == "nozzle" and self.max_open_s < self.required_open_s:
            raise MeasurementError(f"Öffnungszeit zu kurz: mindestens {self.required_open_s:.1f} s inklusive Reserve einstellen.")


class PositionTimeline:
    def __init__(self, max_gap_s=0.3):
        self.times = []
        self.positions = []
        self.roundtrips = []
        self.max_gap_ns = int(max_gap_s * 1e9)

    def add(self, time_ns, position_mm, roundtrip_ns=0):
        if not math.isfinite(position_mm) or (self.times and time_ns <= self.times[-1]):
            raise MeasurementError("Ungültige oder rückläufige Achszeit.")
        self.times.append(int(time_ns))
        self.positions.append(float(position_mm))
        self.roundtrips.append(int(roundtrip_ns))

    def at(self, time_ns):
        i = bisect.bisect_left(self.times, time_ns)
        if i < len(self.times) and self.times[i] == time_ns:
            return self.positions[i], self.roundtrips[i] / 2e9
        if i == 0 or i == len(self.times):
            raise MeasurementError("Kein beidseitiger Positionsbezug für Messzeitpunkt.")
        span = self.times[i] - self.times[i - 1]
        if span > self.max_gap_ns:
            raise MeasurementError("Positionsdatenlücke größer als 300 ms.")
        fraction = (time_ns - self.times[i - 1]) / span
        position = self.positions[i - 1] + fraction * (self.positions[i] - self.positions[i - 1])
        uncertainty_s = (span + max(self.roundtrips[i - 1:i + 1])) / 2e9
        return position, uncertainty_s


class BarrierHistory:
    """Consume the actual PLC diagnostic ring; never use pair-gated pulse signals."""
    def __init__(self):
        self.sequence = None
        self.last_clock = None

    def consume(self, history, sampled_ns, roundtrip_ns=0):
        if not isinstance(history, (list, tuple)) or len(history) != 771 or history[0] != 1:
            raise MeasurementError("Ungültiger SPS-Lichtschrankenpuffer.")
        newest, clock = int(history[1]), int(history[2])
        if self.last_clock is not None and ((clock - self.last_clock) & MASK) > 60_000:
            raise MeasurementError("SPS-Uhr zurückgesetzt oder Messdaten zu alt.")
        self.last_clock = clock
        if self.sequence is None:
            self.sequence = newest
            return []
        count = (newest - self.sequence) & MASK
        if count > 256:
            raise MeasurementError("Lichtschrankenpuffer übergelaufen oder SPS neu gestartet.")
        result = []
        for n in range(1, count + 1):
            seq = (self.sequence + n) & MASK
            slot = 3 + (seq % 256) * 3
            if int(history[slot]) != seq:
                raise MeasurementError("Lichtschrankenpuffer während des Lesens verändert.")
            event_ms, encoded = int(history[slot + 1]), int(history[slot + 2])
            age = (clock - event_ms) & MASK
            if age > 60_000 or not 2 <= encoded <= 17:
                raise MeasurementError("Ungültiger Lichtschrankenzeitpunkt/-kanal.")
            result.append({"sequence": seq, "sensor": encoded // 2,
                           "clear": bool(encoded % 2), "plc_time_ms": event_ms,
                           "time_ns": int(sampled_ns - age * 1_000_000),
                           "time_uncertainty_s": roundtrip_ns / 2e9 + 0.001})
        self.sequence = newest
        return result


class EdgeCapture:
    def __init__(self, sensors):
        self.edges = {int(sensor): {} for sensor in sensors}

    def add(self, event):
        sensor = event["sensor"]
        if sensor not in self.edges:
            return
        edges = self.edges[sensor]
        key = "off" if event["clear"] else "on"
        expected = "on" if not edges else "off"
        if len(edges) == 2 or key != expected:
            raise MeasurementError(f"Widersprüchliche Flankenfolge an LB{sensor}; Signal/Entprellung prüfen.")
        edges[key] = dict(event)

    @property
    def complete(self):
        return all(len(edges) == 2 for edges in self.edges.values())

    def positioned(self, timeline):
        if not self.complete:
            raise MeasurementError("Nicht alle Ein-/Ausschaltflanken erfasst.")
        result = {}
        for sensor, edges in self.edges.items():
            result[sensor] = {}
            for key, edge in edges.items():
                pos, uncertainty = timeline.at(edge["time_ns"])
                result[sensor][key] = {**edge, "position_mm": pos,
                                      "time_uncertainty_s": uncertainty + edge["time_uncertainty_s"]}
        return result


def statistics_for(rows):
    distances = [row["distance_mm"] for row in rows]
    if not distances:
        return {}
    forward = [r["distance_mm"] for r in rows if r["direction"] == "forward"]
    backward = [r["distance_mm"] for r in rows if r["direction"] == "backward"]
    result = {"count": len(distances), "mean_mm": statistics.mean(distances),
              "std_mm": statistics.stdev(distances) if len(distances) > 1 else 0.0,
              "minimum_mm": min(distances), "maximum_mm": max(distances)}
    for name, values in (("forward", forward), ("backward", backward)):
        result[f"{name}_mean_mm"] = statistics.mean(values) if values else None
    result["direction_difference_mm"] = (
        abs(statistics.mean(forward) - statistics.mean(backward)) if forward and backward else None)
    return result


def barrier_row(cycle, direction, edges, first, second, speed_mm_s):
    centres = {s: (e["on"]["position_mm"] + e["off"]["position_mm"]) / 2
               for s, e in edges.items()}
    distance = abs(centres[second] - centres[first])
    if distance <= 0:
        raise MeasurementError("Die Schaltmitten beider Lichtschranken liegen an derselben Position.")
    uncertainty = sum(max(e[k]["time_uncertainty_s"] for k in ("on", "off"))
                      for e in edges.values()) * speed_mm_s
    return {"cycle": cycle, "direction": direction, "distance_mm": distance,
            "on_distance_mm": abs(edges[second]["on"]["position_mm"] - edges[first]["on"]["position_mm"]),
            "centres_mm": centres, "edges": edges, "estimated_uncertainty_mm": uncertainty}


def smooth_force(samples, window=20):
    """Assign each centred average to the mean timestamp AND position of its window."""
    if len(samples) < window:
        return []
    result = []
    keys = ("position_mm", "time_ns", "force_1_n", "force_2_n", "force_total_n")
    sums = {k: sum(s[k] for s in samples[:window]) for k in keys}
    for end in range(window, len(samples) + 1):
        result.append({k: sums[k] / window for k in keys})
        if end < len(samples):
            for k in keys:
                sums[k] += samples[end][k] - samples[end - window][k]
    return result


def force_peak(samples, noise_n, window=20):
    smoothed = smooth_force(samples, window)
    if len(smoothed) < window:
        raise MeasurementError("Zu wenige Kraftmesswerte für einen belastbaren Peak.")
    peaks = {}
    for key in ("force_1_n", "force_2_n", "force_total_n"):
        maximum = max(abs(s[key]) for s in smoothed)
        indices = [i for i, s in enumerate(smoothed) if math.isclose(abs(s[key]), maximum, abs_tol=1e-12)]
        i = indices[len(indices) // 2]
        sample = smoothed[i]
        peaks[key] = {"position_mm": sample["position_mm"], "force_n": sample[key], "time_ns": sample["time_ns"]}
        if key == "force_total_n":
            positions = [s["position_mm"] for s in smoothed]
            if maximum < max(0.005, 5 * noise_n):
                raise MeasurementError("Kraftpeak nicht ausreichend über dem Tara-Rauschen.")
            if (i < 10 or i >= len(smoothed) - 10
                    or min(sample["position_mm"] - min(positions), max(positions) - sample["position_mm"]) < 0.1):
                raise MeasurementError(
                    f"Kraftpeak am Messrand: {sample['position_mm']:.3f} mm, "
                    f"|FΣ|={maximum:.4f} N, Messbereich {min(positions):.3f}–{max(positions):.3f} mm. "
                    f"Kraftkurve und Glättung ({window} Rohwerte) prüfen; "
                    "bei einem tatsächlichen Randpeak den Messbereich erweitern.")
    return peaks, smoothed


RAW_FIELDS = ("kind", "cycle", "direction", "time_ns", "source_utc_ns", "sequence",
              "position_mm", "roundtrip_ns", "sensor", "clear", "plc_time_ms",
              "force_1_n", "force_2_n", "force_total_n", "source_force_1_n", "source_force_2_n",
              "live_force_1_n", "live_force_2_n", "estimated_uncertainty_mm", "status", "message")


class ProbeRecording:
    def __init__(self, root, config, profile=None, tare=None):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.path = Path(root) / f"{stamp}_{config.mode}"
        self.path.mkdir(parents=True)
        self.config = config
        self.document = {"version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                         "config": asdict(config), "profile": profile, "tare": tare,
                         "force_processing": "profile_and_tare" if profile else "tare_only" if tare else "bridge",
                         "geometry": {"home_position_mm": 0.0, "outward_target_mm": config.outward_target_mm,
                                      "outward_sensor_order": list(config.sensors),
                                      "return_sensor_order": list(reversed(config.sensors)),
                                      "nozzle_reference_direction": "backward" if config.mode == "nozzle" else None,
                                      "valve_open_phase": "home_before_outbound" if config.mode == "nozzle" else None},
                         "status": "running", "rows": [], "summary": {}, "apply_allowed": False,
                         "accuracy_validated": False}
        self.file = (self.path / "raw.csv").open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=RAW_FIELDS, extrasaction="ignore")
        self.writer.writeheader()
        self.save()

    def raw(self, **values):
        self.writer.writerow(values)

    def save(self):
        self.file.flush()
        temporary = self.path / "session.json.tmp"
        temporary.write_text(json.dumps(self.document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(self.path / "session.json")

    def add(self, row):
        self.document["rows"].append(row)
        self.document["summary"] = statistics_for(self.document["rows"])
        self.save()

    def finish(self, error=None):
        self.document["status"] = "failed" if error else "complete"
        self.document["error"] = error
        self.document["apply_allowed"] = not error and len(self.document["rows"]) == CYCLES * 2
        self.save()
        self.file.close()


def persist_spacing(path, key, value, plc):
    """Write/read back the PLC before atomically merging just one global setting."""
    if not math.isfinite(value) or not 1 <= value <= 5000:
        raise MeasurementError("Übernahme nur für Abstände von 1–5000 mm möglich.")
    path = Path(path)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else plc.global_settings()
    if settings.get("version") != 1:
        raise MeasurementError("Unbekanntes globales Einstellungsformat.")
    plc.write_spacing(key, value)
    actual = plc.read_spacing(key)
    if not math.isclose(actual, value, rel_tol=1e-6, abs_tol=0.001):
        raise MeasurementError("SPS-Rücklesen des Abstands stimmt nicht überein.")
    settings[key] = actual
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise MeasurementError(f"SPS-Abstand bereits übernommen, globale Datei konnte nicht gespeichert werden: {exc}") from exc
    return actual
