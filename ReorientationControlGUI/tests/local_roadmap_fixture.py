"""Historical fixture already in this repo; production reads the shared roadmap repo."""

from pathlib import Path


def fixture_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "all_1_roadmaps" / "Df1a_roadmap_verified"
