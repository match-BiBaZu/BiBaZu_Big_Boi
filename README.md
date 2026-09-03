# BiBaZu_Big_Boi

The code to run the big BiBaZu test stand.

## Conveyor setup

1. Install or update the runtime environment from the repository root:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

2. Build and download the TwinCAT project after changes to `MAIN.TcPOU`.
3. Open a terminal in `CSVSaver`.
4. Start the conveyor and light-barrier setup tool:

```powershell
python ConveyorSetupGUI.py
```

The setup tool uses the stored conveyor calibration for all `mm` and `mm/s`
commands. Light-barrier distance measurements latch the EL7047 internal position
inside the PLC cycle and can be applied to sensor pairs 1-2, 3-4, and 5-6.

The fourth tab, `Pressure Delay`, uses the Baumer USB GenTL producer at
`C:\Program Files\Baumer Camera Explorer\bgapi2_usb.cti`. It records every frame
from camera VCXU-02C (serial `700005072151`) as JPEG plus timestamp metadata,
automatically stops after the selected light barrier and configurable post-roll,
and provides frame-by-frame LB-to-movement evaluation. After each completed
recording, the pre-trigger frames are used to learn the image noise and the
first persistent component motion is found with optical flow. That frame is
automatically marked and saved as the session result. Loading a recording opens
its saved first-movement frame directly. `Analyze Movement` reruns the automatic
analysis; the slider and `Mark First Movement` remain available to correct the
selection manually. Normal recording remains passive. For minimum-latency
tests, `Enable Fastest Response` saves the current
PLC values for the selected light barrier and its paired nozzle array, applies
the chosen test pressure, sets manual delay, offset and both response-delay
compensations to zero, and disables debounce for that barrier. Use
`Restore Previous Setup` after the test; shutdown also requests restoration.
`Apply Pressure` changes only the pressure of the paired array. `Pulse Duration`
and `Apply Pulse Duration` set its valve opening time independently from 1 to
500 ms; fastest-response mode does not alter that duration.

For the preferred hardware timing, connect the PNP receiver output to the
camera process connector (`Line0`: M8 pin 3/green, `GND IN1`: pin 4/yellow).
`Record` starts free-running capture before the part arrives. The first
`Line0RisingEdge` event marks time zero on the camera clock, and capture stops on
the first frame at or after that timestamp plus the configured post-roll. The
selected light barrier in the tab must match the sensor physically connected to
Line0. The camera status shows `Line0 ready`; `ADS fallback` means that the older
PLC/host clock estimate will be used instead.

The paired array is selected automatically: LB 1/2 -> array 1, LB 3/4 -> array
2, LB 5/6 -> array 3, and LB 7/8 -> array 4. The applied pressure and timing mode
are stored in `session.json` and included in `calibration_results.csv`, so runs
at different pressures can be compared later. New recording folders include
both values, for example `20260903_104310_LB4_3000mbar`.

`Compare Recordings…` in Frame Review accepts multiple session folders (or a
parent folder containing them) from one light barrier. It plots pressure in bar
against every marked LB-to-movement delay, shows the mean for each pressure,
adds ±1 standard-deviation error bars when a pressure has more than three
trials, and overlays a least-squares linear model with its equation and R².
Unmarked sessions or sessions without pressure metadata are skipped.
