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
inside the PLC cycle and can be applied to every adjacent pair from 1-2 through
7-8. All seven distances are stored in the PLC and are shown at the bottom of
the conveyor GUI with 2-3, 4-5, and 6-7 on the second row. The four existing
velocity channels continue to use pairs 1-2,
3-4, 5-6, and 7-8.

The Pressure Control light-barrier dialog shows those four velocity-pair
spacings and separate user-entered distances from LB2/4/6/8 to arrays 1/2/3/4.
Their machine defaults are `50/45/45/48 mm` for arrays 1 through 4.
Offset timing uses the matching velocity and subtracts the effective force
response delay, so an array offset of `0 mm` refers to the workpiece leading
edge being aligned with the nozzle. `Show Offset Equation` displays the exact
formula and current values. Force Delay Settings provides one response-delay
value per array, with `8.7 ms` as the default. These distances and force-response
values are global machine settings and are not overwritten by pressure profiles.

`USB High-Speed Camera` in the bottom bar of Nozzle Array Pressure Control opens
the USB-camera live view in a separate window. This view is fixed to light
barrier 4: `Record` arms the capture, the LB4 event starts image storage, and
the capture stops after the selected recording duration (500 ms by default).
The camera exposure is set to 1000 µs when the window connects and remains
editable through the Exposure field and `Apply Exposure`. The Output field and
`Browse` select where recording folders are stored. `Recording folder name`
optionally sets the name of the new session subfolder; an empty field keeps the
automatic name, and an existing name receives `_01`, `_02`, and so on instead
of being overwritten. The completed recording is loaded directly into Frame
Review, where the slider and Previous/Next buttons step through every saved
frame. While the review is active, incoming live preview images no longer
replace the selected recorded frame; arming a new recording switches the
display back to live video. Pressure, pulse-duration, fastest-response and
delay-analysis controls are intentionally omitted from this review-only window.

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
The stop-trigger light barrier can be changed between recordings. When fastest-
response mode is still active, selecting another barrier first restores the
saved PLC setup; the selector is disabled only while recording, analyzing, or
writing/restoring PLC settings.

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
the light barrier, pressure, and impulse duration, for example
`LB4_3000mbar_10ms_104310_03092026`.

`Compare Recordings…` in Frame Review accepts multiple session folders (or a
parent folder containing them) from one light barrier. Each session without a
saved first-movement result is analyzed and marked independently before the
plot is opened; existing results, including manual corrections, are preserved.
The result dialog has horizontally scrollable galleries for pressure on the x
axis (one plot per impulse duration) and impulse duration on the x axis (one
plot per pressure). Every plot shows its fixed condition, an equal-setpoint-
weighted suggested delay, group means, ±1 standard-deviation error bars for
more than three trials at an x value, and a least-squares linear model. Each
plot can be saved independently as SVG or PNG. Individual analysis failures or
sessions without pressure or impulse-duration metadata are skipped.
