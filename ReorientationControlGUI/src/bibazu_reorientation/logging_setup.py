from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bibazu_reorientation.settings import default_run_directory


def configure_logging() -> Path:
    directory = default_run_directory().parent / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "bibazu-reorientation.log"
    handler = RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("bibazu_reorientation")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
    return target
