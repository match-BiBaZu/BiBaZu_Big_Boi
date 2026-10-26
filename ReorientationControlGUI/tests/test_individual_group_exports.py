from copy import deepcopy
from dataclasses import replace
import json

import pytest

from bibazu_reorientation.config import TransitionResolver, save_roadmap_part_definition, load_part_definition
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.models import PartDefinition, TransitionSpec
from test_group_exports import payload as group_payload


def payload():
    value=group_payload()
    # Two displayed source members share an action witnessed only by member a.
    value['nodes'].append(dict(value['nodes'][0],node_id=3,source_pose_ids=['a2']))
    value['member_states'].append(dict(value['member_states'][0],member_id='a2'))
    groups=[dict(group_id=g,group_key=f'Test:group:{g}:membership{g}',pose_ids=ids,
                 planning_member_ids=members) for g,ids,members in
            [(0,[0,3],['a','a2']),(1,[1],['b1','b2']),(2,[2],['c'])]]
    section=value['consumer_export'];section.update(version=3,pose_groups=groups,
        same_group_target_satisfied=True,composite_fallbacks=False)
    section['group_keys']['3']=section['group_keys']['0']
    for choice in section['calibration_choices']:
        choice.update(applicable_source_pose_ids=groups[choice['source']]['pose_ids'],
            accepted_target_pose_ids=groups[choice['target']]['pose_ids'])
    for route in section['execution_routes']:
        route.update(source_group_key=section['group_keys'][str(route['source'])],
                     target_group_key=section['group_keys'][str(route['target'])])
    return value


def save(tmp_path,data):
    path=tmp_path/'Test_roadmap.json';path.write_text(json.dumps(data),encoding='utf-8');return path


def test_all_members_share_choices_and_same_group_has_no_motion(tmp_path):
    roadmap=load_pose_roadmap(save(tmp_path,payload()))
    assert [e.edge_id for e in roadmap.route_options(0,1)]==[e.edge_id for e in roadmap.route_options(3,1)]
    assert roadmap.route_options(0,3)==()
    definition=PartDefinition(2,'Test',tmp_path/'model.pt',(),3,(),pose_group_keys=roadmap.pose_group_keys)
    assert TransitionResolver(definition).plan(0)==()


@pytest.mark.parametrize('mutate',[
    lambda d:d['consumer_export']['calibration_choices'][0].update(is_composite=True),
    lambda d:d['consumer_export']['calibration_choices'][0].update(applicable_source_pose_ids=[0]),
    lambda d:d['consumer_export']['calibration_choices'][0].update(witnesses=[dict(source_member_id='below',target_member_id='b1')]),
])
def test_invalid_bridge_partial_applicability_and_hidden_witness_rejected(tmp_path,mutate):
    data=payload();mutate(data)
    with pytest.raises(ValueError):load_pose_roadmap(save(tmp_path,data))


def test_group_recognition_shared_classes_and_qualified_route_roundtrip(tmp_path):
    data=payload();path=save(tmp_path,data)
    model=tmp_path/'model.pt';model.write_bytes(b'model')
    mesh=tmp_path/'part.stl';mesh.write_bytes(b'mesh')
    profile=tmp_path/'p.json';profile.write_text(json.dumps(dict(version=1,arrays=[])))
    kwargs=dict(roadmap_path=path,part_name='Test',model_path=model,mesh_path=mesh,
        pose_class_mapping={0:5,3:5,1:6,2:7},target_pose=2,
        transition_profiles={'ab':profile,'bc':profile})
    unqualified=save_roadmap_part_definition(tmp_path/'config.yaml',**kwargs)
    with pytest.raises(ValueError,match='coverage'):TransitionResolver(unqualified).plan(3)
    definition=save_roadmap_part_definition(tmp_path/'config.yaml',**kwargs,
        transition_group_coverage={'ab':data['consumer_export']['group_keys']['0'],
                                   'bc':data['consumer_export']['group_keys']['1']})
    assert definition.recognition_mapping=={5:0,6:1,7:2}
    assert [e.edge_id for e in TransitionResolver(definition).plan(3)]==['ab','bc']
    assert load_part_definition(tmp_path/'config.yaml').pose_group_keys==definition.pose_group_keys
    assert '0, 3' in definition.recognition_label(0)


def test_missing_connectivity_is_reported_without_constructing_a_bridge(tmp_path):
    roadmap=load_pose_roadmap(save(tmp_path,payload()))
    definition=PartDefinition(2,'Test',tmp_path/'model.pt',(),2,(),pose_group_keys=roadmap.pose_group_keys)
    with pytest.raises(ValueError,match='No path'):TransitionResolver(definition).plan(3)


def test_explicit_migration_uses_group_keys_instead_of_colliding_numbers(tmp_path):
    import hashlib
    import yaml
    data=payload();keys=data['consumer_export']['group_keys']
    old_hash=hashlib.sha256(b'previous group export').hexdigest()
    data['migration']=dict(previous_roadmap_sha256=old_hash,
        previous_pose_groups={'0':keys['1'],'1':keys['0'],'2':keys['2'],'3':'omitted-group'})
    path=save(tmp_path,data)
    model=tmp_path/'model.pt';model.write_bytes(b'model')
    mesh=tmp_path/'part.stl';mesh.write_bytes(b'mesh')
    profile=tmp_path/'p.json';profile.write_text(json.dumps(dict(version=1,arrays=[])))
    config=dict(schema_version=2,part_name='Test',roadmap_path=str(path),roadmap_sha256=old_hash,
        model_path=str(model),mesh_path=str(mesh),target_pose=0,
        poses=[dict(id=i,label=f'Old group {i}',model_class_id=11+i) for i in range(4)],
        transition_profiles={'old-removed':str(profile)},transition_group_coverage={'old-removed':'omitted-group'})
    file=tmp_path/'legacy.yaml';file.write_text(yaml.safe_dump(config),encoding='utf-8')
    with pytest.raises(ValueError):load_part_definition(file)
    migrated=load_part_definition(file,accept_roadmap_change=True)
    assert migrated.target_pose==1
    assert {p.id:p.model_class_id for p in migrated.poses}=={0:12,3:12,1:11,2:13}
    assert migrated.unavailable_transition_profiles==(('old-removed',profile),)
    assert migrated.unavailable_transition_group_coverage==(('old-removed','omitted-group'),)
    assert migrated.recognition_mapping[14] < 0
    assert 'no roadmap route' in migrated.recognition_label(migrated.recognition_mapping[14])


def test_batch_same_group_target_disables_arrays_despite_an_active_profile(tmp_path):
    from test_batch import definition,profile,decision
    from bibazu_reorientation.batch import PartQueuePlanner
    from bibazu_reorientation.models import PartDecisionCode
    part=replace(definition(tmp_path),pose_group_keys=((1,'same'),(2,'same')))
    queued=PartQueuePlanner(part,profile(tmp_path)).build(1,decision(2))
    assert queued.decision_code==PartDecisionCode.TARGET
    assert queued.expected_array_mask==0
    assert not any(a.enabled or a.nozzle_mask for a in queued.arrays)


def test_pressure_picker_shares_choices_and_reports_same_group_target(tmp_path,qtbot,monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'CSVSaver'))
    from roadmap_transition_dialog import load_roadmap_document,RoadmapTransitionDialog
    document=load_roadmap_document(save(tmp_path,payload()))
    assert [e.edge_id for e in document.route_options(3,1)]==[e.edge_id for e in document.route_options(0,1)]
    picker=RoadmapTransitionDialog(document);qtbot.addWidget(picker)
    picker._set_pose_selection(3,0)
    assert picker.transition_table.rowCount()==0
    assert 'already satisfied' in picker.path_hint.text()


def test_single_cycle_same_group_target_has_zero_actuator_mask(tmp_path):
    import time
    from test_controller import configured_controller,inference_frame
    controller,pressure=configured_controller(tmp_path)
    controller.part=replace(controller.part,pose_group_keys=((1,'same'),(2,'same')))
    controller.start_cycle()
    started=time.time()
    for index in range(3):controller.accept_inference(inference_frame(2,started+index*.01))
    assert controller._plan.expected_array_mask==0
    assert not any(controller._plan.enables[f'MAIN.GuiArrayEnabled{i}'] for i in range(1,5))


def test_omitted_recognition_group_bypasses_without_sending_negative_pose_ids(tmp_path):
    from test_batch import definition,profile,decision
    from bibazu_reorientation.batch import PartQueuePlanner,queue_record_values
    from bibazu_reorientation.models import PartDecisionCode
    queued=PartQueuePlanner(definition(tmp_path),profile(tmp_path)).build(1,decision(-1))
    assert queued.decision_code==PartDecisionCode.BYPASS_UNCERTAIN
    assert queued.expected_array_mask==0
    assert 'omitted_group' in queued.reason
    assert queue_record_values(queued)['MAIN.GuiReorientationQueuePoseId']==0
