import json
from dataclasses import replace
import pytest
from bibazu_reorientation.config import TransitionResolver, save_roadmap_part_definition
from bibazu_reorientation.models import PartDefinition, PoseDefinition, TransitionSpec
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.orientation_animation import rotate, preview_frame
from bibazu_reorientation.export_records import read_export

def payload():
    identity = [0.0, 0.0, 0.0, 1.0]
    end = list(rotate(identity, "y", 45))
    states = [
        dict(member_id=k, group_id=g, retained=True, quaternion_xyzw=q)
        for k, g, q in [
            ("a1", 0, identity),
            ("a2", 0, list(rotate(identity, "y", 5))),
            ("b", 1, end),
        ]
    ]
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
        capture_interval_deg=None,
        commands=[
            dict(
                axis="y",
                signed_angle_deg=45.0,
                start_quaternion_xyzw=identity,
                end_quaternion_xyzw=end,
            )
        ],
    )
    return dict(
        schema_version=1,
        nodes=[
            dict(
                node_id=g,
                kind="robust",
                pose_ids=[g],
                source_pose_ids=[key],
                representative_quaternion_xyzw=q,
            )
            for g, key, q in [(0, "a1", identity), (1, "b", end)]
        ],
        edges=[],
        member_states=states,
        member_transfers=[edge],
        member_routes=[],
    )

def test_profile_coverage_roundtrip_and_no_implicit_legacy_promotion(tmp_path):
    data = payload()
    path = tmp_path / "Test_roadmap.json"
    path.write_text(json.dumps(data))
    roadmap = load_pose_roadmap(path)
    edge = roadmap.member_transitions[0]
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    mesh = tmp_path / "part.stl"
    mesh.write_text("fixture")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(version=1, arrays=[])))
    kwargs = dict(
        roadmap_path=path,
        part_name="Test",
        mesh_path=mesh,
        model_path=model,
        pose_class_mapping={0: 0, 1: 1},
        target_pose=1,
        transition_profiles={edge.edge_id: profile},
    )
    result = save_roadmap_part_definition(tmp_path / "part.yaml", **kwargs)
    assert not result.transitions[0].covered_source_member_ids
    with pytest.raises(ValueError, match="coverage"):
        TransitionResolver(result).plan(0)
    result = save_roadmap_part_definition(
        tmp_path / "part.yaml", transition_source_coverage={edge.edge_id: ("a1", "a2")}, **kwargs
    )
    assert TransitionResolver(result).plan(0)
    with pytest.raises(ValueError, match="unknown"):
        save_roadmap_part_definition(
            tmp_path / "invalid.yaml",
            transition_source_coverage={edge.edge_id: ("missing",)},
            **kwargs,
        )


def test_exported_branch_coverage_is_required_for_every_step(tmp_path):
    states=tuple(dict(member_id=k,group_id=g,retained=True) for k,g in [("a",0),("b1",1),("b2",1),("c",2)])
    edges=(TransitionSpec(0,1,tmp_path/"p.json","first",covered_source_member_ids=("a",)),
           TransitionSpec(1,2,tmp_path/"p.json","second",covered_source_member_ids=("b1",)))
    route=dict(source=0,target=2,profile_edge_ids=["first","second"],priority=[2,2],
        steps=[dict(edge_id="first",required_source_member_ids=["a"]),
               dict(edge_id="second",required_source_member_ids=["b1","b2"])])
    definition=PartDefinition(2,"Test",tmp_path/"m.pt",tuple(PoseDefinition(i,str(i),i) for i in range(3)),
        2,edges,member_states=states,execution_routes=(route,))
    with pytest.raises(ValueError,match="coverage"):
        TransitionResolver(definition).plan(0)
    edges=(edges[0],replace(edges[1],covered_source_member_ids=("b1","b2")))
    assert TransitionResolver(replace(definition,transitions=edges)).plan(0)==edges


def test_old_member_routes_are_not_materialized_by_consumer(tmp_path):
    data=payload()
    data["member_routes"]=[dict(source_member_id="a2",target=1,component_edge_ids=["member:0->1:test"])]
    path=tmp_path/"Test_roadmap.json";path.write_text(json.dumps(data))
    roadmap=load_pose_roadmap(path)
    assert len(roadmap.member_transitions)==1
    assert not roadmap.multi_reorientation_transitions
    data["member_transfers"][0]["possible_target_member_ids"]=["unknown"]
    with pytest.raises(ValueError,match="unknown"):
        read_export(data)


def test_exported_settling_preview():
    landing=rotate([0,0,0,1],"y",45)
    segments=[dict(segment_kind="settling",start_quaternion_xyzw=landing,
        end_quaternion_xyzw=[0,0,0,1],rotation_vector_chute_rad=[0,-.7853981633974483,0])]
    assert preview_frame(segments,1)==pytest.approx([0,0,0,1])
