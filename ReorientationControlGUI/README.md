# BiBaZu Reorientation Control

## Individual poses and shared group actions (consumer export v3)

Exports declaring `planning_semantics: pose_to_group` instead require the exact source pose. Choice lists and previews use that pose's commands and accept any member of the destination group. Group-only recognition cannot establish an exact member of a multi-member source group, so execution remains unavailable there until an exact observation is available. Reimport the changed export explicitly; old group-shared profile assignments remain unavailable rather than acquiring new applicability.

The external grouped catalogue now shows individual poses numbered by their own observed frequencies, with membership and percentages in both GUIs. Members below the configured cutoff are omitted and cannot witness transfers. Outgoing actions are shared by all displayed source members; any qualifying target-group member satisfies the target. There are no internal group moves or composite fallback bridges. Missing connectivity is reported without constructing a fallback.

Recognition classes continue to identify groups. The GUI reports the possible pose set rather than claiming a particular member was recognised. Selecting a member accepts its group as the target, including zero actuator enables when already in that group. Source-group calibration and assigned pressure profiles remain required.

After the catalogue changes, explicitly reimport the roadmap. Migration uses stable group keys and the exact previous export hash; old numeric group IDs are never interpreted as new individual pose numbers. Compatible assignments can be retained, while removed/composite assignments and omitted recognition groups remain unavailable. Existing exports and profile metadata remain readable. Generation and route construction remain entirely in the external grouped roadmap application.


Continuous feed is the default production mode: start production, then
add workpieces while it keeps running, with multiple parts in transit. Each start pose gets
one immutable pressure-profile queue record at the movable yellow handoff line.
**Snapshot batch** remains selectable for preloaded stationary parts; that mode
freezes the latest Baumer/YOLO frame and its left-to-right queue order at Start.
Legacy schema-v1 projects
support Pose 1/2; schema-v2 roadmap projects resolve either a direct transition or
one unique simple path through up to `pose_count - 1` assigned transfers and combine its active PressureControl
arrays into one immutable PLC queue record per part.
All operator-facing labels, dialogs, status messages, and validation errors in the
application are in English.

The PLC source supports the two EL7201 servo motors through CoE and cyclic process
data using the existing conveyor ADS variables. Before using the new belt, follow
the [tandem conveyor commissioning guide](../CSVSaver/TANDEM_CONVEYOR.md); the new
motor setup and belt calibration must be verified before motion is enabled.

## Continuous feeding and the yellow profile handoff line

1. Open a part configuration, connect hardware and wait for the YOLO model to be
   ready. Choose **Continuous feed** in Hardware.
2. While stopped, move **Yellow profile handoff line** to the desired horizontal
   position (5–80% of image width). There is one yellow line: it marks both the
   end of pose recognition and commitment of the part's selected profile. Its
   position and production mode are saved between launches.
   **Input boundary** adjusts the ignored region at the right-hand entry side,
   measured as a percentage from the left. The default 90% ignores the rightmost
   10% of the image. A detection is accepted only once its whole box clears that
   boundary; ignored detections have no box/label and never enter the profile queue.
   The input boundary is not drawn. Set it to 100% to disable the entry region.
   Adjust both boundaries before a run; the input boundary stays right of handoff.
3. Press **Start production run**; parts may already be visible upstream of the
   yellow handoff line. A fresh YOLO frame is required, with no empty-zone check. The
   normal stopped Beckhoff preparation runs once, then the belt stays running.
   Wait for **Belt running** before adding parts from the right, moving leftward.
4. The current YOLO class when the part's leading edge reaches the yellow line
   selects its profile exactly once. There is no five-frame confirmation or extra
   tracking box; earlier classes do not lock the selection. Target, unknown or
   unassigned poses pass without firing. The PLC applies the queued arrays when
   that particular part reaches the light barriers. Detections during preparation
   wait until transport is enabled rather than aborting startup.
5. Press **Stop production run** to stop the conveyor and disable every nozzle
   array immediately. Stop cancels pending work and releases ownership after its
   safe-output writes complete. It does not wait for LB8, queue depth zero or PLC
   state 40. The run is recorded as stopped, with unfinished parts not reported as
   completed. Stop also remains available if a snapshot batch is still draining.

Keep the handoff line upstream of LB1 with enough travel distance for inference
and ADS queue acknowledgement at the selected belt speed. The line and production
mode are locked during a run. Vision loss before all visible parts are committed,
invalid tracking order, a full queue, an acknowledgement taking more than ten seconds
after its ADS write completes (or a write waiting more than ten seconds),
or a PLC fault stops continuous operation. Recoverable PLC light-barrier diagnostics
are logged as informational notices without warning banners or conveyor stops.
Feed subsequent parts before previous parts reach LB8; each has its own queue record.
PLC status polling uses cached ADS Sum Reads to reduce acknowledgement delays.
Losing an individual YOLO detection does not stop the run or show a tracking-loss
warning. Expired tracks are discarded; a new detection at the handoff line selects
its current class and profile without requiring uninterrupted upstream tracking.
Once accepted parts are only draining,
camera/YOLO loss is a warning and those frozen profiles finish.
The existing PLC state-20 queue contract already supports this mode; no PLC source
activation is needed for the GUI change. Run exports record the production mode.

## Manual profiles without a roadmap

Select **New configuration** above the camera view or from the **Configuration**
menu, then choose **Manual configuration**. The other option is **Roadmap configuration**.
Manual configuration assigns pressure profiles directly to YOLO start poses.
A roadmap and CAD model are not required.

1. Enter a part name and select the YOLO `.pt` model. Click **Read YOLO classes**
   to populate the start-pose table from the model's class IDs and names. Classes
   may also be added or removed manually; arbitrary class names and IDs are supported.
2. Select the **Target class (pass through)**. For each other start pose that should
   be actuated, browse to one complete pressure JSON that reorients it to this target.
   Each start-pose class has its own assignment, reused for every workpiece in that
   class. No roadmap route search or profile chaining takes place.
3. Save the manual YAML. **Open configuration** restores it; **Edit configuration**
   changes the model, classes, target and profile assignments. Blank assignments
   are allowed in a draft. At least one non-target profile is required to run.
4. If profile speeds disagree, select the common belt speed and press **Use machine
   parameters**. The 200 mm/s prototype limit and every profile's lower maximum
   still apply. Selected profiles must agree on global light-barrier settings.
5. Connect the hardware and choose continuous feed or snapshot batch. Continuous
   feed selects the assigned start-pose profile at the yellow handoff line as each
   new part arrives. Snapshot batch freezes the preloaded parts at Start. Target,
   unknown and unassigned poses pass through with all nozzle enables false.

Manual configurations use schema version 3 and save portable model/profile paths.
Manual pose IDs match the YOLO class IDs directly: Wago classes 0–7 are Pose 0–7,
and class 0 can be the pass-through target. Older manual YAMLs with IDs offset by
one are resolved through their saved class mapping when loaded, preserving every
profile assignment without rewriting the original file. Saving from the editor
writes the class IDs directly.
Legacy two-pose and roadmap configurations remain supported. Run exports record
manual mode and copy the selected profiles; each part records the actual assigned
profile rather than attributing a profile to a pass-through part.

## Installation and start

- Roadmap generation, geometry analysis, and route construction belong to the external grouped roadmap application.
- CAD/STEP pairs are bundled in `src/bibazu_reorientation/assets/geometry`.
- Roadmaps come directly from the sibling `BiBaZu_StableComponentenRoadmaps_grouped` checkout; `BIBAZU_ROADMAPS_DIR` can select another local path.
- See [standalone geometry](docs/standalone-geometry.md) for source versions and older configuration limits.

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

Use **Configuration → New configuration … → Roadmap configuration** to select a pose roadmap first.
The dialog accepts the handover YAML (`part`, `poses`, `transitions`) and the
internal JSON export (`source`, `nodes`, `edges`). It derives the part/CAD data,
robust poses and directed transitions, then asks for the `.pt` model, explicit
model-class mapping, target-pose card, and optional Pressure JSON per eligible
edge. The generated schema-v2 YAML stores all paths relative to its own location.
Legacy pressure profiles resolve omitted machine values only after the first PLC
baseline read. Selecting a YAML or profile never writes to the PLC.

Pressure Control JSON profile versions **1–13** are supported, including the current
WagoKlemme exports. The four arrays, six nozzle selections, pressure, delay, pulse
and offset are read directly. Calibration, sensor-spacing snapshots and force
response snapshots do not replace the live machine settings. The version-13
`conveyor_velocity_delay` checkbox belongs to Pressure Control's manual offset
calculation; Reorientation keeps its existing PLC batch calculation using measured
light-barrier speed. Future profile versions are rejected until reviewed.

Each profile-eligible transition has a **Preview …** button. The preview loads the
selected STL/OBJ once and animates the directed change from the source-pose
quaternion to the target-pose quaternion. It also shows both endpoint views, the
commanded signed angle, and the edge ID. Playback can be paused, scrubbed, or
restarted. If CAD rendering is unavailable, the dialog keeps the roadmap's start
and target images with a direction arrow. This is an orientation aid, not a
simulation of the physical trajectory through the chute.

The setup dialog reads optional **MULTIPLE REORIENTATION** choices from the finished
`consumer_export` records. The external roadmap application constructs their commands,
preview segments, component IDs and legacy identities. A calibrated profile may cover
a complete exported transfer; an exported execution route may instead name its
individual calibrated profiles.

Grouped roadmap exports can additionally contain `composite_transfers`: separate
geometry-only Y/Z rotation candidates between fixed representative poses. Setup
shows their ordered commands and transient orientation previews. These candidates
have no verified contact/settling model and require calibration of a complete
transfer profile. They become runtime route legs only after a pressure profile is
explicitly assigned. Optional exported `routes` and `connectivity` describe
orientation-level reachability and do not assert hardware feasibility.

Legacy two/three-flip route IDs and saved assignments remain supported through the
external export. Control applications do not search the graph or invent missing
routes. Saved assignments for choices absent from an older export are retained
for later recovery, without making them executable. If no exported choice has the required profiles and coverage, calibration
or an offline roadmap upgrade is required. Parallel assigned choices retain ambiguity
errors. Older files expose their explicit transitions without derived routes.

Consumer-export **version 2** treats groups as whole member sets. The exporter
prioritizes exact ±90°/±180° X/Y/Z commands, retaining other rotations as alternatives.
Setup lists the exported reorientation names in that priority order. Previews
use actual member witnesses and settling segments for each leg. Arrival and
departure members of an intermediate group may differ under the exported
group-interchangeability planning assumption; the preview adds no switching motion.

Assign a pressure profile and explicitly check **Calibrated for source group**
for each group choice used by runtime. The saved `transition_group_coverage`
declaration binds that transfer to its stable source-group identity. Matching
Pressure Control metadata can restore it. Older profiles and member-coverage
declarations remain supported without automatic group qualification. Recognition
mappings and representative thumbnails are unchanged. Geometry-only choices stay
available for inspection and calibration until qualified.

Version-1 consumer exports retain their original per-member coverage rules.
Both versions use finished exported routes; this application performs no route
search or geometric feasibility checks.

### Bauteil, YOLO-Modell und Pressure-Profil auswählen

In der Anwendung gibt es dafür zwei gleichwertige Wege: die Schaltflächen oberhalb
des Kamerabildes oder das Menü **Configuration**.

1. **New configuration … → Roadmap configuration** beginnt mit einer Roadmap in YAML oder JSON.
2. Name und CAD-Pfad werden übernommen, bleiben aber editierbar. Nur robuste
   Posen erhalten eine explizite, eindeutige YOLO-Klassen-ID und sind als Ziel
   auswählbar.
3. Für jede aktuierte Robust-zu-Robust-Kante kann optional ein Pressure-Profil
   ausgewählt werden. Passive/metastabile Kanten bleiben sichtbar, aber
   schreibgeschützt. Gruppierte Exporte zeigen ihre fertigen Gruppenoptionen.
4. Fehlende Profile sind als Entwurf erlaubt. Die Readiness-Anzeige nennt fehlende
   Profile, erreichbare Startposen, Mapping- und Hashstatus sowie Abweichungen bei
   Name/CAD-Pfad.
5. Beim Laden wird der SHA-256 der Roadmap geprüft. Nach einer Änderung muss
   **Re-import roadmap** bestätigt werden; nur identische Pose- und Kanten-IDs
   behalten ihre Zuordnungen.
6. Schema-v1-Zwei-Posen-Dateien bleiben unverändert ladbar und ausführbar.
   Roadmaps mit Export-Routen können ausgeführt werden, sobald ein qualifizierter
   exportierter Pfad zum Ziel vorhanden ist; es gibt keine Drei-Kanten-Grenze. Parallele belegte Kanten werden mit ihren `edge_id` als mehrdeutig
   blockiert.

Mit **Edit configuration** wird die aktuell geladene YAML vollständig
vorausgefüllt geöffnet. Sie kann am bisherigen Ort überschrieben oder im
Speicherdialog unter einem neuen Namen abgelegt werden. Während eines laufenden
Produktionslaufs sind Neu, Öffnen und Bearbeiten gesperrt.

### Snapshot batch: start a production run

1. Konfiguration öffnen. Das YOLO-Modell wird automatisch in einem Worker geladen;
   **Load YOLO model** erlaubt einen manuellen Reload. Bei einer gemappten Klasse
   steht die Roadmap-Pose groß im Box/OBB-Overlay; daneben steht nur die Konfidenz
   in Prozent. Die YOLO-Klassen-ID wird im Bild nicht mehr angezeigt.
2. The GUI compares conveyor speeds in all assigned transition profiles. Matching
   valid speeds load automatically. Otherwise select a positive speed up to
   **200 mm/s** (and within each profile's lower limit) and press **Use machine
   parameters**. Legacy two-pose configurations also support this speed override.
   Old UR angle metadata is ignored; no robot connection or acknowledgement is needed.
3. Press **Connect all components**. Before manual transport or production starts,
   the GUI automatically stops and prepares both Beckhoff motors using the existing
   velocity-only PLC configuration. It retains the PLC acceleration setting.
   Light settings remain operator information and do not block production start.
4. Alle Werkstücke müssen im Kamerabild liegen. Danach **Start production run**
   drücken. Genau der zu diesem Zeitpunkt letzte frische YOLO-Frame wird kopiert;
   spätere Bilder ändern weder Reihenfolge noch Pose noch Profil.
   Die verschiebbare gelbe Linie markiert die Pose-Gültigkeitsgrenze. Sobald eine Bounding Box diese Linie erreicht oder links davon liegt,
   blendet die Vorschau ihre Pose aus und die Startaufnahme ignoriert die Vorhersage.
5. Die GUI sortiert alle Detektionen von links nach rechts (das führende Teil
   zuerst), erzeugt pro Teil genau einen unveränderlichen Queue-Datensatz und wartet
   auf jede SPS-Bestätigung. Erst danach wird das Förderband eingeschaltet.
   Stark überlappende Boxen bleiben als getrennte Werkstücke erhalten, solange ihre
   Mittelpunkte nicht nahezu zusammenfallen; dies reduziert Zusammenführungen durch
   schattenbedingt lange Ql1i-OBBs.
   Zielpose, unbekannte Klasse, angeschnittene Detektion oder nicht eindeutiger Pfad
   werden mit Maske null eingereiht und passieren ohne Düsen; dies erzeugt eine
   Warnung, stoppt den Lauf aber nicht.
6. Direkt nach dem Förderbandstart sendet die GUI `Finish`. Die SPS bleibt im
   Drain-Zustand und stoppt das Band erst, wenn jedes vorab eingereihte Teil LB8
   passiert hat, alle Array-FIFOs leer und alle Ergebnisse protokolliert sind.

Die SPS friert Düsenmaske, Druck, Delay, Puls und Offset je Teil ein. Kamera- oder
YOLO-Ausfall nach dem Start-Snapshot wird nur noch als Warnung angezeigt und
protokolliert; der eingefrorene Lauf wird fortgesetzt. SPS-Watchdog-, Antriebs-,
Ventil- und Queue-Vertragsfehler bleiben harte Stopbedingungen.

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
All BLE, YOLO, and ADS worker results cross into the UI through QObject slots;
worker-thread signals never call labels, dialogs, or MainWindow state through
anonymous Python callbacks.

**Start conveyor** and **Stop conveyor** provide manual transport independently of
YOLO, lights, and part configuration. The speed comes from the existing Conveyor
speed field. Manual start requires only ADS, an idle production run, and a positive
speed; it explicitly selects forward travel and writes all four array enables false.
A latched fault from the previous automatic run is reset automatically before the
manual start. During production, manual start is locked and Stop conveyor requests
the same immediate production stop.

Conveyor preparation is asynchronous and leaves the Qt event loop responsive.
Preflight allows a running conveyor; Start stops it automatically for preparation.
Snapshot workpiece count is not a preflight requirement. At Start, an oversized
snapshot is still rejected before PLC writes; an empty snapshot finishes without
queue records. Use continuous feed to keep running while new parts are added.
Preparation verifies `ConveyorServoVelocityOnly`, waits for standstill, selects two motors
with directions -1/-1, ratio 1.0 and no extra RPM cap, then resets and verifies
fault-free stopped status. Acceleration stays at its live PLC value (0.1–300 rpm/s);
change it through the existing conveyor setup tools. Each wait is bounded to eight
seconds. **Stop conveyor** and **Cancel production start** remain available during
preparation. Cancellation, connection loss and shutdown invalidate queued start
commands and delayed completion callbacks. No preparation step enables transport
or nozzle arrays. Production still freezes the frame at Start and acknowledges
the complete queue before motion. A missing prototype PLC symbol blocks connection
or preparation instead of being replaced by a successful default.

Reloading a YOLO model now retires the previous inference worker asynchronously and
starts the requested model automatically after that worker has exited. Model readiness
is cleared immediately during this handover, so a run cannot start with a stopped
worker.

The Baumer preview has explicit backpressure: at most one converted frame may be
waiting for Qt, and large sensor images are reduced in the camera worker before
they enter the GUI thread. Repeated camera-fresh/preflight values are suppressed,
so the checklist is rebuilt only when a check really changes rather than once per
frame. A real four-device soak on 2026-08-11 connected ADS, the camera, and both
panels together; the preview ran at 15 FPS without an application hang.

## Operating contract

- Schema v1 requires exactly the model classes `0 = Pose 1`, `1 = Pose 2`.
  Schema v2 and v3 use the explicit class mapping from YAML. Continuous feed uses
  the current class at the yellow line; unmapped classes pass without firing.
- Continuous feed starts from a fresh frame and clear light barriers, then
  queues each workpiece once at the yellow line without multi-frame confirmation.
  Snapshot batch freezes every detection in a fresh frame at Start and checks
  actual queue capacity before PLC writes; an empty snapshot is allowed.
- The PLC enters state 20 with the conveyor disabled. Snapshot batch acknowledges
  its frozen queue before conveyor enable; continuous feed enables the empty belt
  and acknowledges each newly committed part while remaining in state 20.
- Snapshot camera/YOLO anomalies after acceptance are warnings. Continuous feed
  requires fresh vision until visible parts are committed; loss aborts that run.
- Light connection, last confirmed commands, and the two operator checkboxes remain
  visible and are logged, but they no longer participate in start preflight.
- This application targets the Beckhoff prototype only. Legacy `ur_ry_angle_deg`
  values are ignored during loading, comparison and execution; source profile
  files and their hashes remain unchanged.
- Belt calibration comes from the PLC. Profile application never writes
  `GuiConveyorMmPerFullStep`, `GuiConveyorCalibrationValid` or marker distance.
  Offline display uses the nominal 50 mm roller / 32:1 reduction scale; valid
  live PLC calibration is required before motion.
- Execution speed is positive and at most 200 mm/s, respecting lower profile
  limits. Higher-speed profiles remain readable and need an explicit speed override.
- The PLC owner is acquired only after safe-stop and full configuration readback.
  A fresh heartbeat is written before ownership; Reset and Start remain asserted
  until PLC state/heartbeat acknowledgement. The conveyor/array enables are written
  only after that acknowledgement.
- On completion or abort, raw conveyor and array enables are written false and
  verified before ownership is released.

Run exports are written below
`%LOCALAPPDATA%\BiBaZuReorientationControl\runs`. Each run gets copied
configuration/roadmap/profiles and `run.json`; each handed-off part gets an atomic
annotated PNG and one schema-v2 row in `parts.csv`. Diagnostic logs rotate separately
under the adjacent `logs` directory.

## TwinCAT activation

Activate the changed `MAIN.TcPOU` before using this GUI. The batch contract provides
a 128-part ring, a separate result ring, monotonically acknowledged commits, eight
ordered sensor cursors and one 16-job FIFO per array. It retains the 250 ms GUI
heartbeat and 2 s watchdog. Each even light barrier latches the matching part's
nozzles, pressure, final delay and pulse; LB8 plus all finished array bits completes
that record. A normal Finish has no short cycle timeout and drains every acknowledged
part and result before stopping the conveyor.

Before hardware acceptance, verify online that the PLC-normalized clear state of
all eight `LightBarrierStableN` signals is `TRUE`. This prototype has no UR robot.

`PressureControlGUI` and `ConveyorSetupGUI` refuse their initial write while a live
reorientation owner is active. With the owner inactive, all original global profile
and light-barrier behavior remains unchanged and no queue is required. If a crashed
batch has already reached the PLC fail-stop latch, Pressure Control can explicitly
return it to legacy mode before applying its normal safe stop. The shared process
lease prevents the three local control applications from running together. Other ADS
writers are prohibited during production. PLC watchdog and GUI finish are not
safety-rated; physical emergency stop and pneumatic pressure relief remain mandatory.

## Tests

```powershell
uv run pytest
uv run ruff check src tests
```

Hardware tests require a separately approved commissioning session. Do not run
them on a pressurized system without an operator at the physical emergency stop.
The Reorientation offscreen suite includes simulated ADS tests for conveyor
preparation, cancellation, speed limits and fixed-batch queue ordering.
