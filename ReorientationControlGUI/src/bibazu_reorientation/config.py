from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bibazu_reorientation.models import PartDefinition, PoseDefinition, TransitionSpec

SCHEMA_VERSION = 1


def _resolve(base: Path, value: Any, field: str, suffix: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} darf nicht leer sein")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.suffix.lower() != suffix:
        raise ValueError(f"{field} muss auf eine {suffix}-Datei zeigen")
    if not path.is_file():
        raise ValueError(f"Datei nicht gefunden: {path}")
    return path


def _resolve_mesh(base: Path, value: Any) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("mesh_path muss ein Dateipfad sein")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.suffix.lower() not in {".stl", ".obj"}:
        raise ValueError("mesh_path muss auf eine STL- oder OBJ-Datei zeigen")
    if not path.is_file():
        raise ValueError(f"3D-Modell nicht gefunden: {path}")
    return path


def load_part_definition(path: Path) -> PartDefinition:
    source = Path(path).expanduser().resolve()
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Ungültiges YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Die Bauteildatei muss ein YAML-Objekt enthalten")
    version = int(payload.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unbekannte Bauteil-Schemaversion: {version}")
    part_name = str(payload.get("part_name", "")).strip()
    if not part_name:
        raise ValueError("part_name darf nicht leer sein")

    poses_raw = payload.get("poses")
    if not isinstance(poses_raw, list) or len(poses_raw) != 2:
        raise ValueError("V1 benötigt genau zwei Posen")
    poses = tuple(
        PoseDefinition(
            id=int(item["id"]),
            label=str(item["label"]),
            model_class_id=int(item["model_class_id"]),
        )
        for item in poses_raw
    )
    if {pose.id for pose in poses} != {1, 2}:
        raise ValueError("V1 benötigt die Pose-IDs 1 und 2")
    if {pose.model_class_id for pose in poses} != {0, 1}:
        raise ValueError("V1 benötigt die YOLO-Klassen 0 und 1")
    expected_labels = {0: "pose1", 1: "pose2"}
    for pose in poses:
        normalized = "".join(
            character for character in pose.label.casefold() if character.isalnum()
        )
        if normalized != expected_labels[pose.model_class_id]:
            raise ValueError("Klasse 0 muss 'Pose 1' und Klasse 1 muss 'Pose 2' heißen")

    target_pose = int(payload.get("target_pose", 0))
    if target_pose not in {1, 2}:
        raise ValueError("V1 unterstützt Pose 1 oder Pose 2 als Ziel")
    transitions_raw = payload.get("transitions")
    if not isinstance(transitions_raw, list) or len(transitions_raw) != 1:
        raise ValueError("V1 benötigt genau den Übergang Pose 2 nach Pose 1")
    transition_raw = transitions_raw[0]
    transition = TransitionSpec(
        from_pose=int(transition_raw["from_pose"]),
        to_pose=int(transition_raw["to_pose"]),
        pressure_profile=_resolve(
            source.parent,
            transition_raw.get("pressure_profile"),
            "pressure_profile",
            ".json",
        ),
    )
    expected_transition = (3 - target_pose, target_pose)
    if (transition.from_pose, transition.to_pose) != expected_transition:
        raise ValueError(
            f"V1 benötigt für Zielpose {target_pose} genau den Übergang "
            f"Pose {expected_transition[0]} nach Pose {target_pose}"
        )
    return PartDefinition(
        schema_version=version,
        part_name=part_name,
        model_path=_resolve(source.parent, payload.get("model_path"), "model_path", ".pt"),
        poses=poses,
        target_pose=target_pose,
        transitions=(transition,),
        mesh_path=_resolve_mesh(source.parent, payload.get("mesh_path")),
        source_path=source,
    )


def _portable_path(target: Path, config_path: Path) -> str:
    target = target.expanduser().resolve()
    try:
        return target.relative_to(config_path.parent.resolve()).as_posix()
    except ValueError:
        return str(target)


def save_part_definition(
    path: Path,
    *,
    part_name: str,
    model_path: Path,
    pressure_profile: Path,
    target_pose: int = 1,
    mesh_path: Path | None = None,
) -> PartDefinition:
    destination = Path(path).expanduser().resolve()
    if not part_name.strip():
        raise ValueError("Der Bauteilname darf nicht leer sein")
    model_path = Path(model_path).expanduser().resolve()
    pressure_profile = Path(pressure_profile).expanduser().resolve()
    if model_path.suffix.lower() != ".pt" or not model_path.is_file():
        raise ValueError(f"YOLO-Modell nicht gefunden: {model_path}")
    if pressure_profile.suffix.lower() != ".json" or not pressure_profile.is_file():
        raise ValueError(f"Pressure-Profil nicht gefunden: {pressure_profile}")
    if target_pose not in {1, 2}:
        raise ValueError("Die Zielpose muss Pose 1 oder Pose 2 sein")
    resolved_mesh: Path | None = None
    if mesh_path is not None and str(mesh_path).strip():
        resolved_mesh = Path(mesh_path).expanduser().resolve()
        if resolved_mesh.suffix.lower() not in {".stl", ".obj"} or not resolved_mesh.is_file():
            raise ValueError(f"3D-Modell nicht gefunden oder nicht unterstützt: {resolved_mesh}")
    source_pose = 3 - target_pose
    payload = {
        "schema_version": SCHEMA_VERSION,
        "part_name": part_name.strip(),
        "model_path": _portable_path(Path(model_path), destination),
        "poses": [
            {"id": 1, "label": "Pose 1", "model_class_id": 0},
            {"id": 2, "label": "Pose 2", "model_class_id": 1},
        ],
        "target_pose": target_pose,
        "transitions": [
            {
                "from_pose": source_pose,
                "to_pose": target_pose,
                "pressure_profile": _portable_path(Path(pressure_profile), destination),
            }
        ],
    }
    if resolved_mesh is not None:
        payload["mesh_path"] = _portable_path(resolved_mesh, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return load_part_definition(destination)


class TransitionResolver:
    def __init__(self, definition: PartDefinition) -> None:
        self.definition = definition

    def plan(self, start_pose: int, target_pose: int | None = None) -> tuple[TransitionSpec, ...]:
        target = self.definition.target_pose if target_pose is None else target_pose
        if start_pose == target:
            return ()
        direct = tuple(
            transition
            for transition in self.definition.transitions
            if transition.from_pose == start_pose and transition.to_pose == target
        )
        if len(direct) != 1:
            raise ValueError(f"Kein eindeutiger Übergang {start_pose} → {target} konfiguriert")
        return direct
