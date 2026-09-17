"""Offline checks of the real PLC declarations and EtherCAT links (no ADS access)."""
from pathlib import Path
import math
import re
import unittest
import xml.etree.ElementTree as ET


PROJECT = Path(__file__).parent / "TwinCAT Projekt3 - Kopie" / "TwinCAT Projekt3"


class TandemIoContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = ET.parse(PROJECT / "TwinCAT Projekt3.tsproj").getroot()
        cls.main = ET.parse(PROJECT / "Untitled1/POUs/MAIN.TcPOU").getroot()
        cls.declaration = cls.main.findtext("./POU/Declaration")
        cls.fb = ET.parse(PROJECT / "Untitled1/POUs/FB_TandemConveyor.TcPOU").getroot()

    def test_legacy_ads_symbols_keep_types_without_hardware_links(self):
        expected = {
            "StepperPosBusy": "BOOL", "StepperPosInTarget": "BOOL",
            "StepperPosWarning": "BOOL", "StepperPosError": "BOOL",
            "StepperPosReadyToExecute": "BOOL", "StepperInternalPosition": "UDINT",
            "StepperStmStatus": "WORD", "StepperWcState": "BOOL",
            "StepperInfoDataState": "UINT", "GuiConveyorEnabled": "BOOL",
            "GuiConveyorReverse": "BOOL", "GuiConveyorSpeedMmPerSec": "REAL",
            "GuiConveyorMmPerFullStep": "REAL", "GuiConveyorCalibrationValid": "BOOL",
            "GuiCalibrationJogSteps": "UDINT", "CalibrationStatusCode": "UINT",
        }
        for name, datatype in expected.items():
            with self.subTest(symbol=name):
                self.assertRegex(self.declaration, rf"\b{name}\s*:\s*{datatype}\b")
        for link in self.project.findall(".//Mappings//Link"):
            self.assertNotIn("MAIN.Stepper", link.get("VarA", ""))
            self.assertNotIn("MAIN.Stepper", link.get("VarB", ""))

    def test_each_servo_has_all_links_to_the_correct_active_pdo(self):
        specs = {
            "Position": ("Inputs", "UDINT", "#x6000", "#x11", "3"),
            "Statusword": ("Inputs", "UINT", "#x6010", "#x01", "3"),
            "ActualVelocity": ("Inputs", "DINT", "#x6010", "#x07", "3"),
            "FeedbackInvalid": ("Inputs", "BOOL", "#x6000", "#x0e", "3"),
            "WcState": ("Inputs", "BOOL", None, None, None),
            "InfoState": ("Inputs", "UINT", None, None, None),
            "Controlword": ("Outputs", "UINT", "#x7010", "#x01", "2"),
            "TargetVelocity": ("Outputs", "DINT", "#x7010", "#x06", "2"),
        }
        for axis, terminal, device, coupler in ((1, 7, 3, 6), (2, 10, 4, 9)):
            name = f"Term {terminal} (EL7201-0010)"
            box = next(b for b in self.project.findall(".//Box") if b.findtext("Name") == name)
            ethercat = box.find("EtherCAT")
            owner = next(o for o in self.project.findall(".//Mappings/OwnerA/OwnerB")
                         if o.get("Name", "").endswith("^" + name))
            self.assertEqual(owner.get("Name"),
                f"TIID^Device {device} (EtherCAT)^Term {coupler} (EK1100)^Term {terminal} (EL7201-0010)")
            links = {l.get("VarA"): l.get("VarB") for l in owner.findall("Link")}
            self.assertEqual(len(links), len(specs))
            for suffix, (area, datatype, index, sub, sm) in specs.items():
                symbol = f"ConveyorServo{axis}{suffix}"
                with self.subTest(symbol=symbol):
                    endpoint = links[f"PlcTask {area}^MAIN.{symbol}"]
                    io = "I" if area == "Inputs" else "Q"
                    self.assertRegex(self.declaration, rf"\b{symbol}\s+AT %{io}\*\s*:\s*{datatype}\b")
                    if index:
                        # XAE expands ESI's double-underscore members into nested nodes.
                        pdo_name, *entry_parts = endpoint.split("^")
                        entry_name = "__".join(entry_parts)
                        pdo = next(p for p in ethercat.findall("Pdo") if p.get("Name") == pdo_name)
                        self.assertEqual(pdo.get("SyncMan"), sm)
                        entry = next(e for e in pdo.findall("Entry") if e.get("Name") == entry_name)
                        self.assertEqual(entry.get("Index").lower(), index)
                        self.assertEqual(entry.get("Sub").lower(), sub)
                        self.assertEqual(entry.findtext("Type"), "BIT" if datatype == "BOOL" else datatype)
                    else:
                        self.assertEqual(endpoint, "WcState^WcState" if suffix == "WcState" else "InfoData^State")
            active_inputs = {p.get("Index").lower() for p in ethercat.findall("Pdo") if p.get("SyncMan") == "3"}
            self.assertEqual(active_inputs, {"#x1a00", "#x1a01", "#x1a02", "#x1a0c"})
            active_outputs = {p.get("Index").lower() for p in ethercat.findall("Pdo") if p.get("SyncMan") == "2"}
            self.assertEqual(active_outputs, {"#x1600", "#x1601"})
            syncs = ethercat.findall("SyncMan")
            self.assertEqual(int.from_bytes(bytes.fromhex(syncs[3].text)[2:4], "little"), 12)
            self.assertEqual(int.from_bytes(bytes.fromhex(syncs[2].text)[2:4], "little"), 6)

    def test_fb_call_supplies_every_input_and_uses_the_one_ms_task(self):
        declaration = self.fb.findtext("./POU/Declaration")
        inputs = declaration.split("VAR_INPUT", 1)[1].split("END_VAR", 1)[0]
        names = re.findall(r"^\s*(\w+)\s*:", inputs, re.MULTILINE)
        body = self.main.findtext("./POU/Implementation/ST")
        call = body.split("TandemConveyor(", 1)[1].split(");", 1)[0]
        for name in names:
            self.assertRegex(call, rf"\b{name}\s*:=")
        task = ET.parse(PROJECT / "Untitled1/PlcTask.TcTTO").getroot()
        self.assertEqual(task.findtext(".//CycleTime"), "1000")
        self.assertIn("CycleSeconds := 0.001", call)
        self.assertIn("MotorCount := ConveyorServoMotorCount", call)
        self.assertRegex(self.declaration, r"ConveyorServoMotorCount\s*:\s*UINT\s*:=\s*2;")
        self.assertIn("Commissioned := ConveyorServoCommissioned AND ConveyorDriveSettings.Valid AND ConveyorDriveSettings2.Valid", call)
        self.assertNotIn("TestStart", self.declaration)
        for link in self.project.findall(".//Mappings//Link"):
            self.assertNotIn("MAIN.Test", link.get("VarA", ""))

    def test_plc_velocity_command_has_no_speed_feedback_or_partner_comparison(self):
        declaration = self.fb.findtext("./POU/Declaration")
        body = self.fb.findtext("./POU/Implementation/ST")

        for removed_name in (
            "SpeedToleranceRpm",
            "SpeedTolerancePercent",
            "FollowingTimer",
            "SyncTimer",
            "MeasuredVelocity",
            "SpeedErrorRpm",
        ):
            self.assertNotIn(removed_name, declaration)
            self.assertNotIn(removed_name, body)
        self.assertIn(
            "DriveCommand[I] := LIMIT(-MaxCommonSpeed, TrajectoryVelocity, MaxCommonSpeed);",
            body,
        )

    def test_each_drive_has_independent_coe_and_fault_monitoring(self):
        body = self.main.findtext("./POU/Implementation/ST")
        for axis, instance, netid in ((1, "ConveyorDriveSettings", "10.145.4.14.4.1"),
                                      (2, "ConveyorDriveSettings2", "10.145.4.14.5.1")):
            self.assertRegex(body, rf"{instance}\(MasterNetId := '{re.escape(netid)}', SlaveAddress := 1002,\s*Online := \(ConveyorServo{axis}InfoState = 8\) AND NOT ConveyorServo{axis}WcState\)")
        fault_check = body.split("StepperPosError := TandemConveyor.Error", 1)[1].split(";", 1)[0]
        for axis in (1, 2):
            self.assertIn(f"ConveyorServo{axis}Statusword", fault_check)
            self.assertIn(f"ConveyorServo{axis}FeedbackInvalid", fault_check)
        self.assertIn("StepperWcState := ConveyorServo1WcState OR ConveyorServo2WcState;", body)
        self.assertIn("IF ConveyorServo2InfoState <> UINT#8 THEN", body)

    def test_commissioned_machine_starts_idle_with_50_mm_roller_calibration(self):
        def initial(name):
            match = re.search(rf"\b{name}\s*:\s*\w+\s*:=\s*([^;]+);", self.declaration)
            self.assertIsNotNone(match, name)
            return match.group(1).strip()

        self.assertEqual(initial("ConveyorServoCommissioned"), "TRUE")
        for name in ("GuiConveyorEnabled", "GuiConveyorCalibrationMode",
                     "GuiArrayEnabled1", "GuiArrayEnabled2", "GuiArrayEnabled3", "GuiArrayEnabled4"):
            self.assertEqual(initial(name), "FALSE", name)
        for number in range(1, 5):
            self.assertEqual(float(initial(f"GuiPressureMbar{number}")), 0.0)
        self.assertEqual(initial("ConveyorServoDirection1"), "-1")
        self.assertEqual(initial("ConveyorServoDirection2"), "-1")
        self.assertEqual(float(initial("ConveyorServoMotor2Ratio")), 1.0)
        self.assertEqual(float(initial("ConveyorServoMaxMotorRpm")), 0.0)
        self.assertAlmostEqual(float(initial("ConveyorServoCalibratedMmPerFullStep")),
                               math.pi * 50.0 / 200.0, places=8)
        for suffix in ("", "2"):
            self.assertEqual(float(initial("ConveyorServoFeedbackCountsPerRev" + suffix)), 1048576.0)
            self.assertEqual(float(initial("ConveyorServoVelocityUnitsPerRevPerSec" + suffix)), 268435.0)

    def test_pressure_inputs_link_measurement_values(self):
        links = {link.get("VarA"): link.get("VarB")
                 for link in self.project.findall(".//Mappings//Link")}
        self.assertEqual(links["PlcTask Inputs^MAIN.RawNozzlePressure"], "AI Standard Channel 1^Value")
        self.assertEqual(links["PlcTask Inputs^MAIN.RawNozzlePressure2"], "AI Standard Channel 2^Value")


if __name__ == "__main__":
    unittest.main()
