import json
from dataclasses import replace
import pytest
from bibazu_reorientation.config import TransitionResolver
from bibazu_reorientation.models import PartDefinition, PoseDefinition, TransitionSpec
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.orientation_animation import command_frame, rotate

def transfer():
    start = [0.0, 0.0, 0.0, 1.0]
    end = list(rotate(start, "y", 90))
    return dict(
        edge_id="composite:0->1:test",
        source=0,
        target=1,
        directed=True,
        transition_kind="composite_candidate",
        evidence_status="geometry_only",
        actuation_count=1,
        commands=[
            dict(
                axis="y",
                signed_angle_deg=90.0,
                start_quaternion_xyzw=start,
                end_quaternion_xyzw=end,
            )
        ],
        terminal_quaternion_xyzw=end,
        terminal_symmetry_quaternion_xyzw=start,
    )


def test_load_explicit_composite_and_reject_nonfinite_command(tmp_path):
    candidate = transfer()
    poses = {0: [0., 0., 0., 1.], 1: candidate["terminal_quaternion_xyzw"]}
    data = dict(schema_version=1, nodes=[dict(node_id=i, kind="robust", representative_quaternion_xyzw=q) for i,q in poses.items()],
                edges=[], composite_transfers=[candidate])
    path=tmp_path/"Test_roadmap.json";path.write_text(json.dumps(data))
    edge=load_pose_roadmap(path).composite_transitions[0]
    assert edge.evidence_status == "geometry_only"
    assert command_frame(edge.commands, .5) == pytest.approx(rotate(poses[0], "y", 45))
    candidate["commands"][0]["signed_angle_deg"] = float("nan")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="angle"):
        load_pose_roadmap(path)


def test_only_exported_long_routes_can_execute(tmp_path):
    poses=tuple(PoseDefinition(i,str(i),i) for i in range(7))
    edges=tuple(TransitionSpec(i,i+1,tmp_path/"p.json",f"a{i}") for i in range(6))
    definition=PartDefinition(2,"Test",tmp_path/"m.pt",poses,6,edges)
    with pytest.raises(ValueError, match="upgrade.*offline"):
        TransitionResolver(definition).plan(0)
    record=dict(source=0,target=6,profile_edge_ids=[f"a{i}" for i in range(6)],
                steps=[dict(edge_id=f"a{i}") for i in range(6)],priority=[2,6])
    definition=replace(definition,execution_routes=(record,))
    assert TransitionResolver(definition).plan(0) == edges
    with pytest.raises(ValueError,match="No path"):
        TransitionResolver(definition).plan(0,max_transitions=3)
    missing=tuple(replace(e,pressure_profile=None) if i==3 else e for i,e in enumerate(edges))
    with pytest.raises(ValueError,match="profiles"):
        TransitionResolver(replace(definition,transitions=missing)).plan(0)
