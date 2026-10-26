"""Limits and stopped configuration for the Beckhoff prototype conveyor."""

import math

MAX_SPEED_MM_PER_SEC = 200.0
NOMINAL_MM_PER_FULL_STEP = math.pi * 50.0 / (200.0 * 32.0)
TANDEM_CONFIGURATION = {
    "MAIN.ConveyorServoCommissioned": True,
    "MAIN.ConveyorServoMotorCount": 2,
    "MAIN.ConveyorServoSingleMotor": 1,
    "MAIN.ConveyorServoDirection1": -1,
    "MAIN.ConveyorServoDirection2": -1,
    "MAIN.ConveyorServoMotor2Ratio": 1.0,
    "MAIN.ConveyorServoMaxMotorRpm": 0.0,
}


def validate_speed(speed: float, profile_maximum: float = MAX_SPEED_MM_PER_SEC) -> None:
    maximum = min(MAX_SPEED_MM_PER_SEC, profile_maximum)
    if not math.isfinite(speed) or not 0 < speed <= maximum:
        raise ValueError(f"Conveyor speed must be positive and at most {maximum:g} mm/s")
