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

States are `0 legacy`, `10 armed`, `20 running to LB6`, `30 draining`, and
`40 complete`. Latched terminal states are `90 manual abort`, `91 heartbeat
timeout`, `92 cycle timeout`, and `93 drain/missing-trigger timeout`.
`94` reports an EL7047/VTEM drive fault during the active cycle.

When the safe latch is set, conveyor and all array enables are cleared, pending
triggers and active delay/pulse states are aborted, and all 24 valve commands are
forced false. Merely clearing `ControlActive` does not clear this latch. A fresh
owner plus explicit reset is required, preventing automatic fallback into legacy
control after communication loss.
