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
        raise ValueError(f"STL enthält keine lesbaren Dreiecke: {source}")

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
        raise ValueError(f"OBJ enthält keine lesbaren Dreiecke: {source}")

    raise ValueError(f"Nicht unterstütztes 3D-Format: {source.suffix}")


def render_mesh_preview(path: Path, width: int = 250, height: int = 175) -> QPixmap:
    triangles = load_mesh_triangles(path)
    points = triangles.reshape(-1, 3)
    center = (points.min(axis=0) + points.max(axis=0)) * 0.5
    centered = triangles - center

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
    painter.drawText(8, height - 8, "Zielorientierung · Modellansicht")
    painter.end()
    return pixmap
