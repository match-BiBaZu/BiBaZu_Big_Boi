# Conveyor control — Device 3 and Device 4

**Latest successful tests, 18:00–18:11 UTC: Device 4 completed 10, 30 and 50 rpm
with the velocity-only PLC.** The 10/30 rpm tests used 1 rpm/s ramps; the 50 rpm
test used the user-requested 3 rpm/s ramp. Each plateau lasted ten seconds.
At 10 rpm, mean target / drive actual / encoder-derived speed were
9.9999 / 9.9797 / 10.0037 rpm. At 30 rpm they were
30.0001 / 30.0098 / 29.9945 rpm. At 50 rpm they were
49.9799 / 50.0397 / 49.9843 rpm. The user confirmed uniform visible rotation
after the repeated 10 rpm test. Their physical change before that successful
repeat was not specified. All three runs used the original Kp=82 and Tn=15 ms;
no changed PI gain was tested. Motor 3 stayed disabled. The instantaneous drive
velocity remained noisy; at 30 rpm, speed derived over ~0.1-second encoder
windows ranged 27.29–33.52 rpm, and over ~1-second windows 29.82–30.28 rpm.
Evidence: `.tandem_validation/velocity_only_device4_20260915T175959Z.json`,
`velocity_only_device4_20260915T180231Z.json`, and
`velocity_only_device4_20260915T181036Z.json` (CSV files alongside).

Post-test diagnostics showed no new drive-history entry, feedback-invalid flag,
working-counter error or EtherCAT synchronization error. Device 3 stayed disabled
through every sample. At 18:11:51 UTC the normal runtime settings were restored:
MotorCount=2, SingleMotor=1, maximum 5 rpm and acceleration 5 rpm/s. Both drives
were verified disabled and `ConveyorServoCommissioned=FALSE`; both brake overrides
were FALSE. Restoration evidence:
`.tandem_validation/motor_count_selection_20260915T181151Z.json`.

The velocity-only program is loaded in the existing project. Nineteen ST
behavior tests and six I/O contract tests passed; TwinCAT compiled without
errors and verified 16 motor links, two pressure links and 68 CoE startup
entries. Deployment evidence: `.tandem_validation/velocity_only_load_20260915T174346Z`.

Earlier velocity-only starts at 5 rpm/s failed separately on both motors with
PLC speed-tracking fault 6. The first Device 4 trial at 1 rpm/s also stopped
before reaching its target. A later user-requested repeat succeeded; the
physical intervention before that repeat was not specified. Device 3 has not
yet passed the velocity-only test. Higher-speed and tandem/loaded readiness
remain unverified. Device 4's P gain was briefly set to 100 while disabled,
then restored to 82 before a motion test; no PI gain change has been validated.
Both drives retain Kp=82 and Tn=15 ms. Earlier details and snapshots are archived
in `.tandem_validation/velocity_only_notes_20260915_1805.md`.

**Velocity-only implementation:** the PLC sends one common ramped velocity,
scaled for each motor's direction, ratio and CoE velocity factor. There is no
position-error correction and no phase locking. `SyncErrorFullSteps` remains
available as a diagnostic; it does not steer the motors or cause a fault.
Speed monitoring uses a 100 ms low-pass of encoder-derived velocity. Its
allowance is 2 rpm + 15% of commanded speed + the filter's acceleration lag;
a sustained tracking or partner-speed mismatch for 250 ms stops the motors.
Fault 5 now means partner-speed mismatch; fault 6 includes speed tracking or
loss of operation-enabled. Communication, enable, stop and move timeouts remain.

Finite GUI/calibration jogs keep their existing commands and busy/done interface,
but finish a calculated speed profile and wait for standstill. They do not
correct a residual distance error or hold a target position. Actual encoder
travel remains the source for the legacy position and calibration readings.
The initial speed cap remains 5 rpm; larger test limits are temporary.

Use the existing project **`TwinCAT Projekt3 - Kopie/TwinCAT Projekt3.sln`**.
It has been updated in place. The additional `Device3_Conveyor` copy has been
removed. Earlier CSTCA projects, instructions and diagnostic evidence are
archived under workspace `.tandem_validation`.

## Hardware and mode

| Setting | Current value |
| --- | --- |
| Controller | `10.145.4.14.1.1` |
| Motor 1 terminal | Device 3 > Term 6 (EK1100) > Term 7 (EL7201-0010) |
| Motor 2 terminal | Device 4 > Term 9 (EK1100) > Term 10 (EL7201-0010) |
| Motor 1 master / slave / serial | `10.145.4.14.4.1` / `1002` / `00306149` — formerly Term 22 |
| Motor 2 master / slave / serial | `10.145.4.14.5.1` / `1002` / `00186867` — formerly Term 21 |
| Operating mode | **CSV, 9**, no NC axis |
| Physical motor count | **2**, sharing the conveyor trajectory |
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
| Stopped motor-count configuration | `MAIN.ConveyorServoMotorCount`: default 2; 1 selects a single motor |
| Single-motor selection | `MAIN.ConveyorServoSingleMotor`: 1 = Device 3, 2 = Device 4; stopped reset required |
| Calibration / finite jog | `MAIN.GuiConveyorCalibrationMode`, `GuiCalibrationMoveLeft/Right` |
| Jog distance / speed | `MAIN.GuiCalibrationJogSteps`, `GuiCalibrationJogSpeedFullStepsPerSec` |
| Shared acceleration | `MAIN.ConveyorServoAccelerationRpmPerSec` |
| Ready / busy / fault | Existing `MAIN.StepperPos*` status symbols |

`Stepper*` names are compatibility signals, not physical EL7047 commands.
`MAIN.ConveyorServo1*` refers to Device 3; `MAIN.ConveyorServo2*` refers to Device 4.
Both motors must be ready in tandem mode. A drive, communication, feedback or
sustained speed-mismatch fault stops the pair. Stop and timeout monitoring remains enabled.

`PressureControlGUI.py` now treats the conveyor as one tandem device. Selecting
Conveyor performs a stopped configuration/reset sequence, verifies the
velocity-only PLC marker, fixes MotorCount to 2, keeps both directions at +1 and
the motor ratio at 1.0, then enables both motors together. Clearing Conveyor
requests the shared stop immediately; a stop during preparation also cancels
the pending start. The nominal belt conversion is based on direct drive and a
50 mm roller: `pi * 50 / 200 = 0.785398 mm` per legacy virtual full step.
Pressure profiles can no longer restore the old stepper calibration.

`Calibrate Conveyor` includes **Target acceleration** in rpm/s and shows the
equivalent belt acceleration in mm/s². **Apply Tandem Settings** stops both
drives, applies the shared ramp, resets the frozen drive configuration and
checks that it returns without a PLC fault before calibration controls resume.

`TestStart`, `TestAbort`, test torque/commutation-angle outputs and `FB_Cstca*`
blocks are removed from the active project. `ConveyorDriveSettings` and
`ConveyorDriveSettings2` independently check actual mode 9, automatic brake control
and zero torque offset through CoE on their respective masters. The conveyor
cannot enable unless both sets of live checks are valid.

The motor-count setting feeds the FB's existing single/tandem support. A changed
count is rejected during operation and requires a stopped reset before adoption.
Both physical I/O mappings and the main program's drive health checks remain in
place. In count-1 operation Device 4's controlword and target remain zero. Restore
count 2 using a stopped reset before tandem operation; changing the ADS value
alone cannot silently remove a running partner from supervision.

## Mapping and startup

Output PDOs **1600, 1601** carry controlword and target velocity (6 bytes).
Input PDOs **1A00, 1A01, 1A02, 1A0C** occupy 12 bytes. Eight links connect
position, statusword, actual velocity, feedback validity, working-counter state,
EtherCAT state, controlword and target velocity for each motor: **16 links total**.

Device 1, Device 2 and non-conveyor mappings are preserved from the saved scan.
Normal application logic controls those I/O again; the earlier test program's
unconditional zeroing of every output is removed. Conveyor/nozzle-array enables
start FALSE and speed/pressure requests start zero. Loading does not itself
request movement.

The matching I/O configuration and PLC must be loaded together. Writing mode 9
alone cannot correct a torque-mode PDO layout. After loading, check
both `ConveyorDriveSettings.Valid` and `ConveyorDriveSettings2.Valid`, Devices 3/4
OP, and zero motor commands before using
the GUI. [Beckhoff CSV without NC](https://infosys.beckhoff.com/content/1033/el72x1-001x/1859316107.html)

## Device 4 integration, 2026-09-15

The existing project was updated in place from the user's Device 4 scan. Readback
identified motor `00186867`, CSV mode 9, zero controlword/target, automatic brake
control and approximately 23.68 V. Its 34 persisted parameters matched the earlier
commissioning record exactly; no motor tuning values were changed.

TwinCAT compiled with zero errors after establishing Device 4's task association.
All **16 motor links**, **68 startup entries**, and both pressure-input mappings
were verified. All **58 non-conveyor links** and the Device 1/2 terminal topology
were preserved. The actual-ST behavior suite passed 16 tests; the updated I/O and
GUI contract suite passed 6 tests, including independent CoE and fault monitoring.

Build evidence: workspace `.tandem_validation/device4_repaired_build_logs/`.

The PLC and matching I/O were loaded and activated. At 13:48:15 UTC both motors
were OP, stationary, with valid feedback and CoE checks, zero output commands,
and no drive/PLC faults. All 68 startup values and both PDO assignments were
verified against live readback.

At 13:48:36 UTC one finite GUI calibration jog requested **18 degrees at about
2 rpm** on both motors. The monitor aborted on encoder separation exceeding
6000 counts (2.06 degrees); the PLC subsequently latched synchronization fault 5.
Both motors' controlwords/targets returned to zero, automatic brake control was
retained, and both encoders became stationary. Temporary GUI jog settings were
restored. No repeat enable or reset was attempted.

Final encoder differences corresponded to **2.455 degrees for Device 3** and
**33.589 degrees for Device 4**. These are reported positions, **not verified
physical travel**: the user initially reported that the rollers were uncoupled
and motor 2 stayed still, then clarified that the coupling was loose and motion
could not actually be seen. A stationary roller therefore does not prove that
the motor shaft or encoder stayed still. The requested finite move did not
complete successfully.

Read-only checks at 13:50:32 UTC found the Device 4 CoE position and mapped PLC
position both **4044663144**. Thus the discrepancy is also present in the drive's
reported data, rather than being confined to GUI display or PLC mapping. Both
drives had statusword 96, valid feedback flags, approximately 23.7 V DC, empty
diagnostic histories, and no missed synchronization events, exceeded cycles,
short shifts or EtherCAT sync-error flags. Those observations do not establish
correct physical feedback or commutation. Confirm which coupling was loose and
that it is secured before considering a repeat test. The failed trajectory alone
does not establish an encoder defect.

Evidence in workspace `.tandem_validation`:
`device4_load_20260915T134614Z/after.json`,
`device4_gui_jog_20260915T134836Z.json`, and
`device4_after_jog_diagnostics.json`. Diagnostics remain outside the production
project; the existing solution is the sole maintained conveyor project.

### Reduced test after securing the coupling

The user confirmed the mechanical shaft/roller coupling was secured. At
13:54:45 UTC a single reduced GUI jog requested **3.6 degrees at about 1 rpm**,
after resetting fault 5 with both stages disabled. Direct CoE reads confirmed
CSV mode 9 and the velocity commands arriving at each drive. Both read back
10156 during the same constant-command portion and later 5592 during trajectory
correction. This confirms command delivery, not correct physical response.

The monitor stopped the test when the reported positions separated by more than
1 degree. At that point, relative encoder positions were approximately -0.003
degrees (Device 3) and +1.203 degrees (Device 4). Normal stopping did not disable
the drives within the test utility's three-second cleanup window; the PLC's
longer stop watchdog subsequently latched fault 7. The commissioning inhibit
was then asserted, both stages were verified disabled, and the original GUI jog
settings were restored. The test utility now asserts that inhibit immediately
on an aborted diagnostic, without relying on ordinary trajectory settling.

Final read-only verification at **13:57:17 UTC** found both controlwords and
targets zero, both statuswords 96, GUI enable/calibration off, and the current
runtime commissioning flag FALSE. Over twelve samples, raw encoder spans were
8 and 12 counts, within the existing 262-count standstill window. The PLC's
`EncoderStationary` flag itself is inactive while commissioning is inhibited.
Paired operation has not been successfully commissioned; no encoder defect is
proven by these tests. Further motion is paused at the user's request.

Evidence: `.tandem_validation/device4_gui_jog_20260915T135445Z.json`,
`device4_final_disabled.json`, and `device4_final_verified.json`.

### Resumed readiness test after reassembly, 16:42 UTC

The user confirmed everything was connected and the rollers remained uncoupled
because no belt was fitted. Both motors passed the idle checks, including their
identities, live CoE settings, all 68 startup values and both PDO assignments.

One paired finite GUI jog requested **3.6 degrees at about 1 rpm**. The test
aborted after approximately 2.1 seconds of the jog when relative encoder travel
separated by over 1 degree. Final reported travel was **+0.965 degrees on Device
3** and **-0.067 degrees on Device 4**. Neither completed the requested move.
Direct CoE reads confirmed mode 9 and the commanded velocities at both drives;
the peak commanded speed, including trajectory correction, was 2.27 rpm.
The user heard both brake clicks and observed little or no physical movement.
Clicks do not establish full mechanical brake release, and no specific wiring,
encoder or tuning defect has been established.

On abort the diagnostic immediately asserted the commissioning inhibit. Both
controlwords and targets were verified zero and both statuswords returned to 96.
GUI settings were restored, and a separate 12-sample read-only check at 16:43:52
UTC confirmed raw encoder spans of just 8 and 12 counts. No new PLC fault was
latched because the external monitor cancelled before the PLC fault thresholds.
The encoder scaling remains 1048576 mapped counts per revolution; the recorded
velocity factor is 268435 per revolution/second, consistent with the documented
[CSV interface](https://infosys.beckhoff.com/content/1033/el72x1-001x/1859316107.html).

Readiness is **not established**. Next diagnosis must distinguish incomplete
brake release/mechanical resistance from motor-phase, feedback or drive-control
problems; repeating the same paired jog has not resolved the cause.

Evidence: workspace `.tandem_validation/device4_resumed_settings.json`,
`device4_gui_jog_20260915T164239Z.json`, and
`device4_resumed_final_verified_20260915.json`.

### Device 3 alone, motor removed from roller, 17:02 UTC

The user requested the same command on Device 3 only and confirmed that the motor
was separated from the roller and free to rotate. The existing project was
updated in place to expose `ConveyorServoMotorCount`, default 2. No extra project
was created. A fresh offline build passed with zero errors, all 16 mappings and
68 startup entries verified. The 17 general behavior tests, five single-motor
checks and six I/O contract tests passed, including stopped selection/restoration
and prevention of a live motor-count change.

After loading and verifying idle outputs, a stopped reset selected count 1. At
**17:02:14 UTC**, the GUI calibration interface requested **3.6 degrees at about
1 rpm**. The move completed normally in **1.14 seconds**, with **10496 counts /
3.603515625 degrees** reported travel. Peak velocity command including trajectory
correction was approximately **1.57 rpm**, within the unchanged 5 rpm limit.
No drive, feedback, communication or adapter fault was observed.

Across all 131 recorded samples, Device 4 had controlword/target zero and no
operation-enabled status; its encoder span was only 20 counts. Cleanup verified
Device 3 disabled with zero target and statusword 96, and restored the temporary
GUI jog settings. At 17:03:11 UTC the two-motor setting was restored through a
stopped reset, followed by the commissioning inhibit. Both final controlwords and
targets were zero, both statuswords 96, and the PLC fault code was zero.

This establishes one successful small unloaded move on Device 3. Removing the
roller and selecting single-motor operation changed two conditions; this result
does not by itself isolate the earlier failure to a specific mechanical or
tandem-control cause, or establish Device 4/loaded readiness.

Evidence: workspace `.tandem_validation/motor_count_verified_build/`,
`motor_count_load_20260915T165941Z/`,
`device3_isolated_jog_20260915T170214Z.json`, and
`motor_count_selection_20260915T170311Z.json`.

## Earlier single-motor verification and hardware history

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

## Earlier single-motor movement, 2026-09-14

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
