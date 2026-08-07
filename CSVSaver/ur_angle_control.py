import time
from collections.abc import Callable

from vendor_ur_rtde import rtde


UR_HOST = "10.10.10.10"
UR_RTDE_PORT = 30004
UR_ANGLE_MIN_DEG = 15.5
UR_ANGLE_MAX_DEG = 21.0
UR_ANGLE_DEFAULT_DEG = 18.0
UR_ANGLE_STEP_DEG = 0.1
UR_COMMAND_TIMEOUT_SECONDS = 30.0


def angle_to_tenths(angle_deg: float) -> int:
    angle_tenths = int(round(float(angle_deg) * 10.0))
    if not 155 <= angle_tenths <= 210:
        raise ValueError("UR Ry angle must be between 15.5 and 21.0 degrees")
    return angle_tenths


class UrAngleClient:
    """Send one acknowledged angle command through the UR RTDE registers."""

    OUTPUT_NAMES = [
        "output_int_register_41",
        "output_int_register_42",
        "output_int_register_43",
    ]
    INPUT_NAMES = ["input_int_register_42", "input_int_register_43"]
    TYPES = ["INT32"]

    def __init__(
        self,
        host: str = UR_HOST,
        port: int = UR_RTDE_PORT,
        connection_factory: Callable[[str, int], object] = rtde.RTDE,
    ) -> None:
        self.host = host
        self.port = port
        self.connection_factory = connection_factory

    def apply_angle(
        self, angle_deg: float, timeout_seconds: float = UR_COMMAND_TIMEOUT_SECONDS
    ) -> dict:
        angle_tenths = angle_to_tenths(angle_deg)
        connection = self.connection_factory(self.host, self.port)
        started = False
        try:
            connection.connect()
            if not connection.send_output_setup(
                self.OUTPUT_NAMES, self.TYPES * len(self.OUTPUT_NAMES), frequency=10
            ):
                raise RuntimeError("UR rejected the RTDE output-register recipe")
            command_data = connection.send_input_setup(
                self.INPUT_NAMES, self.TYPES * len(self.INPUT_NAMES)
            )
            if command_data is None:
                raise RuntimeError(
                    "UR input registers 42/43 are already controlled by another RTDE client"
                )
            if not connection.send_start():
                raise RuntimeError("UR did not start RTDE synchronization")
            started = True

            initial_state = connection.receive()
            if initial_state is None:
                raise RuntimeError("UR closed the RTDE connection")
            last_command = int(initial_state.output_int_register_42)
            command = last_command + 1
            if command > 2_000_000_000:
                command = 1

            command_data.input_int_register_42 = angle_tenths
            command_data.input_int_register_43 = command
            if not connection.send(command_data):
                raise RuntimeError("UR angle command could not be sent")

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                state = connection.receive()
                if state is None:
                    raise RuntimeError("UR closed the RTDE connection")
                status = int(state.output_int_register_43)
                acknowledged_command = int(state.output_int_register_42)
                if status == -1 and acknowledged_command == command:
                    raise RuntimeError("UR program rejected the requested angle")
                if status == 3 and acknowledged_command == command:
                    applied_tenths = int(state.output_int_register_41)
                    if applied_tenths != angle_tenths:
                        raise RuntimeError(
                            "UR acknowledged a different angle "
                            f"({applied_tenths / 10.0:.1f} degrees)"
                        )
                    return {
                        "angle_deg": applied_tenths / 10.0,
                        "command": command,
                    }
            raise TimeoutError(
                "UR program did not acknowledge the angle within "
                f"{timeout_seconds:.0f} seconds"
            )
        finally:
            if started:
                try:
                    connection.send_pause()
                except Exception:
                    pass
            try:
                connection.disconnect()
            except Exception:
                pass
