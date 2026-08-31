from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from bibazu_reorientation.mesh_preview import (
    COORDINATE_AXIS_COLORS,
    _display_rotation_matrix,
    load_mesh_triangles,
    render_mesh_preview,
    render_triangles_preview,
    slerp_quaternion,
)


def _binary_stl(path: Path) -> None:
    header = bytes(80)
    triangle = struct.pack(
        "<12fH",
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0,
    )
    path.write_bytes(header + struct.pack("<I", 1) + triangle)


def test_binary_stl_is_rendered(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "part.STL"
    _binary_stl(source)

    assert load_mesh_triangles(source).shape == (1, 3, 3)
    preview = render_mesh_preview(source, 180, 120)
    assert preview.width() == 180
    assert preview.height() == 120
    rotated = render_mesh_preview(
        source,
        190,
        130,
        quaternion_xyzw=(0.0, 0.0, 1.0, 0.0),
        caption="CAD · Pose 9",
    )
    assert rotated.size().width() == 190


def test_obj_polygon_is_triangulated(tmp_path: Path) -> None:
    source = tmp_path / "part.obj"
    source.write_text(
        "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n",
        encoding="utf-8",
    )
    assert load_mesh_triangles(source).shape == (2, 3, 3)


def test_slerp_uses_normalized_shortest_path() -> None:
    start = (0.0, 0.0, 0.0, 1.0)
    end = (0.0, 0.0, 1.0, 0.0)

    assert np.allclose(slerp_quaternion(start, end, 0.0), start)
    assert np.allclose(slerp_quaternion(start, end, 1.0), end)
    midpoint = np.asarray(slerp_quaternion(start, end, 0.5))
    assert np.isclose(np.linalg.norm(midpoint), 1.0)
    assert np.allclose(slerp_quaternion(start, tuple(-np.asarray(start)), 0.5), start)


def test_loaded_triangles_can_be_reused_for_animation(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "part.stl"
    _binary_stl(source)
    triangles = load_mesh_triangles(source)

    first = render_triangles_preview(triangles, 200, 140)
    second = render_triangles_preview(
        triangles,
        200,
        140,
        quaternion_xyzw=(0.0, 0.0, 1.0, 0.0),
    )

    assert first.size() == second.size()
    assert not first.isNull()
    assert not second.isNull()


def test_pose_display_uses_requested_z_up_axis_arrangement() -> None:
    axes = np.eye(3)

    transformed = axes @ _display_rotation_matrix().T

    horizontal = 1.0 / np.sqrt(2.0)
    vertical = 1.0 / np.sqrt(6.0)
    depth = 1.0 / np.sqrt(3.0)
    assert np.allclose(
        transformed,
        (
            (-horizontal, -vertical, -depth),
            (horizontal, -vertical, -depth),
            (0.0, 2.0 * vertical, -depth),
        ),
        atol=1e-12,
    )

    # Qt screen Y increases downward: +X is therefore down-left/downhill,
    # +Y down-right, and +Z vertically upward, matching the roadmap plots.
    screen_directions = transformed[:, :2] * np.asarray((1.0, -1.0))
    assert screen_directions[0, 0] < 0.0 and screen_directions[0, 1] > 0.0
    assert screen_directions[1, 0] > 0.0 and screen_directions[1, 1] > 0.0
    assert np.isclose(screen_directions[2, 0], 0.0)
    assert screen_directions[2, 1] < 0.0


def test_pose_preview_contains_xyz_coordinate_triad(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "part.STL"
    _binary_stl(source)

    image = render_mesh_preview(source, 220, 160).toImage()
    colors = {
        image.pixelColor(x, y).name() for y in range(image.height()) for x in range(image.width())
    }

    assert set(COORDINATE_AXIS_COLORS.values()) <= colors
