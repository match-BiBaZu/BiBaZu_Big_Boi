"""Bundled CAD and the shared local component-roadmap repository."""

import os
from pathlib import Path

from bibazu_reorientation.export_records import relocated_path


def geometry_directory() -> Path:
    return Path(__file__).resolve().parent / "assets" / "geometry"


def cad_directory() -> Path:
    return geometry_directory() / "cad"


def roadmap_directory() -> Path:
    override = os.environ.get("BIBAZU_ROADMAPS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "BiBaZu_StableComponentenRoadmaps_grouped"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[4] / "BiBaZu_StableComponentenRoadmaps_grouped"


def local_geometry_path(value: str | Path) -> Path:
    """Relocate an old geometry-checkout path only when its exact copy exists."""
    path = Path(value)
    parts = str(value).replace("\\", "/").split("/")
    if "bibazu_geometry_to_pose" not in parts:
        return path
    index = parts.index("bibazu_geometry_to_pose") + 1
    suffix = parts[index:]
    # Historical geometry roadmaps have different IDs; never guess a replacement.
    roots = {"Werkstücke_STL_grob": cad_directory()}
    if suffix and suffix[0] in roots:
        candidate = roots[suffix[0]].joinpath(*suffix[1:]).resolve()
        if candidate.is_relative_to(geometry_directory()) and candidate.exists():
            return candidate
    return path


def external_roadmap_path(value):
    return relocated_path(value, roadmap_directory())
