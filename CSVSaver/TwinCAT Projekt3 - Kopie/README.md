# Two-motor conveyor with existing GUI

The existing PLC project now uses pure velocity commands without position
correction. Direction, ratio, speed ramp and GUI commands remain supported;
encoder positions supply travel reporting and stopped-reset qualification only. Finite
GUI jogs run a calculated speed profile without endpoint correction.

Device 4 passed ten-second plateaus at 10, 30 and 50 rpm with unchanged PI gains.
The first two used 1 rpm/s ramps; 50 rpm used 3 rpm/s. Encoder-derived means were
10.0037, 29.9945 and 49.9843 rpm. Device 3 remained disabled. Device 3's velocity-only start had previously failed with
a speed-tracking stop. No PI tuning or tandem/loaded readiness is established.

After the final test both drives were disabled and inhibited, both brake overrides
were FALSE, and the normal MotorCount=2, 5 rpm and 5 rpm/s runtime settings were
restored. See the parent `TANDEM_CONVEYOR.md` for current state and evidence.

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
A stopped reset can select MotorCount=1 and ConveyorServoSingleMotor=1 for
Device 3 alone, or ConveyorServoSingleMotor=2 for Device 4 alone. The inactive
motor always receives zero controlword and target velocity.
The main program still checks both physical drives' health. All active-motor
fault, feedback, stop and timeout monitoring remains enabled. Changing motor
count in the FB requires stopped reconfiguration; no partner feedback is fabricated.

## Initial limits and mapping

- The source disables the former **5 rpm (13.09 mm/s)** commissioning cap:
  `ConveyorServoMaxMotorRpm=0`. Acceleration remains **5 rpm/s**. GUI maximum
  speed and command representation bounds remain active. Load this updated PLC
  before using the updated GUI, which writes zero during stopped configuration.
- Direction -1 for both motors; 1048576 mapped encoder counts/revolution; 268435 velocity units
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
