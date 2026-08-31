from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF

COORDINATE_AXIS_COLORS = {
    "X": "#ef4444",
    "Y": "#22c55e",
    "Z": "#3b82f6",
}


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


def _display_rotation_matrix() -> np.ndarray:
    """Roadmap view: +X down-left/downhill, +Y down-right, and +Z up."""
    horizontal = 1.0 / np.sqrt(2.0)
    vertical = 1.0 / np.sqrt(6.0)
    depth = 1.0 / np.sqrt(3.0)
    return np.asarray(
        [
            [-horizontal, horizontal, 0.0],
            [-vertical, -vertical, 2.0 * vertical],
            [-depth, -depth, -depth],
        ],
        dtype=np.float64,
    )


def _draw_coordinate_axes(
    painter: QPainter,
    width: int,
    height: int,
    view_matrix: np.ndarray,
) -> None:
    """Draw a compact world-coordinate triad using the pose plot's camera."""
    origin = np.asarray((width - 42.0, 39.0), dtype=np.float64)
    axis_length = max(13.0, min(25.0, width * 0.11, height * 0.16))
    axes = (
        ("X", np.asarray((1.0, 0.0, 0.0))),
        ("Z", np.asarray((0.0, 0.0, 1.0))),
        ("Y", np.asarray((0.0, 1.0, 0.0))),
    )
    for label, axis in axes:
        transformed = axis @ view_matrix.T
        direction = transformed[:2].copy()
        direction[1] *= -1.0
        length = float(np.linalg.norm(direction))
        if length < 1e-9:
            color = QColor(COORDINATE_AXIS_COLORS[label])
            painter.setPen(QPen(color, 2.0))
            painter.setBrush(QColor("#111827"))
            painter.drawEllipse(QPointF(*origin), 6.0, 6.0)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(*origin), 2.0, 2.0)
            painter.drawText(QPointF(origin[0] - 15.0, origin[1] + 5.0), label)
            continue
        unit = direction / length
        end = origin + unit * axis_length
        color = QColor(COORDINATE_AXIS_COLORS[label])
        painter.setPen(QPen(color, 2.2))
        painter.drawLine(QPointF(*origin), QPointF(*end))
        perpendicular = np.asarray((-unit[1], unit[0]))
        arrow = QPolygonF(
            [
                QPointF(*end),
                QPointF(*(end - unit * 6.0 + perpendicular * 3.0)),
                QPointF(*(end - unit * 6.0 - perpendicular * 3.0)),
            ]
        )
        painter.setBrush(color)
        painter.drawPolygon(arrow)
        text_position = end + unit * 8.0 + np.asarray((0.0, 4.0))
        painter.drawText(QPointF(*text_position), label)


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
    oriented = triangles - center
    if quaternion_xyzw is not None:
        oriented = oriented @ _quaternion_matrix(quaternion_xyzw).T

    # Put every orientation back into the physical chute corner: floor z=0,
    # wall y=0, and +Y points into the chute.
    oriented_points = oriented.reshape(-1, 3)
    oriented = oriented + np.asarray(
        (0.0, -oriented_points[:, 1].min(), -oriented_points[:, 2].min())
    )
    oriented_points = oriented.reshape(-1, 3)
    minimum = oriented_points.min(axis=0)
    maximum = oriented_points.max(axis=0)
    object_span = np.maximum(maximum - minimum, 1e-9)
    margin = 0.12 * float(np.max(object_span))
    x0, x1 = minimum[0] - margin, maximum[0] + margin
    y1 = maximum[1] + margin
    z1 = maximum[2] + margin
    floor = np.asarray([(x0, 0.0, 0.0), (x1, 0.0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)])
    wall = np.asarray([(x0, 0.0, 0.0), (x1, 0.0, 0.0), (x1, 0.0, z1), (x0, 0.0, z1)])

    view_matrix = _display_rotation_matrix()
    camera_triangles = oriented @ view_matrix.T
    camera_floor = floor @ view_matrix.T
    camera_wall = wall @ view_matrix.T

    def project(values: np.ndarray) -> np.ndarray:
        result = values[..., :2].copy()
        result[..., 1] *= -1.0
        return result

    projected = project(camera_triangles)
    projected_floor = project(camera_floor)
    projected_wall = project(camera_wall)
    flat = np.concatenate((projected.reshape(-1, 2), projected_floor, projected_wall), axis=0)
    span = np.maximum(flat.max(axis=0) - flat.min(axis=0), 1e-9)
    drawing_height = max(1, height - 22)
    scale = min((width - 24) / span[0], (drawing_height - 14) / span[1])
    scene_center = (flat.min(axis=0) + flat.max(axis=0)) * 0.5

    def place(values: np.ndarray) -> np.ndarray:
        result = (values - scene_center) * scale
        result[..., 0] += width * 0.5
        result[..., 1] += drawing_height * 0.5 + 2.0
        return result

    projected = place(projected)
    projected_floor = place(projected_floor)
    projected_wall = place(projected_wall)

    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#111827"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#9a7548"), 0.8))
    painter.setBrush(QColor(227, 201, 168, 70))
    painter.drawPolygon(QPolygonF([QPointF(float(x), float(y)) for x, y in projected_wall]))
    painter.setPen(QPen(QColor("#7d8590"), 0.8))
    painter.setBrush(QColor(201, 209, 217, 62))
    painter.drawPolygon(QPolygonF([QPointF(float(x), float(y)) for x, y in projected_floor]))
    seam = projected_wall[:2]
    painter.setPen(QPen(QColor("#cbd5e1"), 1.5))
    painter.drawLine(QPointF(*seam[0]), QPointF(*seam[1]))
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
    _draw_coordinate_axes(painter, width, height, view_matrix)
    painter.setPen(QPen(QColor("#94a3b8"), 1.0))
    painter.drawText(8, height - 8, caption)
    painter.end()
    return pixmap
