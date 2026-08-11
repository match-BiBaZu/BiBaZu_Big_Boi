# BiBaZu Reorientation Control

Supervised control for one part per cycle. A Baumer image is evaluated by a YOLO
Detect or OBB model. Legacy schema-v1 projects support Pose 1/2; schema-v2 roadmap
projects resolve either a direct transition or one unique path with one intermediate
pose and combine its active PressureControl arrays into one physical conveyor pass.
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
The former PLC address `192.168.10.23` is automatically migrated to the current
`192.168.0.23` when the application starts.

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
6. Schema-v1-Zwei-Posen-Dateien bleiben unverändert ladbar und ausführbar.
   Schema-v2-Roadmaps können ausgeführt werden, sobald mindestens ein Startzustand
   das Ziel über einen eindeutigen profilierten Pfad mit maximal einer Zwischenpose
   erreicht. Parallele belegte Kanten werden mit ihren `edge_id` als mehrdeutig
   blockiert.

Mit **Edit configuration** wird die aktuell geladene YAML vollständig
vorausgefüllt geöffnet. Sie kann am bisherigen Ort überschrieben oder im
Speicherdialog unter einem neuen Namen abgelegt werden. Während eines laufenden
Zyklus sind Neu, Öffnen und Bearbeiten gesperrt.

### Roadmap-Zyklus starten

1. Konfiguration öffnen. Das YOLO-Modell wird automatisch in einem Worker geladen;
   **Load YOLO model** erlaubt einen manuellen Reload. Bei einer gemappten Klasse
   steht die Roadmap-Pose groß im Box/OBB-Overlay; daneben steht nur die Konfidenz
   in Prozent. Die YOLO-Klassen-ID wird im Bild nicht mehr angezeigt.
2. Die GUI liest alle zugeordneten Transition-Profile und zeigt deren UR-Ry-Winkel
   und Förderbandgeschwindigkeit. Stimmen beide Werte überall überein, werden die
   Hardwarefelder automatisch gefüllt. Bei Abweichungen Werte auswählen und
   **Use machine parameters** drücken.
3. **Connect all components** drücken, beide Leuchten einschalten/einstellen und
   deren Zyklus-Checkbox bestätigen. Falls UR aktiv ist, **Apply UR angle** drücken.
4. Sobald alle Preflight-Zeilen grün sind, **Start cycle** drücken. Erst jetzt wird
   der Drei-Frame-Konsens gesammelt. Die erkannte Pose bestimmt den eindeutigen
   direkten oder zweistufigen Pfad.
5. Nur die aktiven Arrays der Pfadprofile werden zusammengeführt. Jedes physische
   Array darf im Pfad höchstens einmal belegt sein. Das SPS-Profil wird zunächst bei
   gestopptem Band geschrieben und zurückgelesen; Freigaben und Band folgen zuletzt.

Eine erkannte Zielpose fährt mit Arraymaske null durch. Dieser Fall erzwingt
unabhängig von zuvor ausgewählten Übergängen zusätzlich alle vier Array-Enables
auf `false`. Eine nicht erreichbare oder mehrdeutige Pose endet vor jedem
Aktuierungswrite als sichtbarer Fehler.

### Hardware einstellen

Unter **Configuration → Hardware settings …** können Kamera-IP/-Seriennummer,
Baumer-CTI, SPS-IP/AMS-Net-ID/ADS-Port und beide Neewer-BLE-Adressen geändert werden.
Die Kamera-Vorschau ist dort auf 1–60 FPS begrenzbar (Standard: 15 FPS); niedrigere
Werte reduzieren die GUI-Last bei hochauflösenden Kamerabildern.
Nach dem Speichern muss die Anwendung einmal über das Desktop-Symbol neu gestartet
werden. Bleiben beide Lichtadressen leer, sucht die Anwendung beim nächsten
Verbindungsaufbau nacheinander zwei unterschiedliche Panels und speichert die
gefundenen Adressen.

Im oberen Hardwarebereich wird nach dem Kameraverbindungsaufbau ein logarithmischer
**Exposure**-Slider freigeschaltet. Er verwendet den von der Baumer-Kamera gemeldeten
Min-/Max-Bereich und sendet erst 250 ms nach der letzten Änderung. Beim Trennen wird
der ursprüngliche Exposure-/Auto-Zustand wiederhergestellt. Die Anzeige dahinter
unterscheidet `cam` (Kamera-Node), `raw` (gemessener Bufferabruf) und `view`
(tatsächlich dargestellte Vorschau) in FPS.

**Disconnect all components** cancels pending BLE work and releases both panels,
the camera, and ADS without waiting in the GUI thread. Light connections are made
one after another because simultaneous WinRT/Bleak discovery and GATT connection
can be unreliable. Each panel owns a private asyncio worker thread, so even a
blocking Windows-GATT call cannot block Qt. Connect and command timeouts release
the stale BLE client and leave a clean, retryable error instead of retaining a
stale connection.

Reorientation Control and Automated Image Capture share a Windows camera/light
lease. Reorientation Control, Pressure Control, and Conveyor Setup additionally
share the PLC-control lease `Local\BiBaZuPlcControl`. Starting a second conflicting
application now shows a clear message and performs no hardware connection. The two
panel connections use one central serial retry sequence; individual panel reconnect
loops are disabled to prevent overlapping WinRT scans. Connect, cancel, and BLE
shutdown work is bounded so a faulty driver cannot freeze Qt or hold the application
open indefinitely.
All BLE, YOLO, and UR worker results cross into the UI through QObject slots;
worker-thread signals never call labels, dialogs, or MainWindow state through
anonymous Python callbacks.

**Start conveyor** and **Stop conveyor** provide manual transport independently of
YOLO, lights, and part configuration. The speed comes from the existing Conveyor
speed field. Manual start is available only with ADS connected, PLC reorientation
state 0, valid calibration, and a stopped fault-free drive; it explicitly selects
forward travel and writes all four array enables false. During an automatic cycle,
manual start is locked and Stop conveyor requests the normal coordinated abort.

Reloading a YOLO model now retires the previous inference worker asynchronously and
starts the requested model automatically after that worker has exited. Model readiness
is cleared immediately during this handover, so a cycle cannot enter Detecting with a
stopped worker.

The Baumer preview has explicit backpressure: at most one converted frame may be
waiting for Qt, and large sensor images are reduced in the camera worker before
they enter the GUI thread. Repeated camera-fresh/preflight values are suppressed,
so the checklist is rebuilt only when a check really changes rather than once per
frame. A real four-device soak on 2026-08-11 connected ADS, the camera, and both
panels together; the preview ran at 15 FPS without an application hang.

## Operating contract

- Schema v1 requires exactly the model classes `0 = Pose 1`, `1 = Pose 2`.
  Schema v2 uses the explicit class mapping from YAML; extra model classes are
  permitted, but any detection of an unmapped class blocks consensus.
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
implements a 250 ms GUI heartbeat, 2 s watchdog, 60 s LB8 timeout, and 35 s drain
timeout. LB8 falling is latched in the 1 ms task; completion additionally requires
the exact expected trigger mask, all four array states idle, no pending trigger,
and all 24 valves closed.

Before hardware acceptance, verify online that the PLC-normalized clear state of
all six `LightBarrierStableN` signals is `TRUE`. Also resolve the documented UR
fixed-orientation discrepancy (`Rz=-90°` versus the installation's possible
`Rz=180°`).

`PressureControlGUI` and `ConveyorSetupGUI` refuse their initial write while the
reorientation owner is active. The shared process lease also prevents these three
control applications from being started together. Close Pressure Control after
creating/saving a profile and before starting Reorientation Control. Other ADS
writers are prohibited during a cycle. The PLC watchdog and GUI stop are not
safety-rated; physical emergency stop and pneumatic pressure relief remain mandatory.

## Tests

```powershell
uv run pytest
uv run ruff check src tests
```

Hardware tests require a separately approved commissioning session. Do not run
them on a pressurized system without an operator at the physical emergency stop.
The current offscreen suite contains 92 tests.
