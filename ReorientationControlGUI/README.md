# BiBaZu Reorientation Control

Supervised v1 control for one part per cycle. A Baumer image is evaluated by a
two-class YOLO Detect or OBB model. Pose 1 or Pose 2 can be selected as target;
the other pose applies the configured directed PressureControl transition profile.
All operator-facing labels, dialogs, status messages, and validation errors in the
application are in English.

## Installation and start

Python 3.12 and the Baumer Camera Explorer/GenTL producer are required. Close
Camera Explorer and every other GenTL client before connecting the camera.
This explicitly includes **Automated Image Capture**: close that GUI completely
before connecting the Baumer camera or either Neewer panel here. Also check Task
Manager for an old `pythonw.exe` instance if no window is visible. Only one of the
two hardware GUIs may control the camera/lights at a time.

```powershell
cd ReorientationControlGUI
uv sync --extra dev
uv run bibazu-reorientation
```

Für Bediener ohne Terminal können Desktop- und Startmenü-Verknüpfungen über
[`WindowsLaunchers/Verknuepfungen-installieren.cmd`](../WindowsLaunchers/Verknuepfungen-installieren.cmd)
installiert werden.

The first start uses camera IP `169.254.117.70`, ADS target
`10.145.4.14.1.1:851` at `192.168.0.23`, and the standard Baumer CTI path.
Device settings are stored with `QSettings` under
`LeibnizUniversitaetHannover/BiBaZuReorientationControl`.

Use **Configuration → New part configuration (roadmap) …** to select a pose roadmap first.
The dialog accepts the handover YAML (`part`, `poses`, `transitions`) and the
internal JSON export (`source`, `nodes`, `edges`). It derives the part/CAD data,
robust poses and directed transitions, then asks for the `.pt` model, explicit
model-class mapping, target-pose card, and optional Pressure JSON per eligible
edge. The generated schema-v2 YAML stores all paths relative to its own location.
Legacy pressure profiles resolve omitted machine values only after the first PLC
baseline read. Selecting a YAML or profile never writes to the PLC.

Each profile-eligible transition has a **Preview …** button. The preview loads the
selected STL/OBJ once and animates the directed change from the source-pose
quaternion to the target-pose quaternion. It also shows both endpoint views, the
commanded signed angle, and the edge ID. Playback can be paused, scrubbed, or
restarted. If CAD rendering is unavailable, the dialog keeps the roadmap's start
and target images with a direction arrow. This is an orientation aid, not a
simulation of the physical trajectory through the chute.

### Bauteil, YOLO-Modell und Pressure-Profil auswählen

In der Anwendung gibt es dafür zwei gleichwertige Wege: die Schaltflächen oberhalb
des Kamerabildes oder das Menü **Configuration**.

1. **New part configuration (roadmap) …** beginnt mit einer Roadmap in YAML oder JSON.
2. Name und CAD-Pfad werden übernommen, bleiben aber editierbar. Nur robuste
   Posen erhalten eine explizite, eindeutige YOLO-Klassen-ID und sind als Ziel
   auswählbar.
3. Für jede aktuierte Robust-zu-Robust-Kante kann optional ein Pressure-Profil
   ausgewählt werden. Passive/metastabile Kanten bleiben sichtbar, aber
   schreibgeschützt. Df1a erzeugt sechs Profilzeilen.
4. Fehlende Profile sind als Entwurf erlaubt. Die Readiness-Anzeige nennt fehlende
   Profile, erreichbare Startposen, Mapping- und Hashstatus sowie Abweichungen bei
   Name/CAD-Pfad.
5. Beim Laden wird der SHA-256 der Roadmap geprüft. Nach einer Änderung muss
   **Re-import roadmap** bestätigt werden; nur identische Pose- und Kanten-IDs
   behalten ihre Zuordnungen.
6. Schema-v1-Zwei-Posen-Dateien bleiben unverändert ladbar und ausführbar. Neue
   Roadmap-Dateien verwenden Schema v2 und sind absichtlich nur Konfiguration:
   **Start cycle** bleibt mit „Multi-pose execution not enabled yet“
   gesperrt, und es erfolgen keine PLC-Schreibzugriffe.

Mit **Edit configuration** wird die aktuell geladene YAML vollständig
vorausgefüllt geöffnet. Sie kann am bisherigen Ort überschrieben oder im
Speicherdialog unter einem neuen Namen abgelegt werden. Während eines laufenden
Zyklus sind Neu, Öffnen und Bearbeiten gesperrt.

### Hardware einstellen

Unter **Configuration → Hardware settings …** können Kamera-IP/-Seriennummer,
Baumer-CTI, SPS-IP/AMS-Net-ID/ADS-Port und beide Neewer-BLE-Adressen geändert werden.
Die Kamera-Vorschau ist dort auf 1–60 FPS begrenzbar (Standard: 15 FPS); niedrigere
Werte reduzieren die GUI-Last bei hochauflösenden Kamerabildern.
Nach dem Speichern muss die Anwendung einmal über das Desktop-Symbol neu gestartet
werden. Bleiben beide Lichtadressen leer, sucht die Anwendung beim nächsten
Verbindungsaufbau nacheinander zwei unterschiedliche Panels und speichert die
gefundenen Adressen.

## Operating contract

- V1 requires exactly the model classes `0 = Pose 1`, `1 = Pose 2`; either pose
  can be the configured target.
- A decision requires one fully visible object in three fresh consecutive frames.
- Both lights must be connected, receive a confirmed command, and be manually
  confirmed for the cycle.
- A profile with an explicit `ur_ry_angle_deg` remains blocked until the separate
  UR apply command returns an exact acknowledgement. No legacy profile gets an
  implicit 18° command.
- The PLC owner is acquired only after safe-stop and full configuration readback.
  The conveyor/array enables are written last.
- On completion or abort, raw conveyor and array enables are written false and
  verified before ownership is released.

Run exports are written below
`%LOCALAPPDATA%\BiBaZuReorientationControl\runs`. Each attempt gets an atomic PNG,
copies of YAML/profile, and a schema-versioned CSV result. Diagnostic logs rotate
separately under the adjacent `logs` directory.

## TwinCAT activation

Activate the changed `MAIN.TcPOU` before using this GUI. The added PLC contract
implements a 250 ms GUI heartbeat, 2 s watchdog, 60 s LB6 timeout, and 35 s drain
timeout. LB6 falling is latched in the 1 ms task; completion additionally requires
the exact expected trigger mask, all four array states idle, no pending trigger,
and all 24 valves closed.

Before hardware acceptance, verify online that the PLC-normalized clear state of
all six `LightBarrierStableN` signals is `TRUE`. Also resolve the documented UR
fixed-orientation discrepancy (`Rz=-90°` versus the installation's possible
`Rz=180°`).

`PressureControlGUI` and therefore `ConveyorSetupGUI` refuse their initial write
while the reorientation owner is active. Other ADS writers are prohibited during a
cycle. The PLC watchdog and GUI stop are not safety-rated; physical emergency stop
and pneumatic pressure relief remain mandatory.

## Tests

```powershell
uv run pytest
uv run ruff check src tests
```

Hardware tests require a separately approved commissioning session. Do not run
them on a pressurized system without an operator at the physical emergency stop.
