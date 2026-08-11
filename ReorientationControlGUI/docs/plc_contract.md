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
- `94`: EL7047 or VTEM drive fault;
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
