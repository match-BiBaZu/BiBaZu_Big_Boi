"""Execute MAIN's actual inlet-pressure measurement ST without hardware."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
MAIN_PATH = ROOT / "TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/Untitled1/POUs/MAIN.TcPOU"
VALVES = ((1, 2, 3, 4, 17, 18), (5, 6, 7, 8, 19, 20),
          (9, 10, 11, 12, 21, 22), (13, 14, 15, 16, 23, 24))
FEEDBACK = ("ActPresValueV02", "ActPresValueV04", "ActPresValueV12", "ActPresValueV14")
spec = importlib.util.spec_from_file_location(
    "impulse_pressure_st_simulator", ROOT / "tests/st_source_simulator.py"
)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)


class ImpulsePressureTests(unittest.TestCase):
    def setUp(self):
        main = ET.parse(MAIN_PATH).getroot()
        self.source = main.find(".//ST").text
        implementation = self.source[self.source.index("// Impulse inlet-pressure measurement.") :]
        implementation = implementation[: implementation.index("LastLightBarrier1 :=")]
        implementation = re.sub(r"\b(END_IF|END_FOR)\b(?!;)", r"\1;", implementation)
        names = set(re.findall(r"\b\w+\b", implementation))
        declarations = []
        for line in main.find(".//Declaration").text.splitlines():
            match = re.match(r"\s*(\w+)\s*(?:AT\s+%\w\*\s*)?:", line)
            if match and match[1] in names:
                line = re.sub(r"\s+AT\s+%\w\*", "", line)
                declarations.append(re.sub(r"ARRAY\s+\[", "ARRAY[", line))
        pou = ET.Element("TcPlcObject")
        ET.SubElement(pou, "Declaration").text = "\n".join(declarations)
        ET.SubElement(pou, "ST").text = implementation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "impulse_pressure.TcPOU"
            ET.ElementTree(pou).write(path, encoding="utf-8")
            self.sim = simulator.StSimulation(path)
        self.sim.step(Enable0=True, Enable1=True, ActValveMode0=3, ActValveMode1=3)

    def scan(self, opened=(), pressures=(4000, 3000, 2000, 1000), completed=(), **inputs):
        for array in range(1, 5):
            self.sim.values["ArrayImpulseCompleted"][array] = array in completed
        inputs.update({f"OpenValve{valve}": valve in opened for valve in range(1, 25)})
        inputs.update(dict(zip(FEEDBACK, pressures)))
        return self.sim.step(**inputs)

    def result(self, array):
        values = self.sim.values
        return (values[f"ArrayImpulseAvgPressureMbar{array}"],
                values[f"ArrayImpulseSampleCount{array}"],
                values[f"ArrayImpulseResultCounter{array}"],
                values[f"ArrayImpulseValid{array}"])

    def test_known_mean_excludes_delay_and_recovery_and_latches(self):
        self.scan(pressures=(6000,) * 4)
        self.scan(opened=VALVES[0], pressures=(4200,) * 4)
        self.scan(opened=VALVES[0], pressures=(4000,) * 4)
        self.scan(opened=VALVES[0], pressures=(3800,) * 4)
        self.assertEqual(self.result(1), (0, 0, 0, False))
        self.scan(completed=(1,), pressures=(6000,) * 4)
        self.assertEqual(self.result(1), (4000, 3, 1, True))
        for _ in range(10):
            self.scan(pressures=(6000,) * 4)
        self.assertEqual(self.result(1), (4000, 3, 1, True))

    def test_every_nonempty_nozzle_combination_counts_once_per_array(self):
        for array, valves in enumerate(VALVES, start=1):
            for mask in range(1, 64):
                opened = [valve for bit, valve in enumerate(valves) if mask & (1 << bit)]
                self.scan(opened=opened)
                self.scan(completed=(array,))
                average, samples, counter, valid = self.result(array)
                self.assertEqual((average, samples, counter, valid),
                                 ((4000, 3000, 2000, 1000)[array - 1], 1, mask, True))

    def test_overlapping_arrays_complete_independently(self):
        self.scan(opened=VALVES[0], pressures=(4200, 3000, 2000, 1000))
        self.scan(opened=VALVES[0] + VALVES[2], pressures=(4000, 3000, 2200, 1000))
        self.scan(opened=VALVES[2], completed=(1,), pressures=(6000, 3000, 2000, 1000))
        self.assertEqual(self.result(1), (4100, 2, 1, True))
        self.assertEqual(self.result(3), (0, 0, 0, False))
        self.scan(completed=(3,))
        self.assertEqual(self.result(3), (2100, 2, 1, True))
        self.assertEqual(self.result(2), (0, 0, 0, False))

    def test_interrupted_impulse_is_invalid_and_next_impulse_recovers(self):
        self.scan(opened=VALVES[1])
        self.scan()
        self.assertEqual(self.result(2), (0, 1, 1, False))
        self.scan(opened=VALVES[1])
        self.scan(completed=(2,))
        self.assertEqual(self.result(2), (3000, 1, 2, True))

    def test_invalid_feedback_anywhere_invalidates_entire_impulse(self):
        for bad in ({"Enable0": False}, {"ActValveMode0": 2}, {"ErrorCode0": 5}):
            with self.subTest(bad=bad):
                self.scan(opened=VALVES[0], **bad)
                self.scan(opened=VALVES[0], Enable0=True, ActValveMode0=3, ErrorCode0=0)
                self.scan(completed=(1,))
                self.assertFalse(self.result(1)[3])
        self.scan(opened=VALVES[2])
        self.scan(completed=(3,), ErrorCode1=5)
        self.assertFalse(self.result(3)[3])

    def test_safety_and_setup_overrides_cannot_publish_valid_result(self):
        for flag in ("ReorientationSafeLatch", "GuiConveyorCalibrationMode", "GuiVelocityCheckMode"):
            with self.subTest(flag=flag):
                self.scan(opened=VALVES[3], **{flag: False})
                self.scan(completed=(4,), **{flag: True})
                self.assertFalse(self.result(4)[3])
                self.scan(**{flag: False})

    def test_closed_valves_or_delay_without_a_pulse_do_not_create_results(self):
        for _ in range(10):
            self.scan(completed=(1, 2, 3, 4))
        for array in range(1, 5):
            self.assertEqual(self.result(array), (0, 0, 0, False))

    def test_maximum_pulse_duration_and_repeat_do_not_mix_samples(self):
        for _ in range(500):
            self.scan(opened=VALVES[0], pressures=(4200,) * 4)
        self.scan(completed=(1,))
        self.assertEqual(self.result(1), (4200, 500, 1, True))
        self.scan(opened=VALVES[0], pressures=(3500,) * 4)
        self.assertEqual(self.result(1), (4200, 500, 1, True))
        self.scan(completed=(1,))
        self.assertEqual(self.result(1), (3500, 1, 2, True))

    def test_completion_and_final_feedback_integration(self):
        marker = self.source.index("// Impulse inlet-pressure measurement.")
        self.assertGreater(marker, self.source.index("FBValveControlV0("))
        self.assertGreater(marker, self.source.index("FBValveControlV1("))
        self.assertGreater(marker, self.source.index("// Setup movement must never trigger"))
        for array, counter in enumerate(("Counter", "Counter2", "Counter3", "Counter4"), start=1):
            block = self.source[self.source.index(f"IF {counter} > LatchedNozzleEndCycles{array} THEN") :]
            self.assertIn(f"ArrayImpulseCompleted[{array}] := TRUE;", block.split("END_IF")[0])
        self.assertEqual(self.source.count("ArrayImpulseCompleted[ArrayImpulseIndex] := FALSE;"), 1)


if __name__ == "__main__":
    unittest.main()
