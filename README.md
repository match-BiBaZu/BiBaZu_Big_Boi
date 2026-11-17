# BiBaZu_Big_Boi

- Software and PLC project for the large BiBaZu pneumatic part-reorientation test stand.
- Combines conveyor transport, camera-based pose detection, timed nozzle impulses, calibration, and experiment logging.

## Annotated feeder prototype

<a href="https://match-bibazu.github.io/assets/bibazu-feeder-annotated.pdf">
  <img src="https://match-bibazu.github.io/assets/bibazu-feeder-annotated.webp" width="600" alt="Close-up of the BiBaZu feeder showing the high-speed camera, light-barrier sensors, wall and belt nozzle arrays, PTFE wall plate, conveyor belt, and example workpieces">
</a>

[View the full-size figure (PDF)](https://match-bibazu.github.io/assets/bibazu-feeder-annotated.pdf) · [More prototype photos](https://match-bibazu.github.io/#prototype)

## Applications and tools

- **Pressure Control** (`CSVSaver/PressureControlGUI.py`): configure four nozzle arrays, pressure, pulse duration, delay, and offset.
- **Profiles and roadmaps**: save/load pressure JSONs, select directed pose transitions, and preview CAD orientations.
- **Conveyor Setup** (`CSVSaver/ConveyorSetupGUI.py`): control transport, calibrate travel and sensor spacing, and check part velocity.
- **Colibri probe calibration** (two tabs in Conveyor Setup): measure light-barrier centres and individual nozzle distances using the 75 mm Colibri axis and QuantumX TAL221 channels 3/4. [Setup, operation and hardware acceptance](docs/COLIBRI_CALIBRATION.md).
- **Reorientation Control** (`ReorientationControlGUI/`): detect poses with Baumer/YOLO and execute roadmap profiles for a fixed batch from one camera snapshot.
- **Camera timing** (`CSVSaver/high_speed_calibration.py`): record USB high-speed video, review frames, and compare light-barrier-to-motion delays.
- **Measurement tools**: plot light-barrier events, measure force-response delays, and export pressure, velocity, and consistency data.
- **UR16e integration** (`CSVSaver/UR16e/`): robot programs and RTDE tools for chute-angle adjustment and calibration.
- **AutoTune integration**: launch the separate AutoTune utility and expose array settings through a local [Pressure Control API](docs/pressure-control-api.md).

Roadmap generation and route construction live in the separate
`BiBaZu_StableComponentenRoadmaps_grouped` application. Both GUIs read its final
exports directly (`BIBAZU_ROADMAPS_DIR` overrides the catalogue). Big Boi stores
calibration profiles and runtime configuration, and selects only exported routes.

## Hardware and connections

- **Chute surface**: polytetrafluoroethylene (PTFE) plate with 4 mm holes.
- **Conveyor belt**: polyethylene mesh with a 1 mm grid size.
- **TwinCAT controller**: [Beckhoff C6043 IPC](https://www.beckhoff.com/en-en/products/ipc/pcs/c60xx-ultra-compact-industrial-pcs/c6043-0090.html) with Intel Core i9 CPU and NVIDIA 2000 GPU running [TwinCAT](https://www.beckhoff.com/en-en/products/automation/twincat/); AMS Net ID `10.145.4.14.1.1`, ADS port `851`; PLC task runs every `1 ms`.
- **[EtherCAT](https://www.beckhoff.com/en-en/products/i-o/ethercat/) layout**: Device 1 carries general I/O; Device 2 carries Festo pneumatics; Devices 3 and 4 each carry one conveyor drive.
- **Air nozzles**: 24 [Lechler Series 600.484.56](https://www.lechler.com/my-en/products/general-industry/airnozzles/flat-jet-nozzles/series600-484-56) nozzles in four arrays of six.
- **Fast-switching valves**: 24 [Festo MHJ9-QS-6-HF](https://ftp.festo.com/Public/PNEUMATIC/SOFTWARE_SERVICE/Datasheet/EN_US/567790.pdf) solenoid valves, one per nozzle. Device 1 terminals 18/19 (`EL2008`) map outputs 1–16. Terminals 20/21 (`EL2004`) map outputs 17–24.
- **Pressure regulation**: two [Festo Motion Terminal VTEM units](https://www.festo.com/us/en/p/motion-terminal-id_VTEM) exchange process data through Device 2, Box 5 (`CPX-FB38 64Byte`).
- **Light barriers**: eight [Panasonic EX-13EB](https://industry.panasonic.com/global/en/products/fasys/sensor/photoelectric/number/ex-13eb) sensors on terminal 22 (`EL1018`), channels 1–8; pairs 1–2, 3–4, 5–6, and 7–8 serve arrays 1–4.
- **Analog measurements**: terminal 250 (`EL3068`, 0–10 V), channels 1/2, maps `RawNozzlePressure` / `RawNozzlePressure2` for pressure/force-delay measurements.
- **Camera trigger**: terminal 27 (`EL2004`), channel 1, maps `CameraRecordTrigger` for a selectable light-barrier recording pulse.
- **Conveyor motors**: two [Beckhoff AM8112](https://www.beckhoff.com/en-en/products/motion/rotary-servomotors/am8100-servomotors-for-compact-drive-technology/am8112.html) servos, each with a 32:1 gearbox and 50 mm roller; shared velocity commands.
- **Servo connections**: Device 3 → terminal 6 (`EK1100`) → terminal 7 (`EL7201-0010`); Device 4 → terminal 9 (`EK1100`) → terminal 10 (`EL7201-0010`).
- **Other configured terminals**: `EL1014-0010` digital inputs, the former `EL7047` stepper stage, and `EL9011` end terminals; the first two have no current PLC mappings.
- **Camera**: [Baumer VLXT-28M.I](https://www.baumer.com/int/en/product-overview/industrial-cameras-image-processing/industrial-cameras/lxseries/vlxtcameras/10-gigecameras-with-copper-cable-10gbase-t-/vlxt-i/vlxt-28m-i/p/42399) with a [Ricoh FL-CC2514-5M lens](https://www.optowl.com/en/_assets/data/FL-CC2514-5M.pdf) (25 mm, 5 MP).
- **Other PC-connected peripherals**: two [Neewer RGB660 Pro II](https://neewer.com/products/neewer-upgraded-660-pro-ii-2-pack-rgb-led-video-light-66601573) BLE panels and a [UR16e robot](https://www.universal-robots.com/products/ur16e/) over Ethernet/RTDE.
- **Connection source**: [TwinCAT system project](CSVSaver/TwinCAT%20Projekt3%20-%20Kopie/TwinCAT%20Projekt3/TwinCAT%20Projekt3.tsproj); PC peripherals are configured in the Python applications.

## Operating specifications

The image recognition model is [YOLO26n](https://docs.ultralytics.com/models/yolo26).

### Performance specifications

| Limit | Value |
| --- | --- |
| Maximum supply pressure | 6 bar per nozzle |
| Maximum tested workpiece mass | 74 g |
| Maximum tested workpiece dimension | 80 mm |
| Maximum tested feed rate | 60 parts/min |

### Conveyor settings and calculated feed rates

| Belt speed | Workpiece pitch | Calculated feed rate | Status |
| --- | --- | --- | --- |
| 100 mm/s | 200 mm (20 cm) | 30 parts/min | Tested |
| 200 mm/s | 200 mm (20 cm) | 60 parts/min | Tested |
| 200 mm/s | 100 mm (10 cm) | 120 parts/min | Coming soon |

Rates are calculated as belt speed divided by workpiece pitch, multiplied by 60.

## Repository layout

- [`CSVSaver/`](CSVSaver/): pressure/conveyor GUIs, ADS communication, calibration, logging, and hardware helpers.
- [`CSVSaver/TwinCAT Projekt3 - Kopie/`](CSVSaver/TwinCAT%20Projekt3%20-%20Kopie/): TwinCAT solution, I/O mappings, and PLC control logic.
- [`ReorientationControlGUI/`](ReorientationControlGUI/): packaged Qt application, part YAMLs, external-roadmap readers, hardware adapters, and tests.
- [`pressure_profiles/`](pressure_profiles/): saved experiment settings.
- [`WindowsLaunchers/`](WindowsLaunchers/): Desktop/Start menu shortcuts and application icons.
- [`docs/`](docs/): integration documentation; tests also live beside the tools in `CSVSaver/`.

## Setup and start

- Use Windows, Python 3.12, `uv`, TwinCAT 3 XAE, and an ADS route to the controller.
- Install Baumer Camera Explorer/GenTL producers for camera features and the bundled Festo VTEM library for PLC builds.
- Install Pressure Control and Conveyor Setup dependencies from the repository root:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

- Start either tool from `CSVSaver`:

```powershell
cd CSVSaver
..\.venv\Scripts\python.exe .\PressureControlGUI.py
# Or: ..\.venv\Scripts\python.exe .\ConveyorSetupGUI.py
```

- Reorientation Control includes its own geometry dependency and CAD; keep the local `BiBaZu_StableComponentenRoadmaps` repo beside this repo for roadmaps.
- Install and start Reorientation Control from this repository's root:

```powershell
uv sync --project ReorientationControlGUI --extra dev
uv run --project ReorientationControlGUI bibazu-reorientation
```

- Install optional shortcuts with [`WindowsLaunchers/Verknuepfungen-installieren.cmd`](WindowsLaunchers/Verknuepfungen-installieren.cmd).
- Build/download the matching PLC source before using changed interfaces; opening a GUI does not deploy TwinCAT changes.
- Check PLC addressing: Pressure/Conveyor tools default to `192.168.10.23`; Reorientation Control defaults to `192.168.0.23`.
- Run one PLC-control GUI at a time; close other camera/light clients before connecting those devices.
- AutoTune runs in a separate repository; conveyor start/stop remains manual during API sessions.

## Further documentation

- [Reorientation Control](ReorientationControlGUI/README.md): configuration, production workflow, run exports, and tests.
- [Standalone geometry](ReorientationControlGUI/docs/standalone-geometry.md): bundled dependencies, portable data, and older configuration limits.
- [Conveyor commissioning](CSVSaver/TANDEM_CONVEYOR.md): drive configuration, calibration, and recorded hardware validation limits.
- [TwinCAT project](CSVSaver/TwinCAT%20Projekt3%20-%20Kopie/README.md): build workflow and conveyor interface.
- [Consistency logging](CSVSaver/CONSISTENCY_LOGGING.md): sensor plots, event buffers, and raw data export.
- [UR angle program](CSVSaver/UR16e/README_Continuous_Angle.md): robot setup and angle commands.
- [Pressure Control API](docs/pressure-control-api.md): local endpoints, session ownership, and AutoTune integration.
- [Windows launchers](WindowsLaunchers/README.md): shortcut installation and removal.
