# Conveyor control — current Device 3 configuration

Use the existing project **`TwinCAT Projekt3 - Kopie/TwinCAT Projekt3.sln`**.
It has been updated in place. The additional `Device3_Conveyor` copy has been
removed. Earlier CSTCA projects, instructions and diagnostic evidence are
archived under workspace `.tandem_validation`.

## Hardware and mode

| Setting | Current value |
| --- | --- |
| Controller | `10.145.4.14.1.1` |
| Motor terminal | Device 3 > Term 6 (EK1100) > Term 7 (EL7201-0010) |
| EtherCAT master / slave address | `10.145.4.14.4.1` / `1002` |
| Motor serial | `00306149` — formerly Term 22 |
| Operating mode | **CSV, 9**, no NC axis |
| Physical motor count | **1**, using logical motor-1 variables |
| Roller / gearbox / direction | 50 mm / none / +1 |
| Encoder counts per revolution | 1048576 mapped counts |
| Velocity factor | 268435 raw units per revolution/second |
| Nominal supply | 24000 mV |
| Initial maximum speed / acceleration | **5 rpm / 5 rpm/s** |

Five rpm corresponds to approximately **13.09 mm/s** at the roller. Higher GUI
requests are limited by the PLC. Speed/acceleration configuration is frozen after
initialization; changes require a stopped reset.

## Existing GUI controls

The normal MAIN and conveyor/calibration/batch interfaces are restored. Existing
GUIs keep the same controller, ADS port 851, and symbol names:

| GUI function | PLC symbol |
| --- | --- |
| Enable | `MAIN.GuiConveyorEnabled` |
| Speed in mm/s | `MAIN.GuiConveyorSpeedMmPerSec` |
| Reverse | `MAIN.GuiConveyorReverse` |
| Stop | Disable conveyor, or `MAIN.GuiCalibrationStop` |
| Reset | `MAIN.GuiConveyorReset` |
| Calibration / finite jog | `MAIN.GuiConveyorCalibrationMode`, `GuiCalibrationMoveLeft/Right` |
| Jog distance / speed | `MAIN.GuiCalibrationJogSteps`, `GuiCalibrationJogSpeedFullStepsPerSec` |
| Ready / busy / fault | Existing `MAIN.StepperPos*` status symbols |

`Stepper*` names are compatibility signals, not physical EL7047 commands.
`MAIN.ConveyorServo1*` now refers to Device 3. Motor-2 symbols remain available
to ADS clients but have no hardware links and do not block readiness. No partner
feedback is fabricated. Active-motor fault, communication, position, stop and
timeout monitoring remains enabled.

`TestStart`, `TestAbort`, test torque/commutation-angle outputs and `FB_Cstca*`
blocks are removed from the active project. `ConveyorDriveSettings` checks actual
mode 9, automatic brake control and zero torque offset through CoE. The conveyor
cannot enable unless those live checks are valid.

## Mapping and startup

Output PDOs **1600, 1601** carry controlword and target velocity (6 bytes).
Input PDOs **1A00, 1A01, 1A02, 1A0C** occupy 12 bytes. Eight links connect
position, statusword, actual velocity, feedback validity, working-counter state,
EtherCAT state, controlword and target velocity to `MAIN.ConveyorServo1*`.

Device 1, Device 2 and non-conveyor mappings are preserved from the saved scan.
Normal application logic controls those I/O again; the earlier test program's
unconditional zeroing of every output is removed. Conveyor/nozzle-array enables
start FALSE and speed/pressure requests start zero. Loading does not itself
request movement.

The matching I/O configuration and PLC must be loaded together. Writing mode 9
alone cannot correct a torque-mode PDO layout. After loading, check
`ConveyorDriveSettings.Valid`, Device 3 OP, and zero motor commands before using
the GUI. [Beckhoff CSV without NC](https://infosys.beckhoff.com/content/1033/el72x1-001x/1859316107.html)

## Verification and hardware history

The updated source compiled with **zero TwinCAT errors**. All **eight motor
links**, **two pressure-input links**, and **34 startup entries** were verified.
The actual ST passed **21 behavior tests**, including single-motor finite moves,
stop, reset and fault handling. The **five updated I/O/GUI contract tests** also
passed. Software checks do not establish successful hardware movement.

After shortening the shielded cable, the earlier Term-22 CSTCA test passed a
250 ms zero-torque enable at 10:06 UTC on 2026-09-14; the earlier cable arrangement
had faulted at enable. This supports the wiring change helping, but was not a
loaded CSV test. Full history is archived in
`.tandem_validation/earlier_cstca_docs/TANDEM_CONVEYOR_before_device3.md`.

## Loaded and live movement verified, 2026-09-14

The existing project was uploaded and its matching CSV I/O activated. After the
controller restart, readback at **15:09:11 UTC** confirmed PLC RUN, actual mode 9,
valid CoE/feedback checks, motor serial 00306149, configured single-motor operation,
and zero conveyor/nozzle commands. Earlier load attempts ended before target changes.

At **15:11:13 UTC**, a single finite move used the existing GUI calibration symbols:
10 virtual full steps (**18 degrees requested**) at 6.6667 full steps/s (about
**2 rpm requested**). The motor moved **51260 encoder counts / 17.5987 degrees**
and completed normally within the existing 0.25-full-step (0.45-degree) position
tolerance. Completion including ramps and settling took approximately **3.915 s**.
No drive, communication or adapter fault was observed.

Encoder displacement through the middle of the move gave approximately **2.13 rpm**.
The trajectory correction briefly raised the velocity command to **3.23 rpm**,
within the configured 5 rpm limit. The 2 rpm value was the requested jog speed,
not a claim of perfectly constant instantaneous speed.

Cleanup verified controlword and target velocity zero, final statusword **96**,
automatic brake control, stationary encoder, and GUI enable/calibration mode off.
The temporary jog settings were restored. The other motor had no hardware links
or output commands, and all nozzle-array permits remained off. This demonstrates
the existing GUI command interface working with Device 3 for this short unloaded
CSV movement; continuous/loaded conveyor performance remains untested.

Evidence: `.tandem_validation/device3_load_20260914T150713Z/after.json` and
`.tandem_validation/device3_gui_jog_20260914T151113Z.json`. The successful build
and source-copy hash verification are retained under `.tandem_validation`.
