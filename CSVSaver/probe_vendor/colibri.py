"""Colibri BAC protocol extracted from the mini test stand; no GUI imports."""
import threading
import time

try:
    import serial
except ImportError:
    serial = None

COLIBRI_BAUD_RATE = 9600
COLIBRI_SLAVE_ADDRESS = 0xFF
COLIBRI_REFERENCE_CURRENT_PERCENT = 20
SERIAL_WRITE_TIMEOUT = 0.3

class ColibriProtocolError(Exception):
    pass


class ColibriController:
    START_BLOCK = 0x04
    END_BLOCK = 0x05
    SHIFT = 0x06

    TG_REQ_STATUS = 0x01
    TG_REQ_ERROR = 0x02
    TG_REQ_POSITION = 0x03
    TG_REQ_PARAM = 0x06
    TG_MOVE_REL = 0x15
    TG_MOTOR = 0x16
    TG_MOVE_ABS = 0x1A
    TG_SET_PARAM = 0x1F

    TG_STATUS = 0x80
    TG_ERROR = 0x81
    TG_PARAM = 0x83
    TG_POSITION = 0x84
    TG_MOVING = 0x85

    MOTOR_ESTOP = 0
    MOTOR_STOP = 1
    MOTOR_REF = 2
    MOTOR_REMOTE = 5
    MOTOR_SET_REFERENCE_POINT = 8
    MOTOR_EXIT_REMOTE = 10
    MOTOR_DISABLE = 11
    MOTOR_ENABLE = 12

    def __init__(self, port, baudrate=COLIBRI_BAUD_RATE, slave_address=COLIBRI_SLAVE_ADDRESS, debug_logger=None):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.port_name = port
        self.slave_address = slave_address
        self.debug_logger = debug_logger
        self.serial_port = serial.Serial(
            port,
            baudrate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.08,
            write_timeout=SERIAL_WRITE_TIMEOUT,
        )
        self.lock = threading.Lock()

    def set_debug_logger(self, debug_logger):
        self.debug_logger = debug_logger

    def close(self):
        self.serial_port.close()

    def status(self):
        response = self._request(self.TG_REQ_STATUS, expected_type=self.TG_STATUS)
        if len(response) < 7:
            raise ColibriProtocolError(f"Short status response: {response.hex(' ')}")
        # This controller sends system status BEFORE motor status. Verified on
        # COM3: motor status 0x78 -> 0x79 during motion, 0x68 during referencing.
        return self._decode_status(response[4], response[3], response[5])

    def position_steps(self):
        response = self._request(self.TG_REQ_POSITION, expected_type=self.TG_POSITION)
        if len(response) < 7:
            raise ColibriProtocolError(f"Short position response: {response.hex(' ')}")
        return int.from_bytes(response[3:7], byteorder="little", signed=True)

    def error(self):
        response = self._request(self.TG_REQ_ERROR, expected_type=self.TG_ERROR)
        if len(response) < 5:
            raise ColibriProtocolError(f"Short error response: {response.hex(' ')}")
        return {
            "last_error": response[3],
            "details": response[4],
        }

    def parameter(self, index, subindex):
        response = self._request(self.TG_REQ_PARAM, index, subindex, expected_type=self.TG_PARAM)
        if len(response) < 6:
            raise ColibriProtocolError(f"Short parameter response: {response.hex(' ')}")
        if response[3] != index or response[4] != subindex:
            raise ColibriProtocolError(f"Unexpected parameter response: {response.hex(' ')}")
        return int.from_bytes(response[5:-1], byteorder="little", signed=False)

    def set_parameter(self, index, subindex, value, byte_count):
        value_bytes = int(value).to_bytes(byte_count, byteorder="little", signed=False)
        return self._request(
            self.TG_SET_PARAM,
            index,
            subindex,
            *value_bytes,
            expected_type=None,
            retry_on_timeout=False,
        )

    def set_remote(self):
        return self.motor_command(self.MOTOR_REMOTE)

    def enable(self):
        return self.motor_command(self.MOTOR_ENABLE)

    def disable(self):
        return self.motor_command(self.MOTOR_DISABLE)

    def stop(self):
        return self.motor_command(self.MOTOR_STOP)

    def emergency_stop(self):
        return self.motor_command(self.MOTOR_ESTOP)

    def reference(self):
        return self.motor_command(self.MOTOR_REF)

    def set_current_position_as_reference(self):
        return self.motor_command(self.MOTOR_SET_REFERENCE_POINT)

    def configure_negative_reference(self):
        self.set_parameter(4, 1, 2, 1)
        self.set_parameter(5, 2, COLIBRI_REFERENCE_CURRENT_PERCENT, 1)

    def motor_command(self, command):
        return self._request(self.TG_MOTOR, command, expected_type=None, retry_on_timeout=False)

    def move_relative_steps(self, steps):
        return self._request(self.TG_MOVE_REL, *self._int32_bytes(steps), expected_type=None, retry_on_timeout=False)

    def move_absolute_steps(self, steps):
        return self._request(self.TG_MOVE_ABS, *self._int32_bytes(steps), expected_type=None, retry_on_timeout=False)

    def _request(self, *data, expected_type, retry_on_timeout=True):
        with self.lock:
            last_response = None
            last_error = None
            request_frame = self._build_frame(data)
            attempts = 2 if retry_on_timeout else 1
            for attempt in range(attempts):
                self.serial_port.reset_input_buffer()
                self._trace(f"TX {request_frame.hex(' ')}")
                self.serial_port.write(request_frame)
                self.serial_port.flush()
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    response = self._read_frame(deadline)
                    if response is None:
                        continue
                    self._trace(f"RX {response.hex(' ')}")
                    try:
                        self._validate_response(response)
                    except ColibriProtocolError as exc:
                        last_error = exc
                        self._trace(f"RX ignored {exc}")
                        continue
                    last_response = response
                    if len(response) >= 3 and response[2] == self.TG_ERROR and expected_type != self.TG_ERROR:
                        continue
                    if expected_type is None or (len(response) >= 3 and response[2] == expected_type):
                        return response
                if attempt + 1 < attempts:
                    time.sleep(0.05)
            if last_response is not None:
                raise ColibriProtocolError(f"Unexpected response: {last_response.hex(' ')}")
            if last_error is not None:
                raise last_error
            raise TimeoutError("No response from Colibri")

    def _build_frame(self, data):
        payload = bytes([self.slave_address, len(data) + 1, *data])
        checksum = sum(payload) & 0xFF
        return bytes([self.START_BLOCK]) + self._escape(payload + bytes([checksum])) + bytes([self.END_BLOCK])

    def _escape(self, data):
        escaped = bytearray()
        for value in data:
            if value in (self.START_BLOCK, self.END_BLOCK, self.SHIFT):
                escaped.extend([self.SHIFT, (value + self.SHIFT) & 0xFF])
            else:
                escaped.append(value)
        return bytes(escaped)

    def _read_frame(self, deadline):
        frame = bytearray()
        in_frame = False
        shifted = False
        while time.monotonic() < deadline:
            byte = self.serial_port.read(1)
            if not byte:
                continue
            value = byte[0]
            if value == self.START_BLOCK:
                frame.clear()
                in_frame = True
                shifted = False
                continue
            if value == self.END_BLOCK and in_frame:
                return bytes(frame)
            if not in_frame:
                continue
            if shifted:
                frame.append((value - self.SHIFT) & 0xFF)
                shifted = False
            elif value == self.SHIFT:
                shifted = True
            else:
                frame.append(value)
        return None

    def _validate_response(self, response):
        if len(response) < 4:
            raise ColibriProtocolError(f"Frame too short: {response.hex(' ')}")
        checksum = sum(response[:-1]) & 0xFF
        if checksum != response[-1]:
            raise ColibriProtocolError(
                f"Bad checksum: expected {checksum:02x}, got {response[-1]:02x}"
            )

    def _decode_status(self, status_byte, system_status_byte, error_byte):
        return {
            "status_byte": status_byte,
            "moving": bool(status_byte & 0x01),
            "software_limit": bool(status_byte & 0x02),
            "ready": bool(status_byte & 0x08),
            "referenced": bool(status_byte & 0x10),
            "remote": bool(status_byte & 0x20),
            "enabled": bool(status_byte & 0x40),
            "password": bool(status_byte & 0x80),
            "system_status_byte": system_status_byte,
            "rotation_error": bool(system_status_byte & 0x01),
            "error_byte": error_byte,
            "error": bool(error_byte & 0x01),
            "watchdog_error": bool(error_byte & 0x02),
            "burnout_error": bool(error_byte & 0x04),
            "eeprom_error": bool(error_byte & 0x08),
            "motor_voltage_error": bool(error_byte & 0x10),
            "temperature_error": bool(error_byte & 0x20),
            "mark_error": bool(error_byte & 0x40),
            "bootloader_error": bool(error_byte & 0x80),
        }

    def _int32_bytes(self, value):
        return int(value).to_bytes(4, byteorder="little", signed=True)

    def _trace(self, message):
        if self.debug_logger:
            self.debug_logger(f"COLIBRI {message}")
