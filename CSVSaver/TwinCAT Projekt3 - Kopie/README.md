# Two-motor conveyor with existing GUI

**Latest test, 2026-09-15 at 17:14 UTC: Device 3 completed a 10 rpm unloaded
move, with smooth shaft rotation confirmed by the user.** Over a ten-second
plateau, average PLC target / drive actual / encoder-derived speed were
9.966 / 9.887 / 9.934 rpm. The finite move covered 2.08517 revolutions including
ramps, without faults or new drive diagnostics. Instantaneous actual velocity
remained noisy. Device 4 stayed disabled. The two-motor selection and original
5 rpm limit were restored afterward, with both drives disabled and motion
inhibited in the current runtime. The 30/50 rpm stages were not run. Earlier
paired tests failed; tandem and loaded readiness remain unverified. See the
parent `TANDEM_CONVEYOR.md` for evidence and limits of this result.

Open `TwinCAT Projekt3.sln` in this directory. This is the existing conveyor project;
the earlier CSTCA diagnostic projects and their evidence remain archived outside it.

Target controller: `10.145.4.14.1.1`. Motor: **Device 3 > Term 6 (EK1100) >
Term 7 (EL7201-0010)**, master `10.145.4.14.4.1`, slave address **1002**,
AM8112 serial **00306149**. Motor 2 is **Device 4 > Term 9 (EK1100) >
Term 10 (EL7201-0010)**, master `10.145.4.14.5.1`, slave address **1002**,
AM8112 serial **00186867**. Both use **CSV mode 9** with no NC axis.

The restored MAIN implements the existing conveyor GUI, calibration and batch
interfaces. Enable, speed in mm/s, reverse, stop, reset and finite calibration moves
use the same ADS symbols as before. Device 3 is logical motor 1; Device 4 is
logical motor 2. Both motors follow the same trajectory in the same shaft
direction, with 50 mm rollers and no gearbox. Both must be ready to enable.

The temporary `TestStart`, `TestAbort`, torque/commutation-angle outputs and
`FB_Cstca*` blocks have been removed. The normal conveyor adapter explicitly
supports one or two motors; MAIN supplies `ConveyorServoMotorCount`, default 2.
A stopped reset can select 1 for Device 3 alone, with Device 4 outputs zero.
The main program still checks both physical drives' health. All active-motor
fault, feedback, stop and timeout monitoring remains enabled. Changing motor
count in the FB requires stopped reconfiguration; no partner feedback is fabricated.

## Initial limits and mapping

- Maximum **5 rpm**, acceleration **5 rpm/s**; speed requests above the limit are
  limited by the PLC. At a 50 mm roller, 5 rpm is approximately **13.09 mm/s**.
- Direction +1; 1048576 mapped encoder counts/revolution; 268435 velocity units
  per revolution/second; nominal DC supply 24000 mV.
- Automatic holding-brake control. PLC CoE checks require actual mode 9,
  manual brake release FALSE and torque offset zero on both masters before
  commissioning is enabled. Each motor has its own `ConveyorDriveSettings` instance.
- Output PDOs `1600`, `1601`: controlword and target velocity (6 bytes).
- Input PDOs `1A00`, `1A01`, `1A02`, `1A0C`: position, statusword, actual velocity,
  feedback-validity flag (12 bytes); working-counter and EtherCAT-state links also
  participate in eight mappings per motor, sixteen total.
- Conveyor enable, calibration mode and nozzle-array permits start FALSE;
  speed and pressure requests start zero. PLC startup does not request movement.

The existing Device 1 and Device 2 I/O and non-conveyor mappings are preserved
from the user's saved scan. Normal application behavior for those I/O returns
with the restored MAIN; this is no longer the diagnostic program that forced
every machine output to zero on every scan.

## Loading and first movement

The PLC and I/O configuration must be loaded together. Changing `7010:03` alone
cannot replace a mismatched torque/velocity PDO layout. After activation and PLC
download/start, verify `ConveyorDriveSettings.Valid = TRUE` and
`ConveyorDriveSettings2.Valid = TRUE`, both motors' controlwords and targets zero,
and Devices 3 and 4 OP. Normal controls are in the existing GUI.

The earlier single-motor movement used the existing finite calibration jog:
10 virtual full steps = **18 degrees**, at 6.6667 full steps/s = approximately
**2 rpm**. The PLC ramps and stops at the finite target; the motor should then
be disabled. This move completed on 2026-09-14 at 15:11 UTC: measured travel
17.5987 degrees, within the configured 0.45-degree tolerance, no drive fault,
and final controlword/target velocity zero. The parent `TANDEM_CONVEYOR.md`
contains the recorded result and limitations.

Read the latest delivery/build and live-test result in the parent
`TANDEM_CONVEYOR.md` before using the project. A successful earlier zero-torque
test does not establish normal CSV operation under current or load.

References: [Beckhoff CSV without NC](https://infosys.beckhoff.com/content/1033/el72x1-001x/1859316107.html).
