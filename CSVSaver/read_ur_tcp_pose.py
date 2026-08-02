import argparse
import math
import socket
import struct
import time


ROBOT_STATE_MESSAGE = 16
CARTESIAN_INFO_PACKAGE = 4


def receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("UR closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_tcp_pose_from_connection(
    connection: socket.socket, timeout: float = 3.0
) -> tuple[float, ...]:
    deadline = time.monotonic() + timeout
    connection.settimeout(timeout)
    while time.monotonic() < deadline:
        message_size = struct.unpack("!I", receive_exact(connection, 4))[0]
        if message_size < 5 or message_size > 10_000_000:
            raise ValueError(f"Invalid UR message size: {message_size}")
        message = receive_exact(connection, message_size - 4)
        if message[0] != ROBOT_STATE_MESSAGE:
            continue

        offset = 1
        while offset + 5 <= len(message):
            package_size = struct.unpack_from("!I", message, offset)[0]
            if package_size < 5 or offset + package_size > len(message):
                break
            package_type = message[offset + 4]
            if package_type == CARTESIAN_INFO_PACKAGE and package_size >= 53:
                return struct.unpack_from("!6d", message, offset + 5)
            offset += package_size
    raise TimeoutError("No CartesianInfo package received from UR")


def read_tcp_pose(host: str, port: int = 30001, timeout: float = 3.0) -> tuple[float, ...]:
    with socket.create_connection((host, port), timeout=timeout) as connection:
        return read_tcp_pose_from_connection(connection, timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read the current UR TCP pose")
    parser.add_argument("host", nargs="?", default="10.10.10.10")
    parser.add_argument("--port", type=int, default=30001)
    args = parser.parse_args()

    x, y, z, rx, ry, rz = read_tcp_pose(args.host, args.port)
    rotation_angle = math.degrees(math.sqrt(rx * rx + ry * ry + rz * rz))
    print(f"TCP position [m]:  x={x:.6f}, y={y:.6f}, z={z:.6f}")
    print(f"TCP position [mm]: x={x * 1000:.3f}, y={y * 1000:.3f}, z={z * 1000:.3f}")
    print(f"TCP rotation vector [rad]: rx={rx:.6f}, ry={ry:.6f}, rz={rz:.6f}")
    print(f"Rotation-vector magnitude: {rotation_angle:.3f} deg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
