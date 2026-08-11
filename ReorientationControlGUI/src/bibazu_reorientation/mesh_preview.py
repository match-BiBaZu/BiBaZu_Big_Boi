from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF


def load_mesh_triangles(path: Path) -> np.ndarray:
    """Load triangle vertices from the small STL/OBJ workpiece files."""
    source = Path(path)
    if source.suffix.lower() == ".stl":
        data = source.read_bytes()
        if len(data) >= 84:
            count = struct.unpack_from("<I", data, 80)[0]
            if 84 + count * 50 == len(data):
                triangles = np.empty((count, 3, 3), dtype=np.float64)
                for index in range(count):
                    values = struct.unpack_from("<12fH", data, 84 + index * 50)
                    triangles[index] = np.asarray(values[3:12]).reshape(3, 3)
                return triangles
        vertices: list[list[float]] = []
        for line in data.decode("utf-8", errors="ignore").splitlines():
            fields = line.strip().split()
            if len(fields) == 4 and fields[0].lower() == "vertex":
                vertices.append([float(value) for value in fields[1:]])
        if len(vertices) and len(vertices) % 3 == 0:
            return np.asarray(vertices, dtype=np.float64).reshape(-1, 3, 3)
        raise ValueError(f"STL does not contain readable triangles: {source}")

    if source.suffix.lower() == ".obj":
        vertices = []
        faces: list[tuple[int, int, int]] = []
        for line in source.read_text(encoding="utf-8", errors="ignore").splitlines():
            fields = line.strip().split()
            if len(fields) >= 4 and fields[0] == "v":
                vertices.append([float(value) for value in fields[1:4]])
            elif len(fields) >= 4 and fields[0] == "f":
                indices = [int(value.split("/", 1)[0]) - 1 for value in fields[1:]]
                for index in range(1, len(indices) - 1):
                    faces.append((indices[0], indices[index], indices[index + 1]))
        if vertices and faces:
            return np.asarray(vertices, dtype=np.float64)[np.asarray(faces)]
        raise ValueError(f"OBJ does not contain readable triangles: {source}")

    raise ValueError(f"Unsupported 3D format: {source.suffix}")


def _quaternion_matrix(
    quaternion_xyzw: tuple[float, float, float, float],
) -> np.ndarray:
    x, y, z, w = quaternion_xyzw
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def slerp_quaternion(
    start_xyzw: tuple[float, float, float, float],
    end_xyzw: tuple[float, float, float, float],
    progress: float,
) -> tuple[float, float, float, float]:
    """Interpolate normalized XYZW quaternions along the shortest rotation."""
    start = np.asarray(start_xyzw, dtype=np.float64)
    end = np.asarray(end_xyzw, dtype=np.float64)
    start_norm = float(np.linalg.norm(start))
    end_norm = float(np.linalg.norm(end))
    if start_norm < 1e-12 or end_norm < 1e-12:
        raise ValueError("Cannot interpolate a zero-length quaternion")
    start /= start_norm
    end /= end_norm
    dot = float(np.dot(start, end))
    if dot < 0.0:
        end = -end
        dot = -dot
    amount = min(1.0, max(0.0, float(progress)))
    if dot > 0.9995:
        result = start + amount * (end - start)
        result /= np.linalg.norm(result)
    else:
        angle = float(np.arccos(np.clip(dot, -1.0, 1.0)))
        denominator = np.sin(angle)
        result = (
            np.sin((1.0 - amount) * angle) / denominator * start
            + np.sin(amount * angle) / denominator * end
        )
    return tuple(float(value) for value in result)  # type: ignore[return-value]


def render_mesh_preview(
    path: Path,
    width: int = 250,
    height: int = 175,
    quaternion_xyzw: tuple[float, float, float, float] | None = None,
    caption: str = "Target orientation · model view",
) -> QPixmap:
    return render_triangles_preview(
        load_mesh_triangles(path),
        width,
        height,
        quaternion_xyzw=quaternion_xyzw,
        caption=caption,
    )


def render_triangles_preview(
    triangles: np.ndarray,
    width: int = 250,
    height: int = 175,
    quaternion_xyzw: tuple[float, float, float, float] | None = None,
    caption: str = "Target orientation · model view",
) -> QPixmap:
    """Render already-loaded triangles, suitable for lightweight animation frames."""
    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or len(triangles) == 0:
        raise ValueError("Triangle data must have shape (N, 3, 3)")
    points = triangles.reshape(-1, 3)
    center = (points.min(axis=0) + points.max(axis=0)) * 0.5
    centered = triangles - center
    if quaternion_xyzw is not None:
        centered = centered @ _quaternion_matrix(quaternion_xyzw).T

    azimuth = np.deg2rad(-40.0)
    elevation = np.deg2rad(24.0)
    rotate_z = np.array(
        [
            [np.cos(azimuth), -np.sin(azimuth), 0.0],
            [np.sin(azimuth), np.cos(azimuth), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    rotate_x = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, np.cos(elevation), -np.sin(elevation)],
            [0.0, np.sin(elevation), np.cos(elevation)],
        ]
    )
    camera_triangles = centered @ (rotate_x @ rotate_z).T
    projected = camera_triangles[..., :2].copy()
    projected[..., 1] *= -1.0
    flat = projected.reshape(-1, 2)
    span = np.maximum(flat.max(axis=0) - flat.min(axis=0), 1e-9)
    scale = min((width - 28) / span[0], (height - 28) / span[1])
    projected = (projected - (flat.min(axis=0) + flat.max(axis=0)) * 0.5) * scale
    projected[..., 0] += width * 0.5
    projected[..., 1] += height * 0.5

    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#111827"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    light = np.asarray((-0.35, -0.45, 0.82))
    order = np.argsort(camera_triangles[..., 2].mean(axis=1))
    for index in order:
        triangle = camera_triangles[index]
        normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        length = float(np.linalg.norm(normal))
        brightness = 0.55 if length == 0 else 0.42 + 0.48 * abs(float(normal @ light) / length)
        base = np.asarray((56, 189, 248)) * brightness
        painter.setBrush(QColor(*(int(value) for value in np.clip(base, 0, 255))))
        painter.setPen(QPen(QColor("#164e63"), 0.7))
        painter.drawPolygon(QPolygonF([QPointF(float(x), float(y)) for x, y in projected[index]]))
    painter.setPen(QPen(QColor("#94a3b8"), 1.0))
    painter.drawText(8, height - 8, caption)
    painter.end()
    return pixmap
