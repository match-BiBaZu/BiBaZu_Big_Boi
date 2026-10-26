from dataclasses import replace
import json

import pytest

from bibazu_reorientation.config import TransitionResolver
from bibazu_reorientation.models import PartDefinition, PoseDefinition, TransitionSpec
from bibazu_reorientation.roadmap import load_pose_roadmap
from test_individual_group_exports import payload as individual_payload, save


def payload():
    value = individual_payload()
    section = value['consumer_export']
    section['planning_semantics'] = 'pose_to_group'
    choice = section['calibration_choices'][0]
    choice.update(source_pose_specific=True, applicable_source_pose_ids=[0],
        source_group_id=0, target_group_id=1,
        witnesses=[dict(source_member_id='a', target_member_id='b1')])
    section['calibration_choices'] = [choice]
    section['execution_routes'] = [dict(route_id='ab:profile', source=0, target=1,
        source_group_key=choice['source_group_key'], target_group_key=choice['target_group_key'],
        source_pose_specific=True, applicable_source_pose_ids=[0], accepted_target_pose_ids=[1],
        group_semantics=True, profile_edge_ids=['ab'], priority=[0,1,90,0], tie_breaker='ab',
        steps=[dict(edge_id='ab', source=0, target=1, required_source_pose_ids=[0],
            possible_target_pose_ids=[1], required_source_group_key=choice['source_group_key'])])]
    return value


def test_exact_pose_options_are_not_inherited(tmp_path):
    roadmap = load_pose_roadmap(save(tmp_path,payload()))
    assert [e.edge_id for e in roadmap.route_options(0,1)] == ['ab']
    assert roadmap.route_options(3,1) == ()
    assert roadmap.route_options(0,3) == ()


@pytest.mark.parametrize('mutate', [
    lambda d:d['consumer_export']['calibration_choices'][0].update(applicable_source_pose_ids=[0,3]),
    lambda d:d['consumer_export']['calibration_choices'][0]['witnesses'][0].update(source_member_id='a2'),
    lambda d:d['consumer_export']['execution_routes'][0]['steps'][0].update(required_source_pose_ids=[3]),
])
def test_incorrect_source_applicability_rejected(tmp_path,mutate):
    data = payload(); mutate(data)
    with pytest.raises(ValueError): load_pose_roadmap(save(tmp_path,data))


def test_group_only_recognition_cannot_execute_an_exact_pose_choice(tmp_path):
    data = payload(); roadmap = load_pose_roadmap(save(tmp_path,data))
    edge, = roadmap.exported_choices
    transition = TransitionSpec(from_pose=0, to_pose=1, pressure_profile=tmp_path/'profile.json',
        edge_id=edge.edge_id, member_context=edge.member_context, source_group_key=edge.source_group_key,
        calibrated_source_group_key=edge.source_group_key)
    definition = PartDefinition(2,'Test',tmp_path/'model.pt',(),1,(transition,),
        execution_routes=roadmap.execution_routes, pose_group_keys=roadmap.pose_group_keys,
        planning_semantics='group_union')
    assert TransitionResolver(definition).plan(0) == (transition,)
    assert TransitionResolver(definition).plan(0,3) == ()
    # Two possible source members share the recognition class.
    poses = (PoseDefinition(0,'Pose 0',5), PoseDefinition(3,'Pose 3',5))
    with pytest.raises(ValueError,match='No path'):
        TransitionResolver(replace(definition,poses=poses)).plan(0)
    with pytest.raises(ValueError,match='No path'):
        TransitionResolver(definition).plan(3)


def test_pressure_picker_matches_source_pose_and_keeps_profile_metadata(tmp_path,qtbot,monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'CSVSaver'))
    from roadmap_transition_dialog import (load_roadmap_document, RoadmapTransitionDialog,
        roadmap_transition_metadata, selection_from_roadmap_transition_metadata)
    document = load_roadmap_document(save(tmp_path,payload()))
    assert [e.edge_id for e in document.route_options(0,1)] == ['ab']
    assert document.route_options(3,1) == ()
    picker = RoadmapTransitionDialog(document); qtbot.addWidget(picker)
    picker._set_pose_selection(3,1)
    assert picker.transition_table.rowCount() == 0
    picker._set_pose_selection(0,1); picker.transition_table.selectRow(0)
    picker.group_coverage_checkbox.setChecked(True); picker._accept_selected_transition()
    metadata = roadmap_transition_metadata(picker.selected_transition)
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert restored.transition.member_context['applicable_source_pose_ids'] == [0]


def test_reimport_retires_group_shared_assignments_without_granting_exact_pose_coverage(tmp_path):
    from bibazu_reorientation.config import save_roadmap_part_definition, load_part_definition
    old = individual_payload(); path = save(tmp_path,old)
    model, mesh, profile = (tmp_path/name for name in ('model.pt','mesh.stl','profile.json'))
    model.write_bytes(b'model'); mesh.write_bytes(b'mesh')
    profile.write_text(json.dumps(dict(version=1, arrays=[])), encoding='utf-8')
    config = tmp_path/'part.yaml'
    save_roadmap_part_definition(config,roadmap_path=path,part_name='Test',model_path=model,mesh_path=mesh,
        pose_class_mapping={0:5,3:5,1:6,2:7},target_pose=1,transition_profiles={'ab':profile},
        transition_group_coverage={'ab':old['consumer_export']['group_keys']['0']})
    revised = payload(); section = revised['consumer_export']
    section['calibration_choices'][0]['edge_id'] = 'exact-ab'
    route = section['execution_routes'][0]
    route.update(route_id='exact-ab:profile',profile_edge_ids=['exact-ab'],tie_breaker='exact-ab')
    route['steps'][0]['edge_id'] = 'exact-ab'
    save(tmp_path,revised)
    with pytest.raises(ValueError): load_part_definition(config)
    definition = load_part_definition(config,accept_roadmap_change=True)
    assert definition.planning_semantics == 'pose_to_group'
    assert dict(definition.unavailable_transition_profiles)['ab'] == profile
    assert all(e.pressure_profile is None for e in definition.transitions)
    with pytest.raises(ValueError,match='No path'): TransitionResolver(definition).plan(0)
