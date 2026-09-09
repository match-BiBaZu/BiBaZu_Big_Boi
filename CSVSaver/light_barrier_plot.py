"""Live digital traces, using PLC event history when available."""

from collections import deque
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget


class BarrierTrace:
    MAX_POINTS = 20000

    def __init__(self, *, single_part=False, maximum_traversal_ms=10000):
        self.single_part = single_part
        self.maximum_traversal_ms = maximum_traversal_ms
        self.raw_states = None
        self.part_active = False
        self.points = deque(maxlen=self.MAX_POINTS)
        self.states = None
        self.sequence = None
        self.clock = None
        self.elapsed_ms = 0
        self.frozen_elapsed_ms = None
        self.frozen_points = None
        self.last_completed_display = None
        self.buffered = None
        self.message = "Waiting for light barrier signals"

    @property
    def display_elapsed_ms(self):
        return self.elapsed_ms if self.frozen_elapsed_ms is None else self.frozen_elapsed_ms

    @property
    def display_points(self):
        return self.points if self.frozen_points is None else self.frozen_points

    def _freeze_display(self):
        if self.frozen_elapsed_ms is None:
            self.frozen_elapsed_ms = self.elapsed_ms
            self.frozen_points = tuple(self.points)
            self.last_completed_display = (self.frozen_elapsed_ms, self.frozen_points)

    def _resume_display(self):
        self.frozen_elapsed_ms = None
        self.frozen_points = None

    def _advance(self, timestamp):
        delta = (int(timestamp) - self.clock) & 0xFFFFFFFF
        if delta >= 0x80000000:
            return False
        self.elapsed_ms += delta
        self.clock = int(timestamp)
        return True

    def _baseline(self, status, history, now, message):
        self.states = tuple(bool(value) for value in status["light_barriers"])
        if history is not None:
            # The separate status read can be newer than the buffer. Recover
            # each known state at the buffer clock from its latest record.
            states = list(self.states)
            seen = set()
            for offset in range(256):
                sequence = (int(history[1]) - offset) & 0xFFFFFFFF
                slot = 3 + (sequence % 256) * 3
                recorded, _, code = map(int, history[slot:slot + 3])
                if recorded != sequence or not 2 <= code <= 17:
                    break
                sensor = code // 2 - 1
                if sensor not in seen:
                    states[sensor] = bool(code % 2)
                    seen.add(sensor)
            self.states = tuple(states)
        self.raw_states = self.states
        self.part_active = False
        if self.single_part:
            self.states = (True,) * 8
        self.sequence = int(history[1]) if history is not None else None
        self.buffered = history is not None
        self.clock = now
        self.elapsed_ms = 0
        self.last_completed_display = None
        self._resume_display()
        self.points.clear()
        self.points.append((0, self.states))
        self.message = message

    def _reset_held_states(self):
        self.states = (True,) * 8
        self.points.append((self.elapsed_ms, self.states))
        self.part_active = False

    def _expire_part(self):
        if (self.single_part and self.part_active
                and self.elapsed_ms > self.maximum_traversal_ms):
            self._reset_held_states()
            return True
        return False

    def _accept_single_part_event(self, sensor, state, previous_state):
        if not self.part_active:
            if sensor != 0 or state or not previous_state:
                return
            self.part_active = True
            self.elapsed_ms = 0
            self._resume_display()
            self.points.clear()
            self.points.append((0, self.states))
        if sensor == 7 and state and not self.states[7]:
            # LB8 has been activated and is now clear: release every latch.
            self._reset_held_states()
        elif not state and previous_state and self.states[sensor]:
            states = list(self.states)
            states[sensor] = False
            self.states = tuple(states)
            self.points.append((self.elapsed_ms, self.states))
            if sensor == 7:
                self._freeze_display()

    def _ordered_events(self, events):
        return events

    def process(self, status):
        history = status.get("light_barrier_event_history")
        now = int(history[2]) if history is not None else int(
            status.get("plc_event_clock_ms", time.monotonic_ns() // 1000000)
        ) & 0xFFFFFFFF
        mode = "PLC event history" if history is not None else "Polled signals (short pulses may be missed)"
        if self.states is None or self.buffered != (history is not None):
            self._baseline(status, history, now, mode)
            return

        events = []
        if history is not None:
            delta = (int(history[1]) - self.sequence) & 0xFFFFFFFF
            if delta > 256:
                self._baseline(status, history, now, "Event gap / PLC restart: trace restarted")
                return
            for offset in range(1, delta + 1):
                sequence = (self.sequence + offset) & 0xFFFFFFFF
                slot = 3 + (sequence % 256) * 3
                recorded, timestamp, code = map(int, history[slot:slot + 3])
                if recorded != sequence or not 2 <= code <= 17:
                    self.message = "Waiting for a consistent PLC event buffer"
                    return
                events.append((timestamp, code // 2 - 1, bool(code % 2)))
        else:
            events = [(now, index, bool(state))
                      for index, state in enumerate(status["light_barriers"])
                      if bool(state) != self.raw_states[index]]

        expired = False
        for timestamp, sensor, state in self._ordered_events(events):
            if not self._advance(timestamp):
                self._baseline(status, history, now, "PLC clock changed: trace restarted")
                return
            previous_state = self.raw_states[sensor]
            raw_states = list(self.raw_states)
            raw_states[sensor] = state
            self.raw_states = tuple(raw_states)
            if self.single_part:
                expired = self._expire_part() or expired
                self._accept_single_part_event(sensor, state, previous_state)
                continue
            states = list(self.states)
            falling_lb1 = sensor == 0 and states[0] and not state
            if falling_lb1:
                self.points.clear()
                self.elapsed_ms = 0
                self._resume_display()
                # Keep the trigger's vertical edge at t=0.
                self.points.append((0, self.states))
            states[sensor] = state
            self.states = tuple(states)
            self.points.append((self.elapsed_ms, self.states))
            if sensor == 7 and previous_state and not state:
                self._freeze_display()

        if not self._advance(now):
            self._baseline(status, history, now, "PLC clock changed: trace restarted")
            return
        if history is not None:
            self.sequence = int(history[1])
        expired = self._expire_part() or expired
        self.message = mode
        if expired:
            self.message = "Maximum traversal time exceeded: held signals reset"
        if len(self.points) == self.MAX_POINTS:
            self.message += " — displaying latest transitions"


class SensorRecording(BarrierTrace):
    """Continuous stable-signal history, unaffected by repeated LB1 edges."""

    def __init__(self):
        super().__init__(single_part=True)
        self.available = False

    def process(self, status):
        self.available = status.get("light_barrier_event_history") is not None
        if not self.available:
            self.states = None
            self.points.clear()
            return
        super().process(status)

    def _baseline(self, status, history, now, message):
        super()._baseline(status, history, now, message)
        self.states = self.raw_states
        self.points.clear()
        self.points.append((0, self.states))

    def _expire_part(self):
        return False

    def _accept_single_part_event(self, sensor, state, previous_state):
        states = list(self.states)
        states[sensor] = state
        self.states = tuple(states)
        self.points.append((self.elapsed_ms, self.states))

    def window(self, start_clock, end_clock):
        if not self.available or not self.points:
            return "unavailable", ()
        # Align clocks across independently timed ADS reads and UDINT rollover.
        start = self.elapsed_ms + ((start_clock - self.clock + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        end = start + ((end_clock - start_clock) & 0xFFFFFFFF)
        if start < self.points[0][0]:
            return "unavailable", ()
        if end > self.elapsed_ms:
            return "waiting", ()
        previous = self.points[0][1]
        points = []
        for timestamp, states in self.points:
            if timestamp < start:
                previous = states
                continue
            if timestamp > end:
                break
            if not points:
                points.append((0, previous))
            points.append((timestamp - start, states))
        return "ready", tuple(points or [(0, previous)])


class PlcTriggerTrace(BarrierTrace):
    """Plot PLC-recorded accepted-edge BOOLs; never infer them from raw signals."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.feed_available = False
        self.last_completed_window = None

    def _freeze_display(self):
        previous = self.last_completed_display
        super()._freeze_display()
        if self.last_completed_display is not previous:
            self.last_completed_window = ((self.clock - self.elapsed_ms) & 0xFFFFFFFF, self.clock)

    def process(self, status):
        history = status.get("light_barrier_filtered_event_history")
        self.feed_available = history is not None
        if history is None:
            self.states = None
            self.points.clear()
            self.last_completed_display = None
            self.last_completed_window = None
            self.message = "PLC trigger feed unavailable — load updated PLC program"
            return
        super().process({
            "light_barriers": [False] * 8,
            "light_barrier_event_history": history,
        })

    def _baseline(self, status, history, now, message):
        super()._baseline(status, history, now, message)
        self.last_completed_window = None
        self.states = self.raw_states
        self.points.clear()
        self.points.append((0, self.states))

    def _expire_part(self):
        return False

    def _accept_single_part_event(self, sensor, state, previous_state):
        if sensor == 0 and state and not previous_state:
            self.elapsed_ms = 0
            self._resume_display()
            self.points.clear()
            self.points.append((0, self.states))
        states = list(self.states)
        states[sensor] = state
        self.states = tuple(states)
        self.points.append((self.elapsed_ms, self.states))
        if sensor == 7 and state and not previous_state:
            self._freeze_display()


class LightBarrierPlot(QWidget):
    COLORS = ("#2563eb", "#ea580c", "#16a34a", "#9333ea",
              "#dc2626", "#0891b2", "#a16207", "#db2777")
    PAIRS = ((1, 2), (3, 4), (5, 6), (7, 8))

    def __init__(self, parent=None, *, live_updates=True, plc_filtered=False):
        super().__init__(parent)
        self.live_updates = live_updates
        self.completed_display = None
        self.completed_message = "Waiting for a completed LB1–LB8 pass"
        self.trace = (PlcTriggerTrace if plc_filtered else BarrierTrace)(single_part=True)
        self.sensor_recording = SensorRecording() if plc_filtered else None
        self.completed_overlay = ()
        self.setMinimumSize(440, 360 if plc_filtered else 320)
        self._update_tooltip()

    def _update_tooltip(self):
        self.setToolTip(
            "One part at a time: first LB1 1 → 0 starts a new plot. Each first "
            "1 → 0 is held at 0 until LB8 returns to 1, then all signals reset to 1. "
            "The plot freezes when LB8 first goes to 0 until the next part starts at LB1. "
            "Repeated pulses are ignored. Maximum traversal time releases stuck signals."
            if self.trace.single_part else
            "Stable signals after inversion and debounce. Each LB1 1 → 0 "
            "transition replaces the traces and restarts time at zero. "
            "The plot freezes when LB8 goes to 0 until the next LB1 trigger."
        )
        if isinstance(self.trace, PlcTriggerTrace):
            self.setToolTip(
                "Actual PLC PairedFirstBarrierFalling / PairedSecondBarrierFalling outputs. "
                "1 means an accepted falling edge of the stable sensor signal; 0 means "
                "no trigger. These one-cycle pulses feed velocity detection and nozzle "
                "trigger logic. They do not represent delayed valve opening."
                " Dashed curves show the recorded sensor levels after inversion/debounce, "
                "before the pair filter. Both layers share the same PLC timestamps."
            )
        if not self.live_updates:
            self.setToolTip(self.toolTip() + " This view updates only on LB8 activation; "
                            "the previous completed plot stays visible during the next pass.")

    def set_single_part(self, enabled):
        self.trace = type(self.trace)(
            single_part=bool(enabled),
            maximum_traversal_ms=self.trace.maximum_traversal_ms,
        )
        self.completed_display = None
        self.completed_overlay = ()
        if self.sensor_recording is not None:
            self.sensor_recording = SensorRecording()
        self.completed_message = "Waiting for a completed LB1–LB8 pass"
        self._update_tooltip()
        self.update()

    def set_maximum_traversal_seconds(self, seconds):
        self.trace.maximum_traversal_ms = round(seconds * 1000)

    def process_status(self, status):
        if self.sensor_recording is not None:
            self.sensor_recording.process(status)
        self.trace.process(status)
        if isinstance(self.trace, PlcTriggerTrace) and not self.trace.feed_available:
            self.completed_display = None
            self.completed_overlay = ()
            self.completed_message = self.trace.message
            self.update()
            return
        if self.live_updates:
            self.update()
        elif (self.trace.last_completed_display is not None
              and self.trace.last_completed_display is not self.completed_display):
            overlay_status, overlay = "ready", ()
            if self.sensor_recording is not None:
                overlay_status, overlay = self.sensor_recording.window(*self.trace.last_completed_window)
                if overlay_status == "waiting":
                    # The stable ring was read before the trigger ring. Wait for
                    # its next read so both curves cover the full same interval.
                    return
            self.completed_display = self.trace.last_completed_display
            self.completed_overlay = overlay
            self.completed_message = (
                "Completed pass • updates on LB8 activation" if self.trace.buffered
                else "Completed pass • polled signals; short pulses may be missed"
            )
            if overlay_status == "unavailable":
                self.completed_message = "Completed triggers • sensor recording unavailable for this pass"
            self.update()

    def set_connected(self, connected):
        if connected:
            self.completed_display = None
            self.completed_overlay = ()
            if self.sensor_recording is not None:
                self.sensor_recording = SensorRecording()
            self.completed_message = "Waiting for a completed LB1–LB8 pass"
            self.trace = type(self.trace)(
                single_part=self.trace.single_part,
                maximum_traversal_ms=self.trace.maximum_traversal_ms,
            )
        else:
            self.trace.message = "Disconnected — trace paused"
            self.completed_message = self.trace.message
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QColor("#374151"))
        painter.drawText(12, 19, "PLC accepted-edge pulses • 1 = trigger"
                         if isinstance(self.trace, PlcTriggerTrace) else
                         "First activation held • freeze on LB8 1 → 0"
                         if self.trace.single_part else
                         "Light barrier signals • freeze on LB8 1 → 0")
        painter.drawText(12, 37, self.trace.message if self.live_updates else self.completed_message)
        left, right, top, bottom = 42, self.width() - 18, 48, self.height() - 42
        if self.sensor_recording is not None:
            painter.drawText(12, 55, "Dashed: sensor before pair filter • solid: accepted trigger")
            top = 68
        row_height = (bottom - top) / len(self.PAIRS)
        points = self.trace.display_points
        elapsed_ms = self.trace.display_elapsed_ms
        if not self.live_updates:
            elapsed_ms, points = self.completed_display or (0, ())
        start = points[0][0] if points else 0
        end = max(start + 100, elapsed_ms)

        def x(timestamp):
            return left + (timestamp - start) / (end - start) * (right - left)

        for row, pair in enumerate(self.PAIRS):
            row_top = top + row * row_height
            high, low = row_top + 27, row_top + row_height - 10
            painter.setPen(QPen(QColor("#e5e7eb"), 1))
            for value, y in ((1, high), (0, low)):
                painter.drawLine(left, round(y), right, round(y))
                painter.setPen(QColor("#6b7280"))
                painter.drawText(20, round(y + 4), str(value))
                painter.setPen(QPen(QColor("#e5e7eb"), 1))
            for tick in range(5):
                px = round(left + tick * (right - left) / 4)
                painter.drawLine(px, round(high), px, round(low))
            for index, sensor in enumerate(pair):
                color = QColor(self.COLORS[sensor - 1])
                painter.setPen(QPen(color, 2))
                legend_x = left + index * 90
                painter.drawLine(legend_x, round(row_top + 10), legend_x + 18, round(row_top + 10))
                painter.drawText(legend_x + 24, round(row_top + 14), f"LB{sensor}")
                if self.completed_overlay:
                    sensor_color = QColor(color)
                    sensor_color.setAlpha(150)
                    painter.setPen(QPen(sensor_color, 1.5, Qt.PenStyle.DashLine))
                    sensor_path = QPainterPath()
                    previous_sensor_y = None
                    sensor_offset = -3 if index == 0 else 3
                    for timestamp, states in self.completed_overlay:
                        y = (high if states[sensor - 1] else low) + sensor_offset
                        if previous_sensor_y is None:
                            sensor_path.moveTo(x(timestamp), y)
                        else:
                            sensor_path.lineTo(x(timestamp), previous_sensor_y)
                            sensor_path.lineTo(x(timestamp), y)
                        previous_sensor_y = y
                    if previous_sensor_y is not None:
                        sensor_path.lineTo(x(elapsed_ms), previous_sensor_y)
                    painter.drawPath(sensor_path)
                    painter.setPen(QPen(color, 2))
                path = QPainterPath()
                previous_y = None
                # A small visual separation leaves both colours visible when
                # paired digital signals overlap; ticks still denote 0 and 1.
                offset = -1.5 if index == 0 else 1.5
                for timestamp, states in points:
                    y = (high if states[sensor - 1] else low) + offset
                    if previous_y is None:
                        path.moveTo(x(timestamp), y)
                    else:
                        path.lineTo(x(timestamp), previous_y)
                        path.lineTo(x(timestamp), y)
                    previous_y = y
                if previous_y is not None:
                    path.lineTo(x(elapsed_ms), previous_y)
                painter.drawPath(path)

        painter.setPen(QColor("#374151"))
        for tick in range(5):
            timestamp = start + tick * (end - start) / 4
            painter.drawText(round(x(timestamp)) - 16, self.height() - 25,
                             f"{timestamp / 1000:.2f}")
        painter.drawText(left, self.height() - 6, "Time [s] • first LB1 activation starts at 0"
                         if self.trace.single_part else
                         "Time [s] • LB1 falling edge resets to 0")
