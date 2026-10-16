from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from bibazu_reorientation.config import TransitionResolver, save_roadmap_part_definition
from bibazu_reorientation.orientation_animation import rotate, preview_frame
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog


def payload():
    a=[0.,0.,0.,1.]; b1=list(rotate(a,'y',90)); b2=list(rotate(a,'x',180)); c=list(rotate(b2,'x',90))
    states=[dict(member_id=k,group_id=g,retained=True,quaternion_xyzw=q)
            for k,g,q in [('a',0,a),('b1',1,b1),('b2',1,b2),('c',2,c)]]
    keys={str(i):f'Test:group:{i}:membership{i}' for i in range(3)}
    choices=[]
    for key,source,target,start,end,axis,source_member,target_member in [
        ('ab',0,1,a,b1,'y','a','b1'),('bc',1,2,b2,c,'x','b2','c')]:
        command=dict(axis=axis,signed_angle_deg=90.,start_quaternion_xyzw=start,end_quaternion_xyzw=end)
        choices.append(dict(edge_id=key,source=source,target=target,directed=True,
            transition_kind='group_transfer',actuation='group_reorientation',commands=[command],
            actuation_count=1,preference_cost=[0,0,1,90.,0],display_name=f'{axis.upper()} +90°',
            source_group_key=keys[str(source)],target_group_key=keys[str(target)],
            source_member_ids=[source_member],possible_target_member_ids=[target_member],
            source_group_member_ids=[s['member_id'] for s in states if s['group_id']==source],
            preview_source_member_id=source_member,preview_target_member_id=target_member,
            preview_segments=[dict(command,segment_kind='command')],evidence_status='geometry_only'))
    route=dict(route_id='group-route:0->2:profiles',source=0,target=2,group_semantics=True,
        profile_edge_ids=['ab','bc'],priority=[0,0,0,2,180.,0,1],tie_breaker='route',
        steps=[dict(edge_id=e['edge_id'],source=e['source'],target=e['target'],
                    required_source_group_key=e['source_group_key']) for e in choices])
    return dict(schema_version=1,part_name='Test',nodes=[dict(node_id=g,pose_ids=[g],kind='robust',
        source_pose_ids=[k],representative_quaternion_xyzw=q) for g,k,q in [(0,'a',a),(1,'b1',b1),(2,'c',c)]],
        edges=[],member_states=states,consumer_export=dict(version=2,group_keys=keys,
        calibration_choices=choices,execution_routes=[route]))


def save(tmp_path,data):
    path=tmp_path/'Test_roadmap.json';path.write_text(json.dumps(data),encoding='utf-8');return path


def test_group_runtime_requires_explicit_group_qualification_and_permits_member_change(tmp_path):
    data=payload();path=save(tmp_path,data)
    model,mesh,profile=(tmp_path/name for name in ('model.pt','part.stl','p.json'))
    model.write_bytes(b'model');mesh.write_bytes(b'mesh');profile.write_text(json.dumps(dict(version=1,arrays=[])))
    kwargs=dict(roadmap_path=path,part_name='Test',model_path=model,mesh_path=mesh,
        pose_class_mapping={0:0,1:1,2:2},target_pose=2,transition_profiles={'ab':profile,'bc':profile})
    definition=save_roadmap_part_definition(tmp_path/'part.yaml',**kwargs,
        transition_source_coverage={'ab':('a',),'bc':('b1','b2')})
    with pytest.raises(ValueError,match='coverage'):
        TransitionResolver(definition).plan(0)
    definition=save_roadmap_part_definition(tmp_path/'part.yaml',**kwargs,
        transition_group_coverage={'ab':data['consumer_export']['group_keys']['0'],
                                  'bc':data['consumer_export']['group_keys']['1']})
    assert [e.edge_id for e in TransitionResolver(definition).plan(0)]==['ab','bc']
    assert definition.transitions[0].member_context['possible_target_member_ids']==['b1']
    assert definition.transitions[1].member_context['source_member_ids']==['b2']
    assert not any(e.covered_source_member_ids for e in definition.transitions)
    with pytest.raises(ValueError,match='Group calibration'):
        save_roadmap_part_definition(tmp_path/'wrong.yaml',**kwargs,transition_group_coverage={'ab':'different catalogue group'})


def test_version_two_rejects_wrong_group_requirements(tmp_path):
    data=payload();data['consumer_export']['execution_routes'][0]['steps'][1]['required_source_group_key']='wrong'
    with pytest.raises(ValueError,match='group coverage'):
        load_pose_roadmap(save(tmp_path,data))


def test_setup_uses_group_checkboxes_and_rotation_names(tmp_path,qtbot):
    dialog=RoadmapSetupDialog();qtbot.addWidget(dialog);dialog._load_roadmap(save(tmp_path,payload()))
    assert not dialog.coverage_inputs
    assert set(dialog.group_coverage_inputs)=={'ab','bc'}
    assert dialog.profile_table.item(0,1).text()=='Y +90°'
    assert all(not widget.isChecked() for widget in dialog.group_coverage_inputs.values())


def test_pressure_group_profile_roundtrip_and_actual_start_member(tmp_path,qtbot,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'CSVSaver'))
    from roadmap_transition_dialog import (load_roadmap_document,RoadmapTransitionDialog,
        roadmap_transition_metadata,selection_from_roadmap_transition_metadata)
    path=save(tmp_path,payload());document=load_roadmap_document(path)
    chooser=RoadmapTransitionDialog(document);qtbot.addWidget(chooser)
    chooser._set_pose_selection(1,2);chooser.transition_table.selectRow(0)
    assert not chooser.coverage_edit.isVisible()
    chooser.group_coverage_checkbox.setChecked(True);chooser._accept_selected_transition()
    selection=chooser.selected_transition
    expected=next(s['quaternion_xyzw'] for s in payload()['member_states'] if s['member_id']=='b2')
    assert selection.source_pose.quaternion_xyzw==tuple(expected)
    metadata=roadmap_transition_metadata(selection);path.unlink()
    restored=selection_from_roadmap_transition_metadata(metadata)
    assert restored.transition.calibrated_source_group_key==selection.transition.source_group_key
    assert restored.transition.display_label=='X +90°'
    assert preview_frame(restored.transition.preview_segments,0)==pytest.approx(expected)
    metadata['calibrated_source_group_key']='another source group'
    assert selection_from_roadmap_transition_metadata(metadata) is None
