import copy
import json

from PyQt6.QtWidgets import QApplication

from roadmap_transition_dialog import (
    RoadmapTransitionDialog,
    load_roadmap_document,
    roadmap_transition_metadata,
    selection_from_roadmap_transition_metadata,
)
from orientation_animation import rotate


def payload():
    q = [0.0, 0.0, 0.0, 1.0]
    target = list(rotate(q, "y", 45))
    edge = dict(
        edge_id="member:0->1:test",
        source=0,
        target=1,
        directed=True,
        evidence_status="geometry_only",
        source_member_ids=["a1"],
        possible_target_member_ids=["b"],
        transfer_model="known_member_endpoint",
        actuation="free_y",
        actuation_count=1,
        total_absolute_angle_deg=45.0,
        capture_interval_deg=[44, 46],
        commands=[
            dict(
                axis="y", signed_angle_deg=45.0, start_quaternion_xyzw=q, end_quaternion_xyzw=target
            )
        ],
    )
    return dict(
        schema_version=1,
        part_name="Test",
        nodes=[
            dict(
                node_id=g,
                kind="robust",
                pose_ids=[g],
                source_pose_ids=[key],
                representative_quaternion_xyzw=quat,
            )
            for g, key, quat in [(0, "a1", q), (1, "b", target)]
        ],
        edges=[],
        member_states=[
            dict(member_id=k, group_id=g, retained=True, quaternion_xyzw=quat)
            for k, g, quat in [("a1", 0, q), ("a2", 0, list(rotate(q, "y", 5))), ("b", 1, target)]
        ],
        member_transfers=[edge],
        member_routes=[],
    )


def test_member_selection_coverage_and_missing_roadmap_roundtrip(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "Test_roadmap.json"
    path.write_text(json.dumps(payload()))
    doc = load_roadmap_document(path)
    chooser = RoadmapTransitionDialog(doc)
    chooser._set_pose_selection(0, 1)
    chooser.transition_table.selectRow(0)
    assert "From a1" in chooser._step_summary(doc.transitions[0])
    assert "capture" in chooser._step_summary(doc.transitions[0])
    chooser.coverage_edit.setText("*")
    chooser._accept_selected_transition()
    selected = chooser.selected_transition
    assert set(selected.transition.covered_source_member_ids) == {"a1", "a2"}
    metadata = roadmap_transition_metadata(selected)
    path.unlink()
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert restored.transition.member_context == selected.transition.member_context
    assert (
        restored.transition.covered_source_member_ids
        == selected.transition.covered_source_member_ids
    )
    assert restored.target_pose.quaternion_xyzw == selected.target_pose.quaternion_xyzw
    chooser.close()
    app.processEvents()


def test_same_group_repositioning_is_selectable(tmp_path):
    app = QApplication.instance() or QApplication([])
    data = payload()
    edge = copy.deepcopy(data["member_transfers"][0])
    edge.update(edge_id="member:0->0:test", target=0, possible_target_member_ids=["a2"])
    data["member_transfers"] = [edge]
    path = tmp_path / "Test_roadmap.json"
    path.write_text(json.dumps(data))
    chooser = RoadmapTransitionDialog(load_roadmap_document(path))
    chooser._pose_clicked(0)
    chooser._pose_clicked(0)
    assert chooser.start_pose_id == chooser.end_pose_id == 0
    assert chooser.transition_table.rowCount() == 1
    chooser.close()
    app.processEvents()
