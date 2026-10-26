# PLC contract and state codes

| Direction | Symbol |
|---|---|
| GUI → PLC | `GuiReorientationControlActive : BOOL` |
| GUI → PLC | `GuiReorientationHeartbeat : UDINT` |
| GUI → PLC | `GuiReorientationStart/Abort/Reset : BOOL` |
| GUI → PLC | `GuiReorientationExpectedArrayMask : BYTE` |
| PLC → GUI | `ReorientationHeartbeatAck : UDINT` |
| PLC → GUI | `ReorientationHeartbeatAlive : BOOL` |
| PLC → GUI | `ReorientationState/FaultCode : UINT` |
| PLC → GUI | `ReorientationBusy/ExitSeen/ArraysIdle/Complete : BOOL` |
| PLC → GUI | `ReorientationExpected/TriggeredArrayMask : BYTE` |
| PLC → GUI | `ReorientationCycleCounter : UDINT` |

States are `0 legacy`, `10 armed`, `20 running`, `30 draining`, and `40 complete`.
Latched terminal states are:

- `90`: operator/GUI abort;
- `91`: heartbeat watchdog timeout;
- `92`: invalid queue commit or result acknowledgement;
- `93`: 128-record part queue exhausted;
- `94`: Beckhoff conveyor or VTEM drive fault;
- `95`: per-array job FIFO overflow;
- `96`: light barrier did not clear, queue underflow/sequence mismatch, or a part
  reached a barrier out of order.

The fixed-batch GUI enters state 20 with `GuiConveyorEnabled = FALSE`, commits and
waits for acknowledgement of the complete snapshot queue, then enables the conveyor
and asserts `Finish`. State 30 continues processing the accepted queue.

When the safe latch is set, conveyor and all array enables are cleared, pending
triggers and active delay/pulse states are aborted, and all 24 valve commands are
forced false. Merely clearing `ControlActive` does not clear this latch. A fresh
owner plus explicit reset is required, preventing automatic fallback into legacy
control after communication loss.

On a clean ADS disconnect or application shutdown, Reorientation first verifies
conveyor and array enables are FALSE, then requests the PLC's guarded
`GuiReorientationLegacyTakeover`. It verifies ownership, the safe latch, Abort,
conveyor/array enables and all valve commands are clear before closing ADS.
No motion or array enable is issued. Pressure Control also checks a remaining
safe latch during connection even when `GuiReorientationControlActive` is already
FALSE, allowing recovery after an interrupted shutdown. A healthy active batch
owner cannot be taken over through this path.

## Beckhoff prototype conveyor

The GUI requires `MAIN.ConveyorServoVelocityOnly = TRUE` in addition to the batch
contract above. Existing `StepperPos*` symbols are the tandem servo compatibility
interface. Required conveyor status reads fail explicitly if a symbol is missing.

Both manual and production startup use a stopped preparation operation before any
transport enable or batch ownership acquisition:

1. Clear conveyor/array enables and calibration/velocity/force-measurement modes;
   send `GuiCalibrationStop` as a pulse without requiring persistent TRUE readback.
2. Wait for `StepperPosBusy = FALSE` and both `ConveyorServoNTargetVelocity = 0`.
3. Write and read back `ConveyorServoCommissioned = TRUE`, `MotorCount = 2`,
   `SingleMotor = 1`, `Direction1/2 = -1`, `Motor2Ratio = 1.0`, and `MaxMotorRpm = 0`.
   These names all have the `MAIN.ConveyorServo` prefix. Preserve the live
   `ConveyorServoAccelerationRpmPerSec`; reject values outside 0.1–300 rpm/s.
4. Allow 250 ms for stationary feedback, pulse `GuiConveyorReset`, then verify
   stopped servo state 0, two commissioned motors, no drive fault/error and valid
   positive finite live belt calibration. Clear Reset after acknowledgement.

The ADS worker advances preparation using a 50 ms timer; each wait has an eight
second deadline. Cancellation invalidates the request and queued motion commands.
Old completions/errors cannot advance a later start. Failure, disconnect and
shutdown request stopped conveyor and disabled arrays. Preparation never writes
an enable TRUE. Automatic snapshot completion retains the PLC Finish/drain contract;
an operator Stop bypasses draining and disables outputs immediately.

`GuiConveyorMmPerFullStep`, `GuiConveyorCalibrationValid` and
`GuiCalibrationMarkerDistanceMm` are not restored from pressure profiles.
`GuiConveyorMaxSpeedMmPerSec` is bounded to 200 mm/s or a profile's lower maximum.
Execution commands require positive speed within that maximum. This avoids a
readback mismatch when the PLC enforces the prototype limit or calibration.
Legacy UR metadata is inert and sends no robot commands.

## Manual start-pose profiles

Manual configurations (schema 3) use the same PLC batch protocol. The GUI maps a
YOLO class to a configured pose and selects its one directly assigned pressure
profile. It freezes that profile's array/nozzle settings in the normal per-part
queue record. No roadmap, route generation or PLC program change is involved.
Target, unknown or unassigned start poses carry mask zero. The whole snapshot queue
must still be acknowledged before enabling transport. Manual profile assignments
and configuration edits are locked during startup, execution and draining.

## Continuous feed

The GUI production selector defaults to continuous feed and records that choice
in `run.json`. State 20 already accepts new immutable queue records while moving.
After fresh-frame preflight and the stopped Beckhoff preparation/ownership/start
handshake, the GUI enables the belt without asserting Finish. A part's leading
edge reaching the configurable yellow handoff line commits the current YOLO class
and selected profile once, without multi-frame confirmation. Track identity is used
only to prevent duplicate commits and to finish visible parts on Stop. Startup
detections wait until preparation and the transport-enable write complete.
Parts may already be visible before Start; the latest frame initializes live
tracking when transport is enabled, without requiring an empty feed zone.
The hidden input boundary excludes detections touching the right-hand entry region
before duplicate suppression, display or queue planning. The whole box must clear
the boundary to be accepted. Its position is saved as `input_line_percent` (90%
by default; 100% disables it), always right of handoff and locked during a run.
That same yellow line gates pose recognition;
there is no additional exit line. Its position is saved in `handoff_line_percent`.

Operator Stop enters GUI STOPPING and cancels preparation, pending commits and
heartbeat/vision waits. It writes `GuiReorientationAbort = TRUE`, conveyor/array
enables FALSE and Start/Finish FALSE, verifies those writes, then releases GUI
ownership. It does not wait for queue depth zero, LB8 or state 40. The existing PLC
abort latch disables pending array work. The run is recorded as stopped rather
than reporting pending parts as completed; delayed callbacks cannot restart it.
Stop is available from RUNNING, FINISHING and DRAINING. Automatic snapshot
completion still uses Finish/state 30 followed by state 40 and normal safe release.
A zero-depth queue during continuous feeding does not imply Finish.

Loss of an individual detection before handoff expires that track without aborting
or warning. A later detection at the handoff line may select its current class.
Continuous inference loss, invalid tracking order, queue capacity exhaustion, wrong or absent queue
acknowledgement, and actual PLC faults abort the run. Queue write dispatch and PLC
acknowledgement each have a ten-second deadline. The acknowledgement deadline
starts when the ADS worker reports that the commit write completed, rather than
when it was requested behind a status poll. Write completion does not count as PLC
acceptance; the matching sequence acknowledgement still releases the next record.
Status polling uses three cached ADS Sum Reads for conveyor, batch and optional
diagnostics. Missing required symbols or failed required items disconnect and stop;
missing optional diagnostics are discovered once per connection and retain their
existing display defaults.
PLC diagnostics 1–5 are informational in continuous feed: the PLC ignores duplicate,
unmatched or out-of-order sensor edges without GUI warning banners or conveyor stops.
Several parts may be queued and in transit before the first part reaches LB8.
Snapshot mode keeps its existing frozen-queue warning behavior. No PLC source
changes are required.
