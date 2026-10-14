# BiBaZu_Big_Boi

- Software and PLC project for the large BiBaZu pneumatic part-reorientation test stand.
- Combines conveyor transport, camera-based pose detection, timed nozzle impulses, calibration, and experiment logging.

## Applications and tools

- **Pressure Control** (`CSVSaver/PressureControlGUI.py`): configure four nozzle arrays, pressure, pulse duration, delay, and offset.
- **Profiles and roadmaps**: save/load pressure JSONs, select directed pose transitions, and preview CAD orientations.
- **Conveyor Setup** (`CSVSaver/ConveyorSetupGUI.py`): control transport, calibrate travel and sensor spacing, and check part velocity.
- **Reorientation Control** (`ReorientationControlGUI/`): detect poses with Baumer/YOLO and execute roadmap profiles for a fixed batch from one camera snapshot.
- **Camera timing** (`CSVSaver/high_speed_calibration.py`): record USB high-speed video, review frames, and compare light-barrier-to-motion delays.
- **Measurement tools**: plot light-barrier events, measure force-response delays, and export pressure, velocity, and consistency data.
- **UR16e integration** (`CSVSaver/UR16e/`): robot programs and RTDE tools for chute-angle adjustment and calibration.
- **AutoTune integration**: launch the separate AutoTune utility and expose array settings through a local [Pressure Control API](docs/pressure-control-api.md).

## Hardware and connections

- **TwinCAT controller**: AMS Net ID `10.145.4.14.1.1`, ADS port `851`; PLC task runs every `1 ms`.
- **EtherCAT layout**: Device 1 carries general I/O; Device 2 carries Festo pneumatics; Devices 3 and 4 each carry one conveyor drive.
- **Nozzle valves**: four arrays of six; Device 1 terminals 18/19 (`EL2008`) map outputs 1–16, terminals 20/21 (`EL2004`) map outputs 17–24.
- **Pressure regulation**: two Festo VTEM units exchange process data through Device 2, Box 5 (`CPX-FB38 64Byte`).
- **Light barriers**: eight SICK WTE11-2P2432 sensors on terminal 22 (`EL1018`), channels 1–8; pairs 1–2, 3–4, 5–6, and 7–8 serve arrays 1–4.
- **Analog measurements**: terminal 250 (`EL3068`, 0–10 V), channels 1/2, maps `RawNozzlePressure` / `RawNozzlePressure2` for pressure/force-delay measurements.
- **Camera trigger**: terminal 27 (`EL2004`), channel 1, maps `CameraRecordTrigger` for a selectable light-barrier recording pulse.
- **Conveyor motors**: two Beckhoff AM8112 servos, each with a 32:1 gearbox and 50 mm roller; shared velocity commands.
- **Servo connections**: Device 3 → terminal 6 (`EK1100`) → terminal 7 (`EL7201-0010`); Device 4 → terminal 9 (`EK1100`) → terminal 10 (`EL7201-0010`).
- **Other configured terminals**: `EL1014-0010` digital inputs, the former `EL7047` stepper stage, and `EL9011` end terminals; the first two have no current PLC mappings.
- **PC-connected peripherals**: Baumer GigE pose camera, Baumer VCXU-02C USB high-speed camera, two Neewer RGB660 Pro II BLE panels, and a UR16e robot over Ethernet/RTDE.
- **Connection source**: [TwinCAT system project](CSVSaver/TwinCAT%20Projekt3%20-%20Kopie/TwinCAT%20Projekt3/TwinCAT%20Projekt3.tsproj); PC peripherals are configured in the Python applications.

## Repository layout

- [`CSVSaver/`](CSVSaver/): pressure/conveyor GUIs, ADS communication, calibration, logging, and hardware helpers.
- [`CSVSaver/TwinCAT Projekt3 - Kopie/`](CSVSaver/TwinCAT%20Projekt3%20-%20Kopie/): TwinCAT solution, I/O mappings, and PLC control logic.
- [`ReorientationControlGUI/`](ReorientationControlGUI/): packaged Qt application, part YAMLs, pose roadmaps, hardware adapters, and tests.
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
