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
and provides frame-by-frame LB-to-movement evaluation. The tab observes the
existing PLC cycle and never writes pressure or delay values.
