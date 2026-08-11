from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from bibazu_reorientation.mesh_preview import (
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
