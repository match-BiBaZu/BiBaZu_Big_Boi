from __future__ import annotations

from pathlib import Path

from PIL import Image

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICON_NAMES = ("reorientation", "pressure", "conveyor", "capture")


def build_icon(source: Path, destination: Path) -> None:
    with Image.open(source) as image:
        rgba = image.convert("RGBA")
        if rgba.width != rgba.height:
            raise ValueError(f"Icon source must be square: {source}")
        alpha = rgba.getchannel("A")
        if alpha.getextrema() != (0, 255):
            raise ValueError(f"Icon source needs transparent and opaque pixels: {source}")
        rgba.save(
            destination,
            format="ICO",
            sizes=[(size, size) for size in ICON_SIZES],
            bitmap_format="png",
        )


def main() -> None:
    directory = Path(__file__).resolve().parent
    for name in ICON_NAMES:
        source = directory / f"{name}.png"
        destination = directory / f"{name}.ico"
        build_icon(source, destination)
        print(f"Built {destination.name}")


if __name__ == "__main__":
    main()
