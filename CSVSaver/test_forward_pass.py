"""Execute MAIN's actual pair gates and legacy trigger decisions without hardware."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
MAIN_PATH = ROOT / "TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/Untitled1/POUs/MAIN.TcPOU"
spec = importlib.util.spec_from_file_location(
    "forward_pass_st_simulator", ROOT / "tests/st_source_simulator.py"
)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)


class ForwardPassTests(unittest.TestCase):
    def setUp(self):
        main = ET.parse(MAIN_PATH).getroot()
        source = main.find(".//ST").text
        gates = source[source.index("// Alternating pair gate.") :]
        gates = gates[: gates.index("// Capture the exact gate BOOLs")]
        triggers = source[source.index("IF NOT GuiReorientationControlActive AND PairedForwardPass[1]") :]
        triggers = triggers[: triggers.index("// Measure the selected force-sensor peak")]
        implementation = gates + triggers
        # The interpreter requires statement terminators, also for block ends.
        implementation = re.sub(r"\b(END_IF|END_FOR)\b(?!;)", r"\1;", implementation)
        names = set(re.findall(r"\b\w+\b", implementation))
        declarations = []
        for line in main.find(".//Declaration").text.splitlines():
            match = re.match(r"\s*(\w+)\s*:", line)
            if match and match[1] in names:
                declarations.append(re.sub(r"ARRAY\s+\[", "ARRAY[", line))
        pou = ET.Element("TcPlcObject")
        ET.SubElement(pou, "Declaration").text = "\n".join(declarations)
        ET.SubElement(pou, "ST").text = implementation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forward_pass.TcPOU"
            ET.ElementTree(pou).write(path, encoding="utf-8")
            self.sim = simulator.StSimulation(path)
        for pair in range(1, 5):
            self.sim.values[f"Array{pair}Active"] = True
        self.clock = 0

    def scan(self, *barriers, elapsed=1, **inputs):
        self.clock += elapsed
        inputs["LightBarrierEventClockMs"] = self.clock
        for barrier in range(1, 9):
            inputs[f"LightBarrier{barrier}FallingEdge"] = barrier in barriers
        for pair in range(1, 5):
            inputs[f"Array{pair}TriggerPending"] = False
        return self.sim.step(**inputs)

    def assert_trigger(self, values, pair, expected):
        self.assertEqual(values["PairedForwardPass"][pair], expected)
        self.assertEqual(values[f"Array{pair}TriggerPending"], expected)

    def test_first_then_second_allows_one_shot_for_each_array(self):
        for pair in range(1, 5):
            self.assert_trigger(self.scan(2 * pair - 1), pair, False)
            self.assert_trigger(self.scan(2 * pair), pair, True)
            self.assert_trigger(self.scan(), pair, False)

    def test_second_alone_or_repeated_after_lock_timeout_never_fires(self):
        for pair in range(1, 5):
            second = 2 * pair
            self.assert_trigger(self.scan(second), pair, False)
            self.scan(second - 1)
            self.assert_trigger(self.scan(second), pair, True)
            for elapsed in (1, 499, 501, 2000):
                self.assert_trigger(self.scan(second, elapsed=elapsed), pair, False)
            self.scan(second - 1)
            self.assert_trigger(self.scan(second), pair, True)

    def test_reverse_order_needs_another_second_edge(self):
        self.assert_trigger(self.scan(2), 1, False)
        self.assert_trigger(self.scan(1), 1, False)
        self.assert_trigger(self.scan(2), 1, True)

    def test_simultaneous_edges_without_prior_first_do_not_arm_or_fire(self):
        for pair in range(1, 5):
            self.assert_trigger(self.scan(2 * pair - 1, 2 * pair), pair, False)
            self.assertFalse(self.sim.values["PairedForwardPassArmed"][pair])
            self.assert_trigger(self.scan(2 * pair, elapsed=501), pair, False)

    def test_simultaneous_old_second_and_next_first_preserve_next_pass(self):
        self.scan(1)
        self.assert_trigger(self.scan(1, 2), 1, True)
        self.assert_trigger(self.scan(2), 1, True)
        self.assert_trigger(self.scan(2, elapsed=501), 1, False)

    def test_first_edge_of_another_pair_cannot_grant_permission(self):
        self.scan(1, 5)
        values = self.scan(4, 8)
        self.assert_trigger(values, 2, False)
        self.assert_trigger(values, 4, False)
        values = self.scan(2, 6)
        self.assert_trigger(values, 1, True)
        self.assert_trigger(values, 3, True)

    def test_slow_forward_pass_is_valid_after_optical_lock_timeout(self):
        self.scan(1)
        self.assert_trigger(self.scan(2, elapsed=2000), 1, True)

    def test_inhibited_modes_and_disabled_array_discard_permission(self):
        for flag in ("GuiConveyorCalibrationMode", "GuiVelocityCheckMode", "ReorientationSafeLatch"):
            with self.subTest(flag=flag):
                self.scan(1, elapsed=501)
                self.scan(**{flag: True})
                self.assert_trigger(self.scan(2, **{flag: False}), 1, False)
        self.scan(1, elapsed=501)
        self.scan(Array1Active=False)
        self.assert_trigger(self.scan(2, Array1Active=True), 1, False)

    def test_batch_uses_same_permission_and_cannot_set_legacy_pending(self):
        self.scan(GuiReorientationControlActive=True, ReorientationState=20)
        self.scan(1)
        values = self.scan(2)
        self.assertTrue(values["PairedForwardPass"][1])
        self.assertFalse(values["Array1TriggerPending"])
        self.assertFalse(self.scan(2, elapsed=501)["PairedForwardPass"][1])
        self.scan(1)
        self.scan(ReorientationState=10)
        self.assertFalse(self.scan(2, ReorientationState=20)["PairedForwardPass"][1])


if __name__ == "__main__":
    unittest.main()
