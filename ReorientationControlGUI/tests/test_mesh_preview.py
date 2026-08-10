from __future__ import annotations

import struct
from pathlib import Path

from bibazu_reorientation.mesh_preview import load_mesh_triangles, render_mesh_preview


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
