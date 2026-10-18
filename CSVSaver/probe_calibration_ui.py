"""Colibri device controls and the two independent calibration tabs."""
from __future__ import annotations

import math
from pathlib import Path

from PyQt6.QtCore import QObject, QPointF, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from probe_calibration import AXIS_POSITION_MIN_MM, AXIS_POSITION_MAX_MM, ProbeConfig, smooth_force
from probe_hardware import ProbeDevices
from probe_bridge import find_bridge


def decimal(low, high, value, suffix, step=0.5, digits=2):
    control = QDoubleSpinBox()
    control.setRange(low, high)
    control.setDecimals(digits)
    control.setSingleStep(step)
    control.setValue(value)
    control.setSuffix(suffix)
    return control


class ForcePositionPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(440, 240)
        self.rows = []
        self.live = []
        self.setToolTip("Summenkraft: blau hin / orange zurück; Einzelkräfte: grün / violett. Senkrechte Linien: LB und Peak.")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        area = self.rect().adjusted(65, 30, -20, -40)
        painter.setPen(self.palette().text().color())
        painter.drawText(12, 20, "Kraft [N] — blau: hin, orange: zurück; grün/violett: Einzelkräfte")
        painter.drawText(area.left(), self.height() - 8, "Colibri-Position [mm]")
        curves = [(row["direction"], row.get("curve", [])) for row in self.rows]
        if self.live:
            curves.append((self.live[0].get("direction", "forward"), smooth_force(self.live)))
        all_samples = [s for _, samples in curves for s in samples]
        if not all_samples:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Kraftkurven erscheinen während der Düsenmessung")
            return
        xmin, xmax = min(s["position_mm"] for s in all_samples), max(s["position_mm"] for s in all_samples)
        ymin = min(0, min(s[k] for s in all_samples for k in ("force_1_n", "force_2_n", "force_total_n")))
        ymax = max(0.01, max(s[k] for s in all_samples for k in ("force_1_n", "force_2_n", "force_total_n")))
        dx, dy = max(0.1, xmax - xmin), max(0.01, ymax - ymin)

        def point(x, y):
            return QPointF(area.left() + (x - xmin) / dx * area.width(), area.bottom() - (y - ymin) / dy * area.height())

        painter.drawRect(area)
        for index in range(6):
            x, y = xmin + dx * index / 5, ymin + dy * index / 5
            painter.drawText(int(point(x, ymin).x()) - 15, area.bottom() + 18, f"{x:.1f}")
            painter.drawText(3, int(point(xmin, y).y()) + 4, f"{y:.3f}")
        painter.setClipRect(area)
        for direction, samples in curves:
            if len(samples) < 2:
                continue
            for key, colour in (("force_1_n", "#499b69"), ("force_2_n", "#9271b5"),
                                ("force_total_n", "#2474bc" if direction == "forward" else "#df831d")):
                painter.setPen(QPen(QColor(colour), 1.5 if key == "force_total_n" else 0.8))
                stride = max(1, len(samples) // 1000)
                points = samples[::stride]
                for a, b in zip(points, points[1:]):
                    painter.drawLine(point(a["position_mm"], a[key]), point(b["position_mm"], b[key]))
        for row in self.rows:
            if "reference_position_mm" in row:
                painter.setPen(QPen(QColor("#777777"), 1, Qt.PenStyle.DashLine))
                x = row["reference_position_mm"]
                painter.drawLine(point(x, ymin), point(x, ymax))
            peak = row.get("peaks", {}).get("force_total_n")
            if peak:
                painter.setPen(QPen(QColor("#d03245"), 1, Qt.PenStyle.DotLine))
                painter.drawLine(point(peak["position_mm"], ymin), point(peak["position_mm"], ymax))


class ProbeTab(QWidget):
    def __init__(self, mode, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.sessions = {}
        self.session_path = None
        self.document = None
        self.busy = False
        self.available = False
        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        form = QFormLayout()
        self.inputs = []
        settings = QSettings("BiBaZu", "ColibriCalibration")
        if mode == "barriers":
            self.first = QComboBox()
            self.second = QComboBox()
            for sensor in range(1, 9):
                self.first.addItem(f"LB {sensor}", sensor)
                self.second.addItem(f"LB {sensor}", sensor)
            self.first.setCurrentIndex(max(0, self.first.findData(settings.value("barriers/first_sensor", 2, type=int))))
            self.second.setCurrentIndex(max(0, self.second.findData(settings.value("barriers/second_sensor", 1, type=int))))
            form.addRow("Zuerst auf der Hinfahrt", self.first)
            form.addRow("Danach auf der Hinfahrt", self.second)
            self.runout = decimal(0.005, 20, 2, " mm")
            form.addRow("Nachlauf nach letzter Ausschaltflanke", self.runout)
            self.inputs += [self.first, self.second, self.runout]
        else:
            self.array = QComboBox()
            self.nozzle = QComboBox()
            for array in range(1, 5):
                self.array.addItem(f"Array {array} / LB {array*2}", array)
            for nozzle in range(1, 7):
                self.nozzle.addItem(f"Düse {nozzle}", nozzle)
            self.pressure = QSpinBox()
            self.pressure.setRange(0, 6000)
            self.pressure.setSingleStep(100)
            self.pressure.setSuffix(" mbar")
            self.pressure.setValue(1000)
            self.open_time = decimal(1, 300, 30, " s", 5, 1)
            form.addRow("Array / Bezugslichtschranke auf Rückfahrt", self.array)
            form.addRow("Einzelne Düse", self.nozzle)
            form.addRow("Kalibrierdruck", self.pressure)
            form.addRow("Maximale Öffnungszeit je Zyklus", self.open_time)
            self.inputs += [self.array, self.nozzle, self.pressure, self.open_time]
        self.speed = decimal(0.5, 5, 2, " mm/s", 0.5, 1)
        self.travel = decimal(1, 75, 75 if mode == "barriers" else 60, " mm")
        self.direction = QComboBox()
        self.direction.addItem("Positiv (+): von Home zu positiven Positionen", 1)
        self.direction.addItem("Negativ (−): von Home zu negativen Positionen", -1)
        self.direction.setCurrentIndex(max(0, self.direction.findData(
            settings.value(f"{mode}/outward_direction", -1, type=int))))
        form.addRow("Fahrgeschwindigkeit", self.speed)
        form.addRow("Hinfahrt ab Home", self.direction)
        form.addRow("Maximaler Weg ab Home" if mode == "barriers" else "Weg bis Wendepunkt jenseits der Lichtschranke", self.travel)
        self.path_hint = QLabel()
        self.path_hint.setWordWrap(True)
        form.addRow("Fahrtrichtung", self.path_hint)
        self.travel.valueChanged.connect(self.update_path)
        self.direction.currentIndexChanged.connect(self.update_path)
        if mode == "barriers":
            self.first.currentIndexChanged.connect(self.update_path)
            self.second.currentIndexChanged.connect(self.update_path)
        self.update_path()
        form.addRow("Messumfang", QLabel("5 × hin und zurück; Home = 0 mm"))
        self.inputs += [self.speed, self.travel, self.direction]
        controls.addLayout(form)
        description = QLabel(
            "Fahrtrichtung und Lichtschrankenreihenfolge sind frei wählbar; die Rückfahrt führt nach Home. "
            "Alle Ein-/Ausschaltpositionen werden erfasst. Das Hauptergebnis ist der Abstand der Schaltmitten. "
            "Das Band bleibt stehen und alle Ventile bleiben geschlossen."
            if mode == "barriers" else
            "Home → Düse → Bezugslichtschranke → Wendepunkt; zurück in umgekehrter Reihenfolge. "
            "Das Ventil öffnet an Home und bleibt für beide Kraftkurven bis zur Rückkehr offen. "
            "Bezug beider Peakabstände ist der Einschaltpunkt der Lichtschranke auf der Rückfahrt."
        )
        description.setWordWrap(True)
        controls.addWidget(description, 1)
        root.addLayout(controls)
        if mode == "nozzle":
            duration_row = QHBoxLayout()
            self.duration_hint = QLabel()
            self.duration_hint.setWordWrap(True)
            self.suggest_duration = QPushButton("Öffnungszeit passend setzen")
            self.suggest_duration.clicked.connect(lambda: self.open_time.setValue(math.ceil(self.required_duration())))
            duration_row.addWidget(self.duration_hint, 1)
            duration_row.addWidget(self.suggest_duration)
            root.addLayout(duration_row)
            self.speed.valueChanged.connect(self.update_duration)
            self.travel.valueChanged.connect(self.update_duration)
            self.open_time.valueChanged.connect(self.update_duration)
            self.update_duration()
            self.inputs.append(self.suggest_duration)
        buttons = QHBoxLayout()
        self.start = QPushButton("Lichtschranken messen" if mode == "barriers" else "Düsenabstand messen")
        self.stop = QPushButton("Abbrechen / Ventil schließen")
        self.apply = QPushButton("Abstand übernehmen" if mode == "barriers" else "Als gemeinsamen Array-Abstand übernehmen")
        buttons.addWidget(self.start)
        buttons.addWidget(self.stop)
        buttons.addStretch()
        buttons.addWidget(self.apply)
        root.addLayout(buttons)
        self.state = QLabel("Zuerst Geräte verbinden.")
        self.state.setWordWrap(True)
        root.addWidget(self.state)
        self.selection = QComboBox()
        self.selection.setToolTip("Messserien dieser Sitzung; CSV und JSON werden automatisch gespeichert.")
        self.selection.currentIndexChanged.connect(self._select)
        root.addWidget(self.selection)
        self.table = QTableWidget(0, 7 if mode == "barriers" else 8)
        self.table.setHorizontalHeaderLabels(
            ["Zyklus", "Richtung", "Abstand [mm]", "Einschaltabstand", "Erste LB ein/aus [mm]", "Zweite LB ein/aus [mm]", "Unsicherheit [mm]"]
            if mode == "barriers" else
            ["Zyklus", "Richtung", "Abstand [mm]", "LB-Bezug Rückfahrt [mm]", "Peakposition [mm]", "FΣ Peak [N]", "F1/F2 Peak [N]", "Unsicherheit [mm]"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(160)
        root.addWidget(self.table, 1)
        self.summary = QLabel("Noch keine Messung.")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.path_label = QLineEdit()
        self.path_label.setReadOnly(True)
        root.addWidget(self.path_label)
        self.plot = ForcePositionPlot() if mode == "nozzle" else None
        if self.plot:
            root.addWidget(self.plot, 2)
        self.update_controls()

    def update_path(self):
        sign = "−" if self.direction.currentData() == -1 else "+"
        endpoint = "Grenze" if self.mode == "barriers" else "Wendepunkt"
        text = f"Hin: 0 → {sign}{self.travel.value():g} mm ({endpoint}); zurück: → 0 mm"
        if self.mode == "barriers":
            text += f" · LB{self.first.currentData()} → LB{self.second.currentData()}"
        self.path_hint.setText(text)

    def required_duration(self):
        return 2 * self.travel.value() / self.speed.value() * 1.2 + 2

    def update_duration(self):
        needed = self.required_duration()
        self.duration_hint.setText(f"Benötigt mit Reserve: mindestens {needed:.1f} s; "
                                   + ("Öffnungszeit ausreichend." if self.open_time.value() >= needed else "Öffnungszeit vor Start erhöhen."))

    def config(self, mm_per_step, force_limit):
        args = dict(mode=self.mode, speed_mm_s=self.speed.value(), travel_mm=self.travel.value(),
                    mm_per_step=mm_per_step, force_limit_n=force_limit,
                    outward_direction=self.direction.currentData())
        if self.mode == "barriers":
            args.update(first_sensor=self.first.currentData(), second_sensor=self.second.currentData(), runout_mm=self.runout.value())
        else:
            args.update(array=self.array.currentData(), nozzle=self.nozzle.currentData(), pressure_mbar=self.pressure.value(),
                        max_open_s=self.open_time.value())
        config = ProbeConfig(**args)
        config.validate()
        return config

    def receive(self, payload):
        path, document = payload["path"], payload["document"]
        if path != self.session_path and self.plot:
            self.plot.live = []
        self.sessions[path] = document
        index = self.selection.findData(path)
        if index < 0:
            config = document["config"]
            name = (f"LB{config['first_sensor']}–{config['second_sensor']}" if self.mode == "barriers"
                    else f"Array {config['array']}, Düse {config['nozzle']}")
            self.selection.addItem(f"{Path(path).name} · {name}", path)
            index = self.selection.count() - 1
        self.selection.setCurrentIndex(index)
        self._select()

    def _select(self, _index=None):
        self.session_path = self.selection.currentData()
        self.document = self.sessions.get(self.session_path)
        if not self.document:
            return
        document = self.document
        rows = document["rows"]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            direction = "Hin" if row["direction"] == "forward" else "Zurück"
            values = [str(row["cycle"]), direction, f"{row['distance_mm']:.3f}"]
            if self.mode == "barriers":
                edges = row["edges"]
                config = document["config"]
                values.append(f"{row['on_distance_mm']:.3f}")
                for sensor in (config["first_sensor"], config["second_sensor"]):
                    edge = edges[str(sensor)]
                    values.append(f"{edge['on']['position_mm']:.3f} / {edge['off']['position_mm']:.3f}")
            else:
                peaks = row["peaks"]
                values += [f"{row['reference_position_mm']:.3f}", f"{peaks['force_total_n']['position_mm']:.3f}",
                           f"{peaks['force_total_n']['force_n']:.5f}",
                           f"{peaks['force_1_n']['force_n']:.5f} / {peaks['force_2_n']['force_n']:.5f}"]
            values.append(f"±{row.get('estimated_uncertainty_mm', 0):.3f}")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        summary = document.get("summary", {})
        if summary:
            text = f"{summary['count']}/10 Fahrten · Mittel {summary['mean_mm']:.3f} mm · σ {summary['std_mm']:.3f} mm"
            for key, label in (("forward_mean_mm", "Hin"), ("backward_mean_mm", "Zurück"), ("direction_difference_mm", "Richtungsabweichung")):
                if summary.get(key) is not None:
                    text += f" · {label} {summary[key]:.3f} mm"
            text += "\nZeitliche Unsicherheit ist geschätzt. 0,1 mm Genauigkeit erst nach Hardwareabnahme bestätigt."
            self.summary.setText(text)
        self.state.setText("Messserie vollständig." if document["status"] == "complete" else document.get("error") or "Messung läuft …")
        self.path_label.setText(str(Path(self.session_path) / "session.json"))
        if self.plot:
            self.plot.rows = rows
            if document["status"] != "running":
                self.plot.live = []
            self.plot.update()
        self.update_controls()

    def update_controls(self):
        for widget in self.inputs:
            widget.setEnabled(not self.busy)
        self.selection.setEnabled(not self.busy)
        self.start.setEnabled(self.available and not self.busy)
        self.stop.setEnabled(self.busy)
        can_apply = self.document and self.document.get("apply_allowed")
        if can_apply and self.mode == "barriers":
            config = self.document["config"]
            can_apply = abs(config["second_sensor"] - config["first_sensor"]) == 1
        self.apply.setEnabled(bool(can_apply and self.available and not self.busy))


class ProbeCalibrationUi(QObject):
    busy_changed = pyqtSignal(bool)
    spacing_applied = pyqtSignal(str, float)

    def __init__(self, net_id, ip, port, settings_file, parent=None):
        super().__init__(parent)
        self.settings = QSettings("BiBaZu", "ColibriCalibration")
        self.settings_file = str(settings_file)
        self.devices = ProbeDevices(net_id, ip, port, self)
        self.available = False
        self.busy = False
        self.active_mode = None
        self.barrier_tab = ProbeTab("barriers")
        self.nozzle_tab = ProbeTab("nozzle")
        self.tabs = {"barriers": self.barrier_tab, "nozzle": self.nozzle_tab}
        self.device_group = QGroupBox("Colibri / QuantumX — Kalibrieraufbau")
        grid = QGridLayout(self.device_group)
        self.port = QComboBox()
        self.port.setEditable(True)
        try:
            from serial.tools.list_ports import comports
            for item in comports():
                self.port.addItem(item.device)
        except ImportError:
            pass
        self.port.setCurrentText(str(self.settings.value("port", "")))
        self.connect = QPushButton("Colibri verbinden")
        self.force_connect = QPushButton("QuantumX verbinden")
        saved_bridge = str(self.settings.value("bridge_path", ""))
        discovered_bridge = find_bridge() if not saved_bridge else None
        self.bridge_path = QLineEdit(saved_bridge or (str(discovered_bridge) if discovered_bridge else ""))
        self.bridge_path.setPlaceholderText("QuantumXMonitor.exe — vollständigen Programmordner vom alten Mess-PC übernehmen")
        self.bridge_browse = QPushButton("Bridge wählen …")
        self.profile = QPushButton("Kraftprofil laden …")
        self.tare = QPushButton("Kraftsensoren tarieren")
        self.home = QPushButton("Home")
        self.move_target = decimal(AXIS_POSITION_MIN_MM, AXIS_POSITION_MAX_MM, 0, " mm")
        self.move = QPushButton("Absolut verfahren")
        self.abort = QPushButton("STOPP")
        self.position = QLabel("Position: —")
        self.forces = QLabel("F1: — · F2: — · FΣ: —")
        self.profile_label = QLabel("Kein Kraftprofil / keine Tara")
        self.state = QLabel("SPS-Kalibrierschnittstelle noch nicht geprüft.")
        self.state.setWordWrap(True)
        self.scale = decimal(0.0001, 0.1, float(self.settings.value("mm_per_step", 0.005)), " mm/Schritt", 0.0001, 6)
        self.limit = decimal(0.01, 4, float(self.settings.value("force_limit_n", 1.0)), " N je Wägezelle", 0.1, 2)
        self.output = QLineEdit(str(self.settings.value("output", str(Path(__file__).resolve().parent / "probe_calibrations"))))
        self.output_browse = QPushButton("Messordner …")
        grid.addWidget(QLabel("COM-Port"), 0, 0)
        grid.addWidget(self.port, 0, 1)
        grid.addWidget(self.connect, 0, 2)
        grid.addWidget(self.position, 0, 3)
        grid.addWidget(self.forces, 0, 4, 1, 3)
        grid.addWidget(self.bridge_path, 1, 0, 1, 3)
        grid.addWidget(self.bridge_browse, 1, 3)
        grid.addWidget(self.force_connect, 1, 4)
        grid.addWidget(self.profile, 1, 5)
        grid.addWidget(self.tare, 1, 6)
        grid.addWidget(self.home, 2, 0)
        grid.addWidget(self.move_target, 2, 1)
        grid.addWidget(self.move, 2, 2)
        grid.addWidget(self.scale, 2, 3)
        grid.addWidget(self.limit, 2, 4)
        grid.addWidget(self.abort, 2, 6)
        grid.addWidget(self.output, 3, 0, 1, 6)
        grid.addWidget(self.output_browse, 3, 6)
        grid.addWidget(self.profile_label, 4, 0, 1, 7)
        grid.addWidget(self.state, 5, 0, 1, 7)
        self.idle_inputs = [self.port, self.connect, self.force_connect, self.bridge_path, self.bridge_browse,
                            self.profile, self.tare, self.move_target, self.scale, self.limit, self.output, self.output_browse]
        self.connect.clicked.connect(lambda: self.devices.submit(
            "disconnect" if self.available else "connect", self.port.currentText().strip()))
        self.force_connect.clicked.connect(self._force_connect)
        self.bridge_browse.clicked.connect(self._bridge_browse)
        self.output_browse.clicked.connect(self._output_browse)
        self.profile.clicked.connect(self._profile)
        self.tare.clicked.connect(lambda: self.devices.submit("tare"))
        self.home.clicked.connect(lambda: self._manual("home"))
        self.move.clicked.connect(lambda: self._manual("move"))
        self.abort.clicked.connect(self.devices.abort)
        for mode, tab in self.tabs.items():
            tab.start.clicked.connect(lambda checked=False, mode=mode: self._start(mode))
            tab.stop.clicked.connect(self.devices.abort)
            tab.apply.clicked.connect(lambda checked=False, mode=mode: self._apply(mode))
        self.devices.status.connect(self._status)
        self.devices.failed.connect(self._failure)
        self.devices.connected.connect(self._connected)
        self.devices.busy_changed.connect(self._busy)
        self.devices.telemetry.connect(self._telemetry)
        self.devices.result.connect(self._result)
        self.devices.applied.connect(self.spacing_applied)
        self._busy(False)

    def _remember(self):
        for key, value in (("port", self.port.currentText()), ("bridge_path", self.bridge_path.text()),
                           ("output", self.output.text()), ("mm_per_step", self.scale.value()), ("force_limit_n", self.limit.value())):
            self.settings.setValue(key, value)
        for mode, tab in self.tabs.items():
            self.settings.setValue(f"{mode}/outward_direction", tab.direction.currentData())
        self.settings.setValue("barriers/first_sensor", self.barrier_tab.first.currentData())
        self.settings.setValue("barriers/second_sensor", self.barrier_tab.second.currentData())

    def _force_connect(self):
        self._remember()
        self.devices.submit("force_disconnect" if self.devices.force_client else "force_connect",
                            ("127.0.0.1", 5500, self.bridge_path.text().strip()))

    def _bridge_browse(self):
        path, _ = QFileDialog.getOpenFileName(self.device_group, "QuantumXMonitor.exe auswählen", self.bridge_path.text(), "Programme (*.exe)")
        if path:
            self.bridge_path.setText(path)

    def _output_browse(self):
        path = QFileDialog.getExistingDirectory(self.device_group, "Messordner", self.output.text())
        if path:
            self.output.setText(path)

    def _profile(self):
        path, _ = QFileDialog.getOpenFileName(self.device_group, "TAL221-Kraftprofil laden", str(self.settings.value("profile_path", "")), "JSON (*.json)")
        if path:
            self.settings.setValue("profile_path", path)
            self.devices.submit("profile", path)

    def _manual(self, kind):
        self._remember()
        config = ProbeConfig(mm_per_step=self.scale.value(), force_limit_n=self.limit.value())
        self.active_mode = None
        self.devices.submit(kind, (config, self.move_target.value()))

    def _start(self, mode):
        try:
            config = self.tabs[mode].config(self.scale.value(), self.limit.value())
            if not self.output.text().strip():
                raise ValueError("Messordner auswählen.")
        except ValueError as exc:
            self._failure(str(exc))
            return
        self._remember()
        self.active_mode = mode
        self.devices.submit("run", (config, self.output.text().strip()))

    def _apply(self, mode):
        tab = self.tabs[mode]
        if not tab.document or not tab.document.get("apply_allowed"):
            return
        self.active_mode = mode
        self.devices.submit("apply", (self.settings_file, tab.session_path))

    def _status(self, message):
        self.state.setText(message)
        if self.active_mode:
            self.tabs[self.active_mode].state.setText(message)

    def _failure(self, message):
        self._status(message)
        QMessageBox.warning(self.device_group, "Colibri-Kalibrierung", message)

    def _connected(self, connected):
        self.available = connected
        self.connect.setText("Colibri trennen" if connected else "Colibri verbinden")
        self._busy(self.busy)

    def _busy(self, busy):
        self.busy = busy
        for widget in self.idle_inputs:
            widget.setEnabled(not busy)
        self.home.setEnabled(self.available and not busy)
        self.move.setEnabled(self.available and not busy)
        self.abort.setEnabled(busy)
        self.force_connect.setText("QuantumX trennen" if self.devices.force_client else "QuantumX verbinden")
        for tab in self.tabs.values():
            tab.busy, tab.available = busy, self.available
            tab.update_controls()
        self.busy_changed.emit(busy)

    def _telemetry(self, values):
        if "position_mm" in values:
            self.position.setText(f"Position: {values['position_mm']:.3f} mm")
        if "force_total_n" in values:
            self.forces.setText(" · ".join(f"{label}: {values[key]:.5f} N" if values[key] is not None else f"{label}: —"
                                          for label, key in (("F1", "force_1_n"), ("F2", "force_2_n"), ("FΣ", "force_total_n"))))
        if "profile_name" in values:
            self.profile_label.setText(values["profile_name"] + (" · Tara bereit" if values["tare_ready"] else " · neue Tara erforderlich"))
        if (self.active_mode == "nozzle" and values.get("force_rows")
                and values.get("direction") in {"forward", "backward"}):
            self.nozzle_tab.plot.live = values["force_rows"]
            self.nozzle_tab.plot.update()

    def _result(self, payload):
        mode = payload["document"]["config"]["mode"]
        self.tabs[mode].receive(payload)

    def shutdown(self):
        self._remember()
        return self.devices.shutdown()
