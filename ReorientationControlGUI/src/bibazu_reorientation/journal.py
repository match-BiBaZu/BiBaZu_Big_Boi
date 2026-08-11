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

    def begin_batch(
        self,
        run_id: str,
        part: PartDefinition,
        profiles: tuple[PressureProfile, ...],
    ) -> Path:
        session = self.directory / run_id
        session.mkdir(parents=False, exist_ok=False)
        (session / "parts").mkdir()
        config_hash = ""
        if part.source_path:
            config_hash = self.sha256(part.source_path)
            shutil.copy2(part.source_path, session / f"part{part.source_path.suffix}")
        roadmap_hash = ""
        if part.roadmap_path and part.roadmap_path.exists():
            roadmap_hash = self.sha256(part.roadmap_path)
            shutil.copy2(
                part.roadmap_path,
                session / f"roadmap{part.roadmap_path.suffix}",
            )
        copied: set[Path] = set()
        profile_metadata = []
        for index, profile in enumerate(profiles, start=1):
            source = profile.source_path.resolve()
            if source in copied:
                continue
            copied.add(source)
            copied_name = f"pressure-profile-{index:02d}.json"
            shutil.copy2(source, session / copied_name)
            profile_metadata.append(
                {
                    "source_path": str(source),
                    "sha256": profile.sha256 or self.sha256(source),
                    "session_copy": copied_name,
                }
            )
        metadata = {
            "schema_version": 2,
            "run_id": run_id,
            "part_name": part.part_name,
            "target_pose": part.target_pose,
            "config_path": str(part.source_path or ""),
            "config_sha256": config_hash,
            "roadmap_path": str(part.roadmap_path or ""),
            "roadmap_sha256": roadmap_hash,
            "model_path": str(part.model_path),
            "model_sha256": self.sha256(part.model_path),
            "mesh_path": str(part.mesh_path or ""),
            "profiles": profile_metadata,
            "started_at": datetime.now().astimezone().isoformat(),
        }
        (session / "run.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return session

    def finish_batch(self, session: Path, *, state: str, detail: str = "") -> None:
        target = session / "result.json"
        temporary = session / ".result.json.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "state": state,
                    "detail": detail,
                    "finished_at": datetime.now().astimezone().isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def save_part_image(self, session: Path, sequence_id: int, image: np.ndarray) -> Path:
        target = session / "parts" / f"part-{sequence_id:08d}.png"
        temporary = target.with_name(f".{target.name}.tmp")
        ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not ok:
            raise OSError("Part PNG could not be encoded")
        with temporary.open("wb") as handle:
            handle.write(encoded.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target

    def append_part(self, session: Path, row: dict[str, Any]) -> None:
        target = session / "parts.csv"
        normalized = {"schema_version": 2, **row}
        for key, value in tuple(normalized.items()):
            if isinstance(value, datetime):
                normalized[key] = value.isoformat()
            elif hasattr(value, "value"):
                normalized[key] = value.value
            elif isinstance(value, (dict, list, tuple)):
                normalized[key] = json.dumps(value, ensure_ascii=False)
        exists = target.exists()
        with target.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(normalized))
            if not exists:
                writer.writeheader()
            writer.writerow(normalized)
            handle.flush()
            os.fsync(handle.fileno())

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
