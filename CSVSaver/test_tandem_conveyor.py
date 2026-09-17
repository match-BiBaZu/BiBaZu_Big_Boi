"""Behavior tests execute FB_TandemConveyor's actual ST with a software drive plant.

No ADS connection, live PLC, motor enable, or external dependencies are used.
The model exercises software logic; it cannot validate motor tuning or mechanics.
"""
from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parent
FB_PATH = ROOT / "TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/Untitled1/POUs/FB_TandemConveyor.TcPOU"
spec = importlib.util.spec_from_file_location(
    "tandem_st_test_simulator", ROOT / "tests/st_source_simulator.py"
)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)
StSimulation = simulator.StSimulation


class DrivePlant:
    """Two independent velocity plants, with raw encoder/scaling and optional faults."""

    def __init__(self, direction2=1, ratio=1.0, initial_position=0, warm_up=True):
        self.sim = StSimulation(FB_PATH)
        self.position = [float(initial_position), float(initial_position)]
        self.velocity = [0.0, 0.0]
        self.units = [1000000.0, 2000000.0]
        self.counts = [1048576.0, 2097152.0]
        self.stalled = [False, False]
        self.speed_gain = [1.0, 1.0]
        self.hold_disabled = [False, False]
        self.overrides = {}
        self.sim.step(
            Commissioned=True, Direction1=1, Direction2=direction2, Motor2Ratio=ratio,
            FeedbackCountsPerRev=self.counts[0], FeedbackCountsPerRev2=self.counts[1],
            VelocityUnitsPerRevPerSec=self.units[0], VelocityUnitsPerRevPerSec2=self.units[1],
            Position1=initial_position, Position2=initial_position,
            Statusword1=64, Statusword2=64, InfoState1=8, InfoState2=8,
        )
        if warm_up:
            for _ in range(120):
                self.step()

    @property
    def values(self):
        return self.sim.values

    def step(self, **commands):
        inputs = {}
        for index in range(2):
            axis = index + 1
            control = self.values[f"Controlword{axis}"]
            status = {0: 64, 6: 1, 7: 3, 15: 7, 128: 64}[control]
            if self.hold_disabled[index]:
                status = 64
            if status == 7 and not self.stalled[index]:
                desired = self.values[f"TargetVelocity{axis}"] * self.speed_gain[index]
                self.velocity[index] += (desired - self.velocity[index]) * 0.1
            else:
                self.velocity[index] = 0.0
            self.position[index] += (
                self.velocity[index] / self.units[index] * self.counts[index] * self.sim.dt
            )
            inputs[f"Statusword{axis}"] = status
            inputs[f"Position{axis}"] = round(self.position[index]) % (1 << 32)
            inputs[f"ActualVelocity{axis}"] = round(self.velocity[index])
        inputs.update(self.overrides)
        inputs.update(commands)
        return self.sim.step(**inputs)

    def enable(self):
        self.step(Enable=True)
        for _ in range(100):
            values = self.step()
            if values["Ready"]:
                return
        raise AssertionError(f"Enable failed: state={values['State']} fault={values['FaultCode']}")

    def run_until(self, predicate, limit=10000):
        for _ in range(limit):
            values = self.step()
            if predicate(values):
                return values
        raise AssertionError(f"Condition timeout: state={values['State']} fault={values['FaultCode']}")


class TandemConveyorTests(unittest.TestCase):
    def assert_pair_zero(self, values):
        self.assertEqual((values["Controlword1"], values["Controlword2"]), (0, 0))
        self.assertEqual((values["TargetVelocity1"], values["TargetVelocity2"]), (0, 0))

    def test_defaults_and_invalid_config_inhibit(self):
        simulation = StSimulation(FB_PATH)
        for _ in range(10):
            values = simulation.step(Enable=True, Execute=True)
            self.assert_pair_zero(values)
        values = simulation.step(Commissioned=True, InfoState1=8, InfoState2=8)
        self.assertTrue(values["Error"])
        self.assertEqual(values["FaultCode"], 1)
        self.assert_pair_zero(values)

    def test_initial_stale_enable_reports_fault_instead_of_silent_deadlock(self):
        plant = DrivePlant()
        # Start again before the first disabled configuration-capture scan.
        fresh = StSimulation(FB_PATH)
        names = ["Direction1", "Direction2", "Motor2Ratio", "FeedbackCountsPerRev",
                 "FeedbackCountsPerRev2", "VelocityUnitsPerRevPerSec", "VelocityUnitsPerRevPerSec2"]
        values = fresh.step(**{name: plant.values[name] for name in names},
                            Commissioned=True, Enable=True, InfoState1=8, InfoState2=8,
                            Statusword1=64, Statusword2=64)
        self.assertEqual(values["FaultCode"], 1)
        self.assert_pair_zero(values)

    def test_both_drives_ready_before_velocity_and_enable_timeout(self):
        plant = DrivePlant()
        plant.hold_disabled[1] = True
        plant.step(Enable=True)
        for _ in range(100):
            values = plant.step()
            self.assertFalse(values["Ready"])
            self.assertEqual(values["TargetVelocity1"], 0)
            self.assertEqual(values["TargetVelocity2"], 0)
        values = plant.run_until(lambda v: v["Error"], limit=5200)
        self.assertEqual(values["FaultCode"], 4)
        self.assert_pair_zero(values)

    def test_forward_reverse_finite_moves_use_both_measured_encoders(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                plant = DrivePlant(direction2=-1, ratio=1.5)
                plant.enable()
                plant.step(Execute=True, StartType=2, TargetPosition=(direction * 100 * 64) % (1 << 32),
                           Velocity=2500)
                self.assertTrue(plant.values["Busy"])
                values = plant.run_until(lambda v: v["InTarget"] or v["Error"], limit=10000)
                self.assertFalse(values["Error"], values["FaultCode"])
                self.assertTrue(values["InTarget"])
                self.assertFalse(values["Busy"])
                measured = simulator.convert(values["LegacyPosition"], "DINT") / 64.0
                self.assertAlmostEqual(measured, direction * 100, delta=0.25)
                self.assertLess(abs(values["SyncErrorFullSteps"]), 0.25)
                for axis in (1, 2):
                    self.assertLess(abs(plant.velocity[axis-1] / plant.units[axis-1] * 200), 1.0)
                self.assertTrue(values["EncoderStationary"])

    def test_position_offset_and_small_speed_drift_never_correct_velocity(self):
        plant = DrivePlant()
        plant.position[1] += 30 * plant.counts[1] / 200
        for _ in range(1200):
            plant.step()
        plant.speed_gain[1] = 0.98
        plant.enable()
        plant.step(Execute=True, StartType=3, Velocity=500)
        for _ in range(4000):
            v = plant.step()
            self.assertFalse(v['Error'], v['FaultCode'])
            # Different raw velocity scales still command the same physical speed.
            self.assertAlmostEqual(v['TargetVelocity1']/plant.units[0],
                                   v['TargetVelocity2']/plant.units[1], delta=0.000002)
        self.assertGreater(abs(v['SyncErrorFullSteps']), 5)

    def test_device4_only_never_enables_device3_and_stops_without_position_hold(self):
        plant = DrivePlant()
        plant.step(Commissioned=False, MotorCount=1, SingleMotor=2)
        plant.step(Commissioned=True)
        for _ in range(120):plant.step()
        plant.step(Reset=True)
        plant.step(Reset=False)
        plant.run_until(lambda v: not v['ResetActive'] and v['FeedbackInitialized'])
        before = plant.position[0]
        plant.enable()
        plant.step(Execute=True, StartType=2, TargetPosition=20*64, Velocity=500)
        plant.speed_gain[1] = 0.97
        for _ in range(5000):
            v=plant.step()
            self.assertEqual(v['Controlword1'],0)
            self.assertEqual(v['TargetVelocity1'],0)
            self.assertFalse(v['Error'],v['FaultCode'])
            if v['InTarget']:break
        self.assertTrue(v['InTarget'])
        self.assertEqual(plant.position[0],before)
        self.assertGreater(plant.position[1],0)
        # No final correction to remove the deliberately induced distance error.
        self.assertLess(v['LegacyPosition']/64,19.75)
        self.assertEqual(v['TargetVelocity2'],0)

    def test_raw_udint_wrap_is_measured_without_position_jump(self):
        plant = DrivePlant(initial_position=(1 << 32) - 300)
        plant.enable()
        plant.step(Execute=True, StartType=2, TargetPosition=20 * 64, Velocity=1000)
        values = plant.run_until(lambda v: v["InTarget"] or v["Error"])
        self.assertFalse(values["Error"], values["FaultCode"])
        self.assertAlmostEqual(values["LegacyPosition"] / 64, 20, delta=0.25)
        self.assertLess(values["Position1"], 1000000)

    def test_partner_fault_and_invalid_cyclic_data_zero_both_in_same_scan(self):
        for injection in ({"Statusword2": 8}, {"WcState2": True},
                          {"InfoState2": 4}, {"FeedbackInvalid2": True}, {"Statusword2": 64}):
            with self.subTest(injection=injection):
                plant = DrivePlant()
                plant.enable()
                plant.step(Execute=True, StartType=3, Velocity=1000)
                for _ in range(50):
                    plant.step()
                values = plant.step(**injection)
                self.assertTrue(values["Error"])
                self.assert_pair_zero(values)
                # Communications/status returning cannot restart a latched drive pair.
                for _ in range(20):
                    values = plant.step(WcState2=False, InfoState2=8, FeedbackInvalid2=False)
                    self.assertTrue(values["Error"])
                    self.assert_pair_zero(values)

    def test_reset_is_one_pulse_and_never_restarts_motion(self):
        plant = DrivePlant()
        plant.enable()
        plant.step(Execute=True, StartType=3, Velocity=1000)
        plant.step(WcState1=True)
        plant.step(WcState1=False, Enable=False, Execute=False)
        for _ in range(120):
            plant.step()
        values = plant.step(Reset=True)
        self.assertEqual((values["Controlword1"], values["Controlword2"]), (128, 128))
        for _ in range(50):
            values = plant.step()
            self.assertFalse(values["Error"])
            self.assert_pair_zero(values)
            self.assertFalse(values["Busy"])

    def test_initial_ethercat_transition_waits_without_latching_or_enabling(self):
        plant = DrivePlant(warm_up=False)
        for _ in range(300):
            values = plant.step(InfoState2=4, WcState2=True, FeedbackInvalid2=True)
            self.assertEqual(values["FaultCode"], 0)
            self.assertFalse(values["ConfigLoaded"])
            self.assert_pair_zero(values)
        plant.step(InfoState2=8, WcState2=False, FeedbackInvalid2=False)
        for _ in range(90):
            values = plant.step()
            self.assertFalse(values["ConfigLoaded"])
            self.assert_pair_zero(values)
        for _ in range(30):
            values = plant.step()
        self.assertTrue(values["ConfigLoaded"])
        self.assertTrue(values["FeedbackInitialized"])
        self.assertEqual(values["FaultCode"], 0)
        self.assert_pair_zero(values)
        # The grace period never applies again, including idle communication loss.
        values = plant.step(InfoState1=4)
        self.assertEqual(values["FaultCode"], 3)
        for _ in range(120):
            values = plant.step(InfoState1=8)
        self.assertEqual(values["FaultCode"], 3)
        self.assert_pair_zero(values)

    def test_startup_timeout_latches_and_requires_stopped_reset(self):
        plant = DrivePlant(warm_up=False)
        for _ in range(5020):
            values = plant.step(InfoState1=4)
            self.assert_pair_zero(values)
        self.assertEqual(values["FaultCode"], 3)
        for _ in range(120):
            values = plant.step(InfoState1=8)
        self.assertEqual(values["FaultCode"], 3)
        self.assertFalse(values["ConfigLoaded"])
        values = plant.step(Reset=True)
        self.assertEqual(values["Controlword1"], 128)
        values = plant.step(Reset=False)
        self.assertEqual(values["FaultCode"], 0)
        self.assertTrue(values["FeedbackInitialized"])
        self.assert_pair_zero(values)

    def test_stationary_encoder_jitter_with_noisy_velocity_allows_capture_and_reset(self):
        plant = DrivePlant(warm_up=False, initial_position=(1 << 32) - 4)
        for index in range(130):
            # Cross the UDINT wrap with <24 encoder counts of jitter. The raw
            # velocity corresponds to +/-8 virtual steps/s despite standstill.
            values = plant.step(Position1=(-4 + index % 16) % (1 << 32),
                                Position2=(-4 + index % 24) % (1 << 32),
                                ActualVelocity1=40000, ActualVelocity2=-80000)
            self.assert_pair_zero(values)
        self.assertTrue(values["ConfigLoaded"])
        self.assertTrue(values["SpeedSettled"])
        plant.step(WcState1=True)
        for _ in range(120):
            values = plant.step(WcState1=False, ActualVelocity1=-40000,
                                ActualVelocity2=80000)
        self.assertEqual(values["FaultCode"], 3)
        values = plant.step(Reset=True, ActualVelocity1=-40000, ActualVelocity2=80000)
        self.assertEqual(values["Controlword1"], 128)
        values = plant.step(Reset=False, ActualVelocity1=40000, ActualVelocity2=-80000)
        self.assertEqual(values["FaultCode"], 0)
        self.assert_pair_zero(values)

    def test_moving_encoder_rejects_reset_even_when_velocity_estimate_is_zero(self):
        for motor in (1, 2):
            with self.subTest(motor=motor):
                plant = DrivePlant()
                plant.step(WcState1=True)
                for index in range(300):
                    # 10 motor full steps/s, cumulative travel across the fixed
                    # standstill window must prevent rebase and fault reset.
                    values = plant.step(WcState1=False, **{
                        f"Position{motor}": round(index * plant.counts[motor - 1] / 20000),
                        "ActualVelocity1": 0, "ActualVelocity2": 0,
                        "Reset": index % 2 == 0,
                    })
                    self.assertEqual(values["FaultCode"], 3)
                    self.assertFalse(values["ResetActive"])
                    self.assert_pair_zero(values)

    def test_finite_move_and_stop_finish_with_noisy_velocity_feedback(self):
        for finite in (True, False):
            with self.subTest(finite=finite):
                plant = DrivePlant()
                plant.enable()
                plant.step(Execute=True, StartType=2 if finite else 3,
                           TargetPosition=20 * 64, Velocity=1000)
                for _ in range(300):
                    plant.step()
                if not finite:
                    plant.step(Execute=False)
                plant.overrides.update(ActualVelocity1=40000, ActualVelocity2=-80000)
                values = plant.run_until(lambda v: not v["Busy"] or v["Error"])
                self.assertEqual(values["FaultCode"], 0)
                self.assertEqual(values["InTarget"], finite)
                self.assertEqual(values["TargetVelocity1"], 0)
                self.assertEqual(values["TargetVelocity2"], 0)

    def test_normal_stop_decelerates_before_disabling(self):
        plant = DrivePlant()
        plant.enable()
        plant.step(Execute=True, StartType=3, Velocity=2500)
        for _ in range(700):
            plant.step()
        previous = plant.values["TargetVelocity1"]
        values = plant.step(Execute=False)
        self.assertEqual(values["State"], 40)
        self.assertTrue(values["Busy"])
        self.assertEqual(values["Controlword1"], 15)
        self.assertGreater(values["TargetVelocity1"], 0)
        self.assertLess(values["TargetVelocity1"], previous)
        values = plant.run_until(lambda v: not v["Busy"] or v["Error"])
        self.assertFalse(values["Error"], values["FaultCode"])
        self.assertFalse(values["InTarget"])
        self.assertEqual(values["TargetVelocity1"], 0)
        values = plant.step(Enable=False)
        self.assert_pair_zero(values)

    def test_emergency_requires_off_interval_then_allows_fresh_command(self):
        plant = DrivePlant()
        plant.enable()
        plant.step(Execute=True, StartType=3, Velocity=1000)
        for _ in range(20):
            plant.step()
        values = plant.step(EmergencyStop=True)
        self.assert_pair_zero(values)
        values = plant.step(EmergencyStop=False)
        self.assert_pair_zero(values)
        plant.step(EmergencyStop=True, Enable=False, Execute=False)
        plant.step(EmergencyStop=False)
        plant.enable()
        values = plant.step(Execute=True, StartType=3, Velocity=1000)
        self.assertTrue(values["Busy"])
        self.assertFalse(values["Error"])

    def test_stalled_partner_does_not_change_or_cancel_open_loop_targets(self):
        plant = DrivePlant()
        plant.enable()
        plant.stalled[1] = True
        plant.step(Execute=True, StartType=3, Velocity=1000)
        for _ in range(2000):
            values = plant.step()
            self.assertFalse(values["Error"], values["FaultCode"])
            self.assertEqual(
                values["TargetVelocity1"] / plant.units[0],
                values["TargetVelocity2"] / plant.units[1],
            )
        self.assertGreater(values["TargetVelocity1"], 0)
        self.assertGreater(values["TargetVelocity2"], 0)

    def test_live_configuration_changes_stop_and_require_explicit_reset(self):
        for change in ({"MotorCount": 1}, {"SingleMotor": 2}, {"Direction2": -1}, {"VelocityUnitsPerRevPerSec2": 1.0},
                       {"FeedbackCountsPerRev2": 42.0}, {"Motor2Ratio": float("nan")}):
            with self.subTest(change=change):
                plant = DrivePlant()
                plant.enable()
                plant.step(Execute=True, StartType=3, Velocity=1000)
                values = plant.step(**change)
                self.assertTrue(values["Error"])
                self.assert_pair_zero(values)

    def test_stopped_reset_selects_motor_one_and_restores_tandem(self):
        plant = DrivePlant()
        for count in (1, 2):
            plant.step(Commissioned=False, Enable=False, Execute=False)
            plant.step(MotorCount=count, Commissioned=True)
            for _ in range(120):
                values = plant.step()
                self.assert_pair_zero(values)
            self.assertEqual(values["FaultCode"], 8)
            plant.step(Reset=True)
            plant.step(Reset=False)
            values = plant.run_until(lambda v: not v["ResetActive"] and v["FeedbackInitialized"])
            self.assertFalse(values["Error"])
            self.assertEqual(values["CfgMotorCount"], count)
            self.assert_pair_zero(values)
            if count == 1:
                partner_position = plant.position[1]
                plant.enable()
                plant.step(Execute=True, StartType=2, TargetPosition=2 * 64, Velocity=80)
                for _ in range(5000):
                    values = plant.step()
                    self.assertEqual(values["Controlword2"], 0)
                    self.assertEqual(values["TargetVelocity2"], 0)
                    if values["InTarget"] or values["Error"]:
                        break
                self.assertFalse(values["Error"])
                self.assertTrue(values["InTarget"])
                self.assertEqual(plant.position[1], partner_position)


if __name__ == "__main__":
    unittest.main()
