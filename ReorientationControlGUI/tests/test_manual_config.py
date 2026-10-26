from __future__ import annotations

import csv
import json
import time
from dataclasses import replace

import pytest
import yaml
from test_batch_controller import FakePressure, advance_to_queue_staging, multi_frame

from bibazu_reorientation.batch import PartQueuePlanner, queue_record_values
from bibazu_reorientation.batch_controller import BatchController
from bibazu_reorientation.config import load_part_definition
from bibazu_reorientation.inference import overlay_labels
from bibazu_reorientation.journal import RunJournal
from bibazu_reorientation.manual_config import save_manual_definition
from bibazu_reorientation.models import (
    BatchState,
    PartDecision,
    PartDecisionCode,
    PartResult,
    PlcSnapshot,
)
from bibazu_reorientation.profiles import load_pressure_profile


def make_manual_part(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    paths = {}
    for class_id, array in ((0, 1), (7, 3)):
        path = tmp_path / f"class-{class_id}.json"
        path.write_text(
            json.dumps(
                {
                    "version": 9,
                    "conveyor_enabled": True,
                    "conveyor_speed_mm_per_sec": 100,
                    "conveyor_max_speed_mm_per_sec": 200,
                    "arrays": [
                        dict(
                            index=array,
                            enabled=True,
                            nozzles_enabled=[True],
                            pressure_mbar=2000 + class_id,
                            pulse_duration_ms=25,
                        )
                    ],
                }
            ),
            encoding="utf-8",
        )
        paths[class_id] = path
    return save_manual_definition(
        tmp_path / "manual.yaml",
        part_name="Manual part",
        model_path=model,
        class_labels={0: "Face down", 7: "On side", 42: "Target", 12: "Unassigned"},
        target_class_id=42,
        profiles_by_class=paths,
    )


@pytest.fixture
def manual_part(tmp_path):
    return make_manual_part(tmp_path)


def source_profiles(part):
    return {
        edge.edge_id: load_pressure_profile(edge.pressure_profile)
        for edge in part.transitions
        if edge.pressure_profile is not None
    }


def test_manual_roundtrip_needs_no_roadmap_or_geometry(manual_part):
    part = load_part_definition(manual_part.source_path)
    assert part == manual_part
    assert part.is_manual_configuration
    assert not part.is_roadmap_configuration
    assert part.roadmap_path is part.roadmap_sha256 is part.mesh_path is None
    assert part.recognition_mapping == {0: 0, 7: 7, 12: 12, 42: 42}
    assert part.recognition_label(7) == "On side"
    assert part.target_pose == 42
    assert len(part.transitions) == 2
    payload = yaml.safe_load(part.source_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["model_path"] == "model.pt"
    assert "roadmap_path" not in payload
    assert {row["pressure_profile"] for row in payload["transitions"]} == {
        "class-0.json",
        "class-7.json",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate_class",
        "duplicate_pose",
        "unknown_target",
        "duplicate_profile",
        "unknown_start",
        "wrong_target",
        "target_profile",
        "missing_profile",
        "negative_class",
    ],
)
def test_invalid_manual_configuration_is_rejected(manual_part, invalid):
    source = manual_part.source_path
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if invalid == "duplicate_class":
        payload["poses"][1]["model_class_id"] = 0
    elif invalid == "duplicate_pose":
        payload["poses"][1]["id"] = 0
    elif invalid == "unknown_target":
        payload["target_pose"] = 999
    elif invalid == "duplicate_profile":
        payload["transitions"].append(payload["transitions"][0])
    elif invalid == "unknown_start":
        payload["transitions"][0]["from_pose"] = 999
    elif invalid == "wrong_target":
        payload["transitions"][0]["to_pose"] = 12
    elif invalid == "target_profile":
        payload["transitions"][0]["from_pose"] = 42
    elif invalid == "missing_profile":
        payload["transitions"][0]["pressure_profile"] = "missing.json"
    else:
        payload["poses"][0]["model_class_id"] = -1
    source.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_part_definition(source)


def test_save_validates_before_overwriting_manual_configuration(manual_part):
    raw = manual_part.source_path.read_bytes()
    with pytest.raises(ValueError):
        save_manual_definition(
            manual_part.source_path,
            part_name="",
            model_path=manual_part.model_path,
            class_labels={0: "Start", 7: "Target"},
            target_class_id=7,
            profiles_by_class={},
        )
    assert manual_part.source_path.read_bytes() == raw


@pytest.mark.parametrize(
    ("pose_id", "code", "mask"),
    [
        (0, PartDecisionCode.ACTUATE, 1),
        (7, PartDecisionCode.ACTUATE, 4),
        (42, PartDecisionCode.TARGET, 0),
        (12, PartDecisionCode.BYPASS_UNCERTAIN, 0),
        (None, PartDecisionCode.BYPASS_UNCERTAIN, 0),
    ],
)
def test_each_start_pose_uses_only_its_assigned_profile(manual_part, pose_id, code, mask):
    profiles = source_profiles(manual_part)
    planner = PartQueuePlanner(manual_part, next(iter(profiles.values())), profiles)
    decision = PartDecision(1, pose_id, 0.9, (), (20, 20, 30, 30), time.time(), "confirmed")
    record = planner.build(1, decision)
    assert record.decision_code == code
    assert record.expected_array_mask == mask
    if mask:
        assert len(record.transition_edge_ids) == 1
    else:
        assert not any(array.enabled or array.nozzle_mask for array in record.arrays)


def test_manual_runtime_does_not_use_roadmap_resolver(manual_part, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Manual execution attempted roadmap route resolution")

    monkeypatch.setattr("bibazu_reorientation.batch.TransitionResolver", forbidden)
    profiles = source_profiles(manual_part)
    planner = PartQueuePlanner(manual_part, next(iter(profiles.values())), profiles)
    decision = PartDecision(1, 7, 0.9, (), (20, 20, 30, 30), time.time(), "confirmed")
    assert planner.build(1, decision).expected_array_mask == 4


def test_manual_mixed_pose_snapshot_preserves_queue_order_and_profile_provenance(
    manual_part, tmp_path, qapp
):
    pressure = FakePressure()
    controller = BatchController(pressure, RunJournal(tmp_path / "runs"))
    controller.set_manual_configuration(
        manual_part, source_profiles(manual_part), conveyor_speed_mm_per_sec=120.0
    )
    controller.set_model_ready(True)
    controller._on_snapshot(
        PlcSnapshot(
            connected=True,
            conveyor_velocity_only=True,
            calibration_valid=True,
        )
    )
    controller.accept_inference(multi_frame((70, 0), (30, 7), (90, 42), (50, 99)))
    assert controller.state == BatchState.READY
    controller.start_run()
    advance_to_queue_staging(controller, pressure)
    records = []
    controller.part_queued.connect(records.append)
    for sequence in range(1, 5):
        assert not any(values.get("MAIN.GuiConveyorEnabled") for _, values, _ in pressure.writes)
        controller._on_snapshot(replace(controller.snapshot, batch_enqueue_ack=sequence))
    assert [record.pose_id for record in records] == [7, None, 0, 42]
    assert [record.expected_array_mask for record in records] == [4, 0, 1, 0]
    assert pressure.writes[-1][0] == "batch_conveyor_start"
    metadata = json.loads((controller._session / "run.json").read_text(encoding="utf-8"))
    assert metadata["configuration_mode"] == "manual"
    assert metadata["roadmap_path"] == ""
    for record in records:
        controller._consume_result(PartResult(record.sequence_id, record.expected_array_mask, 0))
    with (controller._session / "parts.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert json.loads(rows[0]["profile_paths"]) == [
        str(manual_part.transitions[1].pressure_profile)
    ]
    assert json.loads(rows[1]["profile_paths"]) == []
    assert json.loads(rows[3]["profile_paths"]) == []
    controller._heartbeat_timer.stop()


def test_manual_mode_rejects_incompatible_global_profile_settings(manual_part, tmp_path, qapp):
    profiles = source_profiles(manual_part)
    key = next(iter(profiles))
    profiles[key] = replace(profiles[key], light_barrier_debounce_ms=123)
    controller = BatchController(FakePressure(), RunJournal(tmp_path / "runs"))
    with pytest.raises(ValueError, match="share the same"):
        controller.set_manual_configuration(manual_part, profiles, conveyor_speed_mm_per_sec=100.0)


def test_manual_profile_assignments_can_be_saved_as_a_draft(manual_part, tmp_path, qapp):
    part = save_manual_definition(
        tmp_path / "draft.yaml",
        part_name="draft",
        model_path=manual_part.model_path,
        class_labels={0: "Start", 7: "Target"},
        target_class_id=7,
        profiles_by_class={},
    )
    controller = BatchController(FakePressure(), RunJournal(tmp_path / "runs"))
    with pytest.raises(ValueError, match="at least one"):
        controller.set_manual_configuration(part, {}, conveyor_speed_mm_per_sec=100)


def test_legacy_one_based_manual_ids_keep_profiles_and_target_when_loaded(manual_part):
    source = manual_part.source_path
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    for pose in payload["poses"]:
        pose["id"] += 1
    payload["target_pose"] += 1
    for edge in payload["transitions"]:
        edge["from_pose"] += 1
        edge["to_pose"] += 1
    original = yaml.safe_dump(payload).encode("utf-8")
    source.write_bytes(original)
    restored = load_part_definition(source)
    assert restored == manual_part
    assert source.read_bytes() == original
    assert {edge.from_pose: edge.pressure_profile.name for edge in restored.transitions} == {
        0: "class-0.json", 7: "class-7.json"
    }


def test_zero_target_passes_without_firing_and_other_classes_keep_their_profiles(
    manual_part, tmp_path
):
    part = save_manual_definition(
        tmp_path / "zero-target.yaml",
        part_name="Zero target",
        model_path=manual_part.model_path,
        class_labels={0: "Pose 0", 7: "Pose 7"},
        target_class_id=0,
        profiles_by_class={7: manual_part.transitions[1].pressure_profile},
    )
    profiles = source_profiles(part)
    planner = PartQueuePlanner(part, next(iter(profiles.values())), profiles)
    for sequence, pose_id in enumerate((0, 7), 1):
        record = planner.build(
            sequence, PartDecision(sequence, pose_id, 0.9, (), (20, 20, 30, 30), time.time(), "")
        )
        assert record.target_pose == 0
        assert record.pose_id == pose_id
        assert record.expected_array_mask == (0 if pose_id == 0 else 4)
        assert record.decision_code == (
            PartDecisionCode.TARGET if pose_id == 0 else PartDecisionCode.ACTUATE
        )
        assert queue_record_values(record)["MAIN.GuiReorientationQueuePoseId"] == pose_id
    detection = multi_frame((70, 0)).detections[0]
    assert overlay_labels(detection, part.recognition_mapping)[0] == "Pose 0"
