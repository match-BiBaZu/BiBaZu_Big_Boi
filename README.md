# BiBaZu_Big_Boi

The code to run the big BiBaZu test stand.

## Conveyor setup

1. Build and download the TwinCAT project after changes to `MAIN.TcPOU`.
2. Open a terminal in `CSVSaver`.
3. Start the conveyor and light-barrier setup tool:

```powershell
python ConveyorSetupGUI.py
```

The setup tool uses the stored conveyor calibration for all `mm` and `mm/s`
commands. Light-barrier distance measurements latch the EL7047 internal position
inside the PLC cycle and can be applied to sensor pairs 1-2, 3-4, and 5-6.
