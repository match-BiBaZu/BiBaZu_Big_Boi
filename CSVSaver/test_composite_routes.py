import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from roadmap_transition_dialog import (RoadmapTransitionDialog, load_roadmap_document,
                                      roadmap_transition_metadata, selection_from_roadmap_transition_metadata)
from orientation_animation import rotate


def payload():
    identity = [0., 0., 0., 1.]
    terminal = list(rotate(identity, 'z', 90))
    return dict(schema_version=1, part_name='Test', nodes=[dict(node_id=i, pose_ids=[i], kind='robust', representative_quaternion_xyzw=q)
                                                         for i,q in [(0, identity), (1, terminal)]], edges=[],
                composite_transfers=[dict(edge_id='composite:0->1:abc', source=0, target=1, directed=True,
                    transition_kind='composite_candidate', evidence_status='geometry_only', actuation_count=1,
                    terminal_quaternion_xyzw=terminal, terminal_symmetry_quaternion_xyzw=identity,
                    commands=[dict(axis='z', signed_angle_deg=90., start_quaternion_xyzw=identity, end_quaternion_xyzw=terminal)])])


def test_composite_selection_preview_and_portable_metadata(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path/'Test_roadmap.json'
    path.write_text(json.dumps(payload()))
    document = load_roadmap_document(path)
    dialog = RoadmapTransitionDialog(document)
    dialog._set_pose_selection(0, 1)
    dialog.transition_table.selectRow(0)
    dialog._preview_command(500)
    assert 'Transient orientation' in dialog.command_preview.text()
    dialog._accept_selected_transition()
    selected = dialog.selected_transition
    metadata = roadmap_transition_metadata(selected)
    assert metadata['evidence_status'] == 'geometry_only'
    assert len(metadata['commands']) == 1
    assert selected.profile_name_stem.endswith('_abc')
    path.unlink()
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert restored.transition.commands == selected.transition.commands
    assert restored.transition.evidence_status == 'geometry_only'
    assert restored.transition.category_label.startswith('Composite candidate')
    dialog.close()
    app.processEvents()


def test_old_exports_do_not_create_routes(tmp_path):
    data = dict(schema_version=1, part_name="Test", nodes=[dict(node_id=i, pose_ids=[i], kind="robust") for i in range(7)],
        edges=[dict(edge_id=f"a{i}", source=i, target=i+1, transition_kind="actuated", actuation="free_y", signed_angle_deg=30.) for i in range(6)])
    path = tmp_path / "Test_roadmap.json"
    path.write_text(json.dumps(data))
    assert load_roadmap_document(path).route_options(0, 6) == ()
    record = dict(edge_id="multi6:0->1->2->3->4->5->6", source=0, target=6,
        transition_kind="multi_reorientation", actuation="multiple_reorientation_6", actuation_count=6,
        component_edge_ids=[f"a{i}" for i in range(6)], commands=[])
    data["consumer_export"] = dict(version=1, calibration_choices=[record], execution_routes=[])
    path.write_text(json.dumps(data))
    route, = load_roadmap_document(path).route_options(0, 6)
    assert route.edge_id == record["edge_id"]
    assert load_roadmap_document(path).route_options(6, 0) == ()


def test_broken_candidate_is_rejected(tmp_path):
    data = payload()
    data['composite_transfers'][0]['commands'][0]['signed_angle_deg'] = float('nan')
    path = tmp_path/'Test_roadmap.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='angle'):
        load_roadmap_document(path)
