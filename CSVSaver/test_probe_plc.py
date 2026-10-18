"""Execute the new blocks extracted from MAIN, including its final physical output mapping."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/Untitled1/POUs/MAIN.TcPOU"
spec = importlib.util.spec_from_file_location("probe_st_simulator", ROOT / "tests/st_source_simulator.py")
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)


class ProbePlcTests(unittest.TestCase):
    def setUp(self):
        main = ET.parse(MAIN).getroot()
        source = main.find(".//ST").text
        code = "\n".join(source.split(f"// PROBE {name} BEGIN", 1)[1].split(f"// PROBE {name} END", 1)[0]
                         for name in ("OWNERSHIP", "COMMANDS", "OUTPUTS"))
        code = re.sub(r"\b(END_IF|END_FOR|END_CASE)\b(?!;)", r"\1;", code)
        names = set(re.findall(r"\b\w+\b", code))
        declarations = []
        for line in main.find(".//Declaration").text.splitlines():
            line = re.sub(r"\s+AT\s+%[IQ]\*", "", line)
            match = re.match(r"\s*(\w+)\s*:", line)
            if match and match[1] in names:
                declarations.append(line)
        pou = ET.Element("TcPlcObject")
        ET.SubElement(pou, "Declaration").text = "\n".join(declarations)
        ET.SubElement(pou, "ST").text = code
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.TcPOU"
            ET.ElementTree(pou).write(path, encoding="utf-8")
            self.sim = simulator.StSimulation(path)
        self.clock = 100
        self.seq = 0
        self.sim.values.update(Enable0=True, Enable1=True, ActValveMode0=3, ActValveMode1=3)
        for sensor in (2, 4, 6, 8):
            self.sim.values[f"LightBarrierStable{sensor}"] = True

    def scan(self, elapsed=1, **values):
        self.clock = (self.clock + elapsed) & 0xFFFFFFFF
        for sensor in (2, 4, 6, 8):
            values.setdefault(f"LightBarrier{sensor}FallingEdge", False)
        return self.sim.step(LightBarrierEventClockMs=self.clock, **values)

    def command(self, command, **values):
        self.seq += 1
        return self.scan(GuiProbeCalibrationCommand=command, GuiProbeCalibrationCommandSequence=self.seq, **values)

    def opened(self):
        return [i for i in range(1, 25) if self.sim.values[f"OpenValve{i}"]]

    def enter(self, mode=2, array=1, nozzle=1, **values):
        return self.command(1, GuiProbeCalibrationMode=mode, GuiProbeCalibrationArray=array,
                            GuiProbeCalibrationNozzle=nozzle, GuiProbeCalibrationTriggerSensor=2*array,
                            GuiProbeCalibrationHeartbeat=1, **values)

    def test_barrier_mode_inhibits_conveyor_and_all_valves(self):
        for i in range(1, 25):
            self.sim.values[f"OpenValve{i}"] = True
        values = self.enter(1, GuiConveyorEnabled=True, GuiArrayEnabled1=True,
                            GuiCalibrationMoveLeft=True, GuiBarrierCalibrationStart=True)
        self.assertTrue(values["ProbeCalibrationActive"])
        self.assertFalse(values["GuiConveyorEnabled"])
        self.assertFalse(values["GuiCalibrationMoveLeft"])
        self.assertTrue(values["ConveyorServoStopRequested"])
        self.assertEqual(self.opened(), [])
        self.scan(LightBarrier2FallingEdge=True)
        self.assertEqual(self.opened(), [])

    def test_each_array_nozzle_maps_to_exactly_one_physical_output(self):
        physical = [1, 2, 3, 4, 17, 18, 5, 6, 7, 8, 19, 20, 9, 10, 11, 12, 21, 22, 13, 14, 15, 16, 23, 24]
        for index, valve in enumerate(physical):
            array, nozzle = index // 6 + 1, index % 6 + 1
            self.enter(array=array, nozzle=nozzle)
            self.command(2)
            self.assertEqual(self.opened(), [])
            self.scan(**{f"LightBarrier{array*2}FallingEdge": True})
            self.assertEqual(self.opened(), [valve])
            self.assertEqual(self.sim.values["ProbeCalibrationOpenedAtMs"], self.clock)
            self.command(3)
            self.assertEqual(self.opened(), [])
            self.command(4)

    def test_valve_stays_open_on_return_without_retrigger(self):
        self.enter()
        self.command(2)
        self.scan(LightBarrier2FallingEdge=True)
        opened_at = self.sim.values["ProbeCalibrationOpenedAtMs"]
        self.scan(elapsed=500, GuiProbeCalibrationHeartbeat=2, LightBarrier2FallingEdge=True)
        self.assertEqual(self.opened(), [1])
        self.assertEqual(self.sim.values["ProbeCalibrationOpenedAtMs"], opened_at)
        self.command(3)
        self.scan(LightBarrier2FallingEdge=True)
        self.assertEqual(self.opened(), [])

    def test_heartbeat_timeout_closes_without_fallback_and_needs_new_entry(self):
        self.enter()
        self.command(2)
        self.scan(LightBarrier2FallingEdge=True)
        values = self.scan(elapsed=1000)
        self.assertEqual(values["ProbeCalibrationFaultCode"], 2)
        self.assertEqual(self.opened(), [])
        self.scan(GuiProbeCalibrationHeartbeat=2, LightBarrier2FallingEdge=True)
        self.assertEqual(self.opened(), [])
        self.assertFalse(self.sim.values["ProbeCalibrationActive"])

    def test_long_timeout_has_no_signed_16bit_overflow(self):
        self.enter(GuiProbeCalibrationMaxOpenMs=60000)
        self.command(2)
        self.scan(LightBarrier2FallingEdge=True)
        for heartbeat in range(2, 102):
            self.scan(elapsed=500, GuiProbeCalibrationHeartbeat=heartbeat)
        self.assertEqual(self.opened(), [1])
        for heartbeat in range(102, 123):
            self.scan(elapsed=500, GuiProbeCalibrationHeartbeat=heartbeat)
        self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 3)
        self.assertEqual(self.opened(), [])

    def test_stop_and_leave_close_on_same_scan(self):
        for stop in ("flag", "command"):
            self.command(4)
            self.enter()
            self.command(2)
            self.scan(LightBarrier2FallingEdge=True)
            if stop == "flag":
                self.scan(GuiProbeCalibrationStop=True)
                self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 5)
            else:
                self.command(4)
            self.assertEqual(self.opened(), [])

    def test_payload_is_latched_and_wrong_barrier_does_not_open(self):
        self.enter(nozzle=5)
        self.command(2)
        self.scan(LightBarrier4FallingEdge=True, GuiProbeCalibrationNozzle=1, GuiProbeCalibrationPressureMbar=5000)
        self.assertEqual(self.opened(), [])
        self.scan(LightBarrier2FallingEdge=True)
        self.assertEqual(self.opened(), [17])
        self.assertEqual(self.sim.values["PresValueV02"], 1000)

    def test_invalid_parameters_owner_vtem_and_blocked_beam_reject(self):
        self.enter(nozzle=7)
        self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 1)
        self.command(4)
        self.enter(GuiReorientationControlActive=True)
        self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 4)
        self.command(4, GuiReorientationControlActive=False)
        self.enter()
        self.scan(ErrorCode0=1)
        self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 6)
        self.command(4, ErrorCode0=0)
        self.enter()
        self.command(2, LightBarrierStable2=False)
        self.assertEqual(self.sim.values["ProbeCalibrationFaultCode"], 1)
        self.assertEqual(self.opened(), [])

    def test_clock_wrap_is_supported(self):
        self.clock = 0xFFFFFFFF - 10
        self.enter()
        self.command(2)
        self.scan(LightBarrier2FallingEdge=True)
        self.scan(elapsed=500, GuiProbeCalibrationHeartbeat=2)
        self.assertEqual(self.opened(), [1])


if __name__ == "__main__":
    unittest.main()
