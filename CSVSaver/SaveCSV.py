import pyads
import csv
import os
import time
from datetime import datetime

AMS_NET_ID = "10.145.4.14.1.1"   # change this
PLC_IP = "192.168.10.23"          # change this

CSV_FILE = "pressure_log.csv"
POLL_INTERVAL_SECONDS = 0.02
CSV_HEADER = [
    "timestamp",
    "AvgPressureN1",
    "AvgPressureN2",
    "EstimatedVelocityArray1",
    "EstimatedVelocityArray2",
]

plc = pyads.Connection(AMS_NET_ID, pyads.PORT_TC3PLC1, PLC_IP)
plc.open()

last_shot_counter = plc.read_by_name("MAIN.ShotCounter", pyads.PLCTYPE_UDINT)
write_header = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0

with open(CSV_FILE, "a", newline="") as f:
    writer = csv.writer(f)

    if write_header:
        writer.writerow(CSV_HEADER)

    while True:
        shot_counter = plc.read_by_name("MAIN.ShotCounter", pyads.PLCTYPE_UDINT)

        if shot_counter != last_shot_counter:
            avg_n1 = plc.read_by_name("MAIN.AvgPressureN1", pyads.PLCTYPE_REAL)
            avg_n2 = plc.read_by_name("MAIN.AvgPressureN2", pyads.PLCTYPE_REAL)
            velocity_1 = plc.read_by_name("MAIN.EstimatedVelocityMmPerSec1", pyads.PLCTYPE_REAL)
            velocity_2 = plc.read_by_name("MAIN.EstimatedVelocityMmPerSec2", pyads.PLCTYPE_REAL)

            writer.writerow([
                datetime.now().isoformat(timespec="milliseconds"),
                avg_n1,
                avg_n2,
                velocity_1,
                velocity_2
            ])
            f.flush()
            last_shot_counter = shot_counter

        time.sleep(POLL_INTERVAL_SECONDS)
