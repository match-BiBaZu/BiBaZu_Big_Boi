"""Read-only EL7201 commissioning snapshot; never writes CoE or PLC values.

Uses the existing Windows ADS router/route. The EtherCAT master's AMS Net ID
is distinct from the controller's; each slave's fixed EtherCAT address is its
ADS port. Beckhoff documents CoE upload as ADS Read, group 0xF302, offset
(index << 16) | subindex:
https://infosys.beckhoff.com/content/1033/tcsystemmanager/1089026187.html

Run from an account that can access the TwinCAT ADS router. Missing objects
and symbols are recorded individually, so old PLC versions can be inspected.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pyads


# (CoE address, description, ADS datatype). All accesses use Connection.read.
OBJECTS = (
    ("1008:00", "terminal_name", "STRING"),
    ("1018:02", "terminal_product_code", "UDINT"),
    ("1018:03", "terminal_revision", "UDINT"),
    ("9009:03", "motor_serial", "STRING"),
    ("9009:04", "motor_order_code", "STRING"),
    ("9009:06", "nameplate_pole_pairs", "UDINT"),
    ("9009:07", "nameplate_standstill_current_rms_mA", "UDINT"),
    ("9009:08", "nameplate_rated_current_rms_mA", "UDINT"),
    ("9009:09", "nameplate_peak_current_rms_mA", "UDINT"),
    ("9009:13", "nameplate_max_speed_rpm", "UDINT"),
    ("9009:1B", "nameplate_brake_type", "STRING"),
    ("9009:20", "nameplate_brake_apply_ms", "UINT"),
    ("9009:21", "nameplate_brake_release_ms", "UINT"),
    ("9008:12", "native_encoder_resolution", "UDINT"),
    ("9008:13", "native_encoder_revolutions", "UDINT"),
    ("9010:12", "dc_link_mV", "UDINT"),
    ("9010:14", "velocity_units_per_rev_per_sec", "UDINT"),
    ("9010:15", "position_resolution_increments", "UDINT"),
    ("9010:16", "position_resolution_revolutions", "UDINT"),
    ("8000:01", "feedback_inverted", "BOOL"),
    ("8000:12", "singleturn_bits", "USINT"),
    ("8000:13", "multiturn_bits", "USINT"),
    ("8008:01", "autoconfig_enabled", "BOOL"),
    ("8008:02", "reconfigure_identical_motor", "BOOL"),
    ("8008:03", "reconfigure_nonidentical_motor", "BOOL"),
    ("8010:12", "current_loop_integral_0_1ms", "UINT"),
    ("8010:13", "current_loop_gain_0_1V_per_A", "UINT"),
    ("8010:14", "velocity_loop_integral_0_1ms", "UDINT"),
    ("8010:15", "velocity_loop_gain_mA_per_rad_per_sec", "UDINT"),
    ("8010:19", "nominal_dc_link_mV", "UDINT"),
    ("8010:1A", "minimum_dc_link_mV", "UDINT"),
    ("8010:1B", "maximum_dc_link_mV", "UDINT"),
    ("8010:31", "amplifier_speed_limit_rpm", "UDINT"),
    ("8010:54", "current_interpretation_feature_bits", "UDINT"),
    ("8010:65", "rotation_inverted", "BOOL"),
    ("8011:11", "motor_max_current_mA", "UDINT"),
    ("8011:12", "motor_rated_current_mA", "UDINT"),
    ("8011:13", "motor_pole_pairs", "USINT"),
    ("8011:15", "commutation_offset_degrees", "INT"),
    ("8011:16", "torque_constant_mNm_per_A", "UDINT"),
    ("8011:18", "rotor_inertia_g_cm2", "UDINT"),
    ("8011:19", "winding_inductance_0_1mH", "UINT"),
    ("8011:1B", "motor_speed_limit_rpm", "UDINT"),
    ("8011:29", "motor_i2t_warning_percent", "USINT"),
    ("8011:2A", "motor_i2t_error_percent", "USINT"),
    ("8011:2B", "motor_temperature_warning_0_1C", "UINT"),
    ("8011:2C", "motor_temperature_error_0_1C", "UINT"),
    ("8011:2D", "motor_thermal_time_constant_0_1sec", "UINT"),
    ("8012:01", "brake_manual_override", "BOOL"),
    ("8012:02", "brake_manual_state", "BOOL"),
    ("8012:11", "brake_release_delay_ms", "UINT"),
    ("8012:12", "brake_application_delay_ms", "UINT"),
    ("8012:13", "brake_emergency_application_timeout_ms", "UINT"),
    ("8012:14", "brake_inertia_g_cm2", "UINT"),
    ("7010:01", "controlword", "UINT"),
    ("7010:03", "requested_mode", "SINT"),
    ("7010:06", "target_velocity", "DINT"),
    ("6010:01", "statusword", "UINT"),
    ("6010:03", "actual_mode", "SINT"),
    ("6010:07", "actual_velocity", "DINT"),
    ("6000:0E", "feedback_invalid", "BOOL"),
    ("6000:11", "actual_position", "UDINT"),
)

PLC_SYMBOLS = (
    ("GuiConveyorEnabled", "BOOL"),
    ("GuiConveyorCalibrationMode", "BOOL"),
    ("GuiConveyorSpeedMmPerSec", "REAL"),
    ("GuiConveyorMmPerFullStep", "REAL"),
    ("GuiConveyorCalibrationValid", "BOOL"),
    ("StepperPosBusy", "BOOL"),
    ("StepperPosError", "BOOL"),
    ("ConveyorServoCommissioned", "BOOL"),
    ("ConveyorServoFaultCode", "UINT"),
    ("ConveyorServoState", "UINT"),
    ("ConveyorServoDirection1", "INT"),
    ("ConveyorServoDirection2", "INT"),
    ("ConveyorServoMotor2Ratio", "LREAL"),
    ("ConveyorServoFeedbackCountsPerRev", "LREAL"),
    ("ConveyorServoFeedbackCountsPerRev2", "LREAL"),
    ("ConveyorServoVelocityUnitsPerRevPerSec", "LREAL"),
    ("ConveyorServoVelocityUnitsPerRevPerSec2", "LREAL"),
    ("ConveyorServoCalibratedMmPerFullStep", "REAL"),
    ("TandemConveyor.ConfigLoaded", "BOOL"),
    ("TandemConveyor.HealthyInputs", "BOOL"),
    ("TandemConveyor.StartupComplete", "BOOL"),
    ("TandemConveyor.FeedbackInitialized", "BOOL"),
    ("TandemConveyor.EncoderStationary", "BOOL"),
    ("TandemConveyor.SpeedSettled", "BOOL"),
    ("TandemConveyor.ResetActive", "BOOL"),
) + tuple(
    (f"ConveyorServo{motor}{suffix}", datatype)
    for motor in (1, 2)
    for suffix, datatype in (
        ("InfoState", "UINT"), ("WcState", "BOOL"),
        ("FeedbackInvalid", "BOOL"), ("Statusword", "UINT"),
        ("ActualVelocity", "DINT"), ("Position", "UDINT"),
        ("Controlword", "UINT"), ("TargetVelocity", "DINT"),
    )
)


def attempt(read):
    try:
        return {"value": read()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect(args):
    report = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "controller_net_id": args.controller_net_id,
        "master_net_id": args.master_net_id,
        "slaves": [],
    }
    with pyads.Connection(args.master_net_id, 65535) as master:
        master.set_timeout(args.timeout_ms)
        report["master_state"] = attempt(
            lambda: master.read(3, 0x100, pyads.PLCTYPE_UINT)
        )
    for slave_address in args.slaves:
        slave = {"ethercat_address": slave_address, "objects": {}}
        with pyads.Connection(args.master_net_id, slave_address) as drive:
            drive.set_timeout(args.timeout_ms)
            for address, name, datatype in OBJECTS:
                index, subindex = (int(part, 16) for part in address.split(":"))
                result = attempt(lambda: drive.read(
                    0xF302, (index << 16) | subindex,
                    getattr(pyads, "PLCTYPE_" + datatype),
                ))
                slave["objects"][address] = {"name": name, **result}
            slave["pdo_assignment"] = {}
            for index in (0x1C12, 0x1C13):
                count = attempt(lambda: drive.read(
                    0xF302, index << 16, pyads.PLCTYPE_USINT,
                ))
                assignment = {"count": count, "entries": []}
                if "value" in count:
                    for subindex in range(1, count["value"] + 1):
                        assignment["entries"].append(attempt(lambda: drive.read(
                            0xF302, (index << 16) | subindex, pyads.PLCTYPE_UINT,
                        )))
                slave["pdo_assignment"][f"{index:04X}"] = assignment
        report["slaves"].append(slave)
    with pyads.Connection(args.controller_net_id, args.plc_port) as plc:
        plc.set_timeout(args.timeout_ms)
        report["plc_state"] = attempt(plc.read_state)
        report["plc_symbols"] = {}
        for name, datatype in PLC_SYMBOLS:
            symbol = "MAIN." + name
            report["plc_symbols"][symbol] = attempt(lambda: plc.read_by_name(
                symbol, getattr(pyads, "PLCTYPE_" + datatype),
            ))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-net-id", default="10.145.4.14.1.1")
    parser.add_argument("--master-net-id", default="192.168.178.10.2.1")
    parser.add_argument("--slaves", nargs=2, type=int, default=[1010, 1011])
    parser.add_argument("--plc-port", type=int, default=851)
    parser.add_argument("--timeout-ms", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = collect(args)
    except Exception as exc:
        report = {"read_only": True, "error": f"{type(exc).__name__}: {exc}"}
    content = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
