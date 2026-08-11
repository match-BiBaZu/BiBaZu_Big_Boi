from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from bibazu_reorientation.models import CycleResult, PartDefinition, PressureProfile
from bibazu_reorientation.settings import default_run_directory


class RunJournal:
    SCHEMA_VERSION = 1

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_run_directory()).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.directory / "cycles.csv"

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def begin(
        self,
        cycle_id: str,
        part: PartDefinition,
        profile: PressureProfile,
        source_profiles: tuple[PressureProfile, ...] | None = None,
    ) -> Path:
        session = self.directory / cycle_id
        session.mkdir(parents=False, exist_ok=False)
        if part.source_path:
            shutil.copy2(part.source_path, session / "part.yaml")
        profiles = source_profiles or (profile,)
        for index, source_profile in enumerate(profiles, start=1):
            suffix = "" if index == 1 else f"-{index}"
            shutil.copy2(source_profile.source_path, session / f"pressure-profile{suffix}.json")
        return session

    def save_decision_image(self, session: Path, image: np.ndarray) -> Path:
        target = session / "decision.png"
        temporary = session / ".decision.png.tmp"
        ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not ok:
            raise OSError("PNG could not be encoded")
        with temporary.open("wb") as handle:
            handle.write(encoded.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target

    def finish(self, result: CycleResult, details: dict[str, Any]) -> None:
        row = {
            "schema_version": self.SCHEMA_VERSION,
            **asdict(result),
            **details,
        }
        for key, value in tuple(row.items()):
            if isinstance(value, datetime):
                row[key] = value.isoformat()
            elif hasattr(value, "value"):
                row[key] = value.value
            elif isinstance(value, (dict, list, tuple)):
                row[key] = json.dumps(value, ensure_ascii=False)
        exists = self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
