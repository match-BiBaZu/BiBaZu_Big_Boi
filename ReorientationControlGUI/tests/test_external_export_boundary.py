import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from bibazu_reorientation.export_records import relocated_path, with_consumer_sidecar
from bibazu_reorientation.resources import roadmap_directory


def test_unlisted_saved_route_assignment_survives_until_offline_upgrade(tmp_path):
    import yaml
    from bibazu_reorientation.config import (
        TransitionResolver,
        load_part_definition,
        save_roadmap_part_definition,
    )

    roadmap = tmp_path / "part_roadmap.json"
    data = dict(
        schema_version=1,
        nodes=[
            dict(node_id=i, kind="robust", representative_quaternion_xyzw=[0, 0, 0, 1])
            for i in range(3)
        ],
        edges=[
            dict(
                edge_id=f"a{i}",
                source=i,
                target=i + 1,
                directed=True,
                transition_kind="actuated",
                actuation="free_y",
                signed_angle_deg=10,
            )
            for i in range(2)
        ],
    )
    roadmap.write_text(json.dumps(data))
    model, mesh, profile = (tmp_path / name for name in ("model.pt", "mesh.stl", "profile.json"))
    model.write_bytes(b"model")
    mesh.write_bytes(b"mesh")
    profile.write_text(json.dumps(dict(version=1, arrays=[])))
    path = tmp_path / "part.yaml"
    macro_id = "multi2:0->1->2"
    config = dict(
        schema_version=2,
        part_name="part",
        model_path=str(model),
        mesh_path=str(mesh),
        roadmap_path=str(roadmap),
        roadmap_sha256=hashlib.sha256(roadmap.read_bytes()).hexdigest(),
        poses=[dict(id=i, model_class_id=i) for i in range(3)],
        target_pose=2,
        transition_profiles={"a0": None, "a1": None, macro_id: str(profile)},
    )
    path.write_text(yaml.safe_dump(config))
    definition = load_part_definition(path)
    assert definition.unavailable_transition_profiles == ((macro_id, profile),)
    with pytest.raises(ValueError, match="upgrade.*offline"):
        TransitionResolver(definition).plan(0)
    saved = save_roadmap_part_definition(
        tmp_path / "resaved.yaml",
        roadmap_path=roadmap,
        part_name="part",
        mesh_path=mesh,
        model_path=model,
        pose_class_mapping={0: 0, 1: 1, 2: 2},
        target_pose=2,
        transition_profiles={"a0": None, "a1": None},
        unavailable_transition_profiles=dict(definition.unavailable_transition_profiles),
    )
    assert saved.unavailable_transition_profiles == definition.unavailable_transition_profiles
    section = dict(
        version=1,
        calibration_choices=[
            dict(
                edge_id=macro_id,
                source=0,
                target=2,
                directed=True,
                transition_kind="multi_reorientation",
                actuation="multiple_reorientation_2",
                actuation_count=2,
                commands=[],
                component_edge_ids=["a0", "a1"],
                evidence_status="",
            )
        ],
        execution_routes=[
            dict(
                route_id="saved:profile",
                source=0,
                target=2,
                profile_edge_ids=[macro_id],
                priority=[1, 1],
                steps=[
                    dict(
                        edge_id=macro_id,
                        source=0,
                        target=2,
                        required_source_member_ids=[],
                        possible_target_member_ids=[],
                    )
                ],
            )
        ],
    )
    roadmap.with_name(roadmap.name + ".consumer.json").write_text(
        json.dumps(dict(roadmap_sha256=config["roadmap_sha256"], consumer_export=section))
    )
    restored = load_part_definition(path)
    assert not restored.unavailable_transition_profiles
    assert TransitionResolver(restored).plan(0)[0].edge_id == macro_id


def test_migration_requires_exact_path_and_preserves_hash(tmp_path):
    root = tmp_path / "catalogue"
    legacy = root / "legacy_catalogue"
    legacy.mkdir(parents=True)
    external = legacy / "original.json"
    external.write_text("{}")
    old = tmp_path / "old_location/roadmap.json"
    manifest = dict(
        records=[
            dict(
                old_path=str(old),
                relative_path="legacy_catalogue/original.json",
                sha256=hashlib.sha256(external.read_bytes()).hexdigest(),
            )
        ]
    )
    (legacy / "path_migrations.json").write_text(json.dumps(manifest))
    assert relocated_path(old, root) == external
    other = tmp_path / "different_location/roadmap.json"
    assert relocated_path(other, root) == other
    external.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="hash differs"):
        relocated_path(old, root)


def test_historical_sidecar_is_bound_to_exact_file_bytes(tmp_path):
    path = tmp_path / "roadmap.json"
    path.write_text("{}")
    sidecar = path.with_name(path.name + ".consumer.json")
    section = dict(version=1, calibration_choices=[], execution_routes=[])
    sidecar.write_text(
        json.dumps(
            dict(
                roadmap_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                consumer_export=section,
            )
        )
    )
    assert with_consumer_sidecar({}, path)["consumer_export"] == section
    path.write_text("{}\n")
    with pytest.raises(ValueError, match="sidecar hash differs"):
        with_consumer_sidecar({}, path)


@pytest.mark.parametrize("part,expected", [("Qk1a", 20), ("Dl1a", 34)])
def test_both_guis_load_finished_exports_without_generation_packages(part, expected):
    big = Path(__file__).resolve().parents[2]
    program = r"""
import importlib.abc
import importlib.util
import json
import os
import sys
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
class NoGenerator(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        generators = {'chute_pose', 'bibazu_stable_roadmaps', 'scipy', 'trimesh', 'networkx'}
        if fullname.split('.')[0] in generators:
            raise AssertionError('Control tried to import generation dependency: ' + fullname)
sys.meta_path.insert(0, NoGenerator())
big, catalogue, part, expected = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
sys.path[:0] = [str(big/'ReorientationControlGUI/src'), str(big/'CSVSaver')]
from PyQt6.QtWidgets import QApplication
from bibazu_reorientation.roadmap import load_pose_roadmap
from bibazu_reorientation.ui.transition_preview_dialog import TransitionPreviewDialog
from bibazu_reorientation.ui.roadmap_setup_dialog import RoadmapSetupDialog
from roadmap_transition_dialog import load_roadmap_document, RoadmapTransitionDialog
app=QApplication([])
path=catalogue/part/(part+'_roadmap.json')
data=json.loads(path.read_text(encoding='utf-8'))
if data['consumer_export']['version'] < 3:
    assert data['connectivity']['members']['known_member_inter_group_action_count']==expected
roadmap=load_pose_roadmap(path)
pressure=load_roadmap_document(path)
ids={r['edge_id'] for r in data['consumer_export']['calibration_choices']}
assert {e.edge_id for e in roadmap.calibratable_transitions}==ids
assert {e.edge_id for e in pressure.transitions if e.calibratable}==ids
edge=next(e for e in roadmap.visible_transitions if any(s.get('segment_kind')=='settling' for s in e.preview_segments))
preview=TransitionPreviewDialog(roadmap,edge)
preview._render_progress(750)
assert not preview.animation.pixmap().isNull()
preview.close()
setup=RoadmapSetupDialog()
setup._load_roadmap(path)
assert setup.profile_table.rowCount()==len(roadmap.visible_transitions)
setup.close()
chooser=RoadmapTransitionDialog(pressure)
chooser._set_pose_selection(edge.from_pose,edge.to_pose)
assert chooser.transition_table.rowCount()>0
chooser.close()
app.processEvents()
"""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            program,
            str(big),
            str(roadmap_directory()),
            part,
            str(expected),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
