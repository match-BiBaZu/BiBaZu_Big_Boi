"""Regression coverage for restoring calibration context from pressure profiles."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_pressure_control_gui as existing_tests

gui = existing_tests.gui
from PyQt6.QtWidgets import QApplication, QFileDialog


class ProfilePoseContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def multi_flip_payload():
        payload = existing_tests.RoadmapTransitionGuiTests.roadmap_payload()
        for source, target, action, angle in (
            (2, 4, "free_z", -45.0), (4, 5, "wall_main_pos_x", 60.0)
        ):
            payload["nodes"].append({
                "node_id": target, "pose_ids": [target], "kind": "robust",
                "floor_contact_topology": "face", "wall_contact_topology": "face",
            })
            payload["edges"].append({
                "edge_id": f"step:{source}->{target}:{action}",
                "source": source, "target": target, "transition_kind": "actuated",
                "actuation": action, "signed_angle_deg": angle,
            })
        return payload

    def select_multi_flip_path(self, document, target):
        chooser = gui.RoadmapTransitionDialog(document)
        chooser._set_pose_selection(1, target)
        chooser.transition_table.selectRow(0)
        chooser._accept_selected_transition()
        return chooser.selected_transition

    def test_each_selected_flip_stays_visible_after_save_and_missing_roadmap(self):
        for target, count in ((4, 2), (5, 3)):
            with self.subTest(flips=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "Df1a_roadmap.json"
                path.write_text(json.dumps(self.multi_flip_payload()), encoding="utf-8")
                document = gui.load_roadmap_document(path)
                selection = self.select_multi_flip_path(document, target)
                self.assertEqual(len(selection.component_flips), count)
                with patch.object(gui.AdsController, "start"):
                    window = gui.PressureControlWindow()
                try:
                    window._apply_roadmap_transition(selection)
                    details = window.transition_flips_text.text()
                    self.assertFalse(window.transition_flips_text.isHidden())
                    self.assertIn("Flip 1:", details)
                    self.assertIn("Pose 1 → Pose 2", details)
                    self.assertIn("free Y rotation", details)
                    self.assertIn("+90.0°", details)
                    self.assertIn("Flip 2:", details)
                    self.assertIn("Pose 2 → Pose 4", details)
                    self.assertIn("free Z rotation", details)
                    self.assertIn("-45.0°", details)
                    if count == 3:
                        self.assertIn("Flip 3:", details)
                        self.assertIn("Pose 4 → Pose 5", details)
                        self.assertIn("+X · main face on wall", details)
                        self.assertIn("+60.0°", details)
                    profile_path = Path(directory) / "calibration.json"
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(profile_path), "")):
                        window.save_profile()
                    saved = json.loads(profile_path.read_text(encoding="utf-8"))
                    self.assertEqual(len(saved["roadmap_transition"]["component_flips"]), count)
                    path.unlink()
                    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(profile_path), "")):
                        window.load_profile()
                    self.assertEqual(window.transition_flips_text.text(), details)
                    self.assertFalse(window.transition_context_frame.isHidden())
                    self.assertFalse(window.transition_flips_text.isHidden())

                    direct = document.transitions[0]
                    window._apply_roadmap_transition(gui.SelectedRoadmapTransition(
                        document.path, document.part_name, direct, document.pose(1), document.pose(2)
                    ))
                    self.assertTrue(window.transition_flips_text.isHidden())
                    self.assertEqual(window.transition_flips_text.text(), "")
                finally:
                    window.close()

    def test_older_component_edge_metadata_recovers_angles_without_guessing_missing_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Df1a_roadmap.json"
            payload = self.multi_flip_payload()
            path.write_text(json.dumps(payload), encoding="utf-8")
            document = gui.load_roadmap_document(path)
            selection = self.select_multi_flip_path(document, 4)
            metadata = gui.roadmap_transition_metadata(selection)
            metadata.pop("component_flips")
            restored = gui.selection_from_roadmap_transition_metadata(metadata)
            self.assertEqual([flip.signed_angle_deg for flip in restored.component_flips], [90.0, -45.0])
            payload["edges"] = [edge for edge in payload["edges"] if edge["source"] != 2]
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = gui.selection_from_roadmap_transition_metadata(metadata)
            with patch.object(gui.AdsController, "start"):
                window = gui.PressureControlWindow()
            try:
                window._apply_roadmap_transition(restored)
                self.assertIn("+90.0°", window.transition_flips_text.text())
                self.assertIn("Flip 2:</b> Pose 2 → Pose 4 · axis / angle unavailable", window.transition_flips_text.text())
                self.assertNotIn("-45.0°", window.transition_flips_text.text())
            finally:
                window.close()

    def test_removed_edge_or_unreadable_roadmap_keeps_saved_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Df1a_roadmap.json"
            payload = existing_tests.RoadmapTransitionGuiTests.roadmap_payload()
            path.write_text(json.dumps(payload), encoding="utf-8")
            document = gui.load_roadmap_document(path)
            selection = gui.SelectedRoadmapTransition(
                path, document.part_name, document.transitions[0],
                document.pose(1), document.pose(2),
            )
            metadata = gui.roadmap_transition_metadata(selection)
            payload["edges"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = gui.selection_from_roadmap_transition_metadata(metadata)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.transition.edge_id, selection.transition.edge_id)
            self.assertEqual(restored.transition.signed_angle_deg, 90.0)
            self.assertEqual(restored.source_pose.equivalent_pose_ids, (1, 4, 7))
            self.assertEqual(restored.target_pose.wall_contact, "edge")
            path.write_text("invalid JSON", encoding="utf-8")
            restored = gui.selection_from_roadmap_transition_metadata(metadata)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.source_pose.pose_id, 1)
            self.assertEqual(restored.target_pose.pose_id, 2)

    def test_legacy_profile_restores_large_images_and_preserves_save_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "Df1a.STL"
            mesh.write_text(
                "solid part\nfacet normal 0 0 1\nouter loop\n"
                "vertex 0 0 0\nvertex 2 0 0\nvertex 0 1 1\n"
                "endloop\nendfacet\nendsolid part\n", encoding="utf-8",
            )
            payload = existing_tests.RoadmapTransitionGuiTests.roadmap_payload()
            payload["source"] = str(mesh)
            (root / "Df1a_roadmap.json").write_text(json.dumps(payload), encoding="utf-8")
            profile_path = root / "Df1a_Uebergang_1-2_multiple_reorientation_2.json"
            profile_path.write_text(
                json.dumps({"version": 12, "arrays": [], "title": "My calibration"}),
                encoding="utf-8",
            )
            with patch.object(gui.AdsController, "start"):
                window = gui.PressureControlWindow()
            try:
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(profile_path), "")):
                    window.load_profile()
                self.assertFalse(window.transition_context_frame.isHidden())
                self.assertIn("Transition 1-2", window.transition_context_text.text())
                self.assertIn("2 flips", window.transition_context_text.text())
                self.assertIn("Individual flip details were not saved", window.transition_flips_text.text())
                self.assertEqual(window.selected_roadmap_transition.transition.via_pose_ids, ())
                for label in (window.transition_source_image, window.transition_target_image):
                    self.assertEqual((label.width(), label.height()), (240, 176))
                    self.assertEqual((label.pixmap().width(), label.pixmap().height()), (224, 164))
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(profile_path), "")) as save_dialog:
                    window.save_profile()
                self.assertEqual(Path(save_dialog.call_args.args[2]), profile_path.resolve())
                saved = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["title"], window.selected_roadmap_transition.transition.display_name)
                self.assertEqual(window.profile_title, saved["title"])
                self.assertEqual(saved["roadmap_transition"]["source_pose_id"], 1)
                self.assertEqual(saved["roadmap_transition"]["target_pose_id"], 2)
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(profile_path), "")):
                    window.load_profile()
                self.assertFalse(window.transition_context_frame.isHidden())
                self.assertFalse(window.transition_source_image.pixmap().isNull())
                renamed = root / "my_calibration_copy.json"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(renamed), "")):
                    window.save_profile()
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as save_dialog:
                    window.save_profile()
                self.assertEqual(Path(save_dialog.call_args.args[2]), renamed.resolve())

                # A profile with no pose identity must not inherit unrelated poses.
                plain = root / "plain_pressure.json"
                plain.write_text(json.dumps({"version": 12, "arrays": []}), encoding="utf-8")
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(plain), "")):
                    window.load_profile()
                self.assertTrue(window.transition_context_frame.isHidden())
                self.assertIsNone(window.selected_roadmap_transition)
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as save_dialog:
                    window.save_profile()
                self.assertEqual(Path(save_dialog.call_args.args[2]), plain.resolve())
            finally:
                window.close()

    def test_every_save_refreshes_title_from_current_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "roadmap.json"
            path.write_text(json.dumps(self.multi_flip_payload()), encoding="utf-8")
            document = gui.load_roadmap_document(path)
            profile_path = root / "calibration.json"
            with patch.object(gui.AdsController, "start"):
                window = gui.PressureControlWindow()
            try:
                window.profile_title = "Old calibration title"
                for target in (2, 5, 4, 4):
                    selection = self.select_multi_flip_path(document, target)
                    window._apply_roadmap_transition(selection)
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(profile_path), "")):
                        window.save_profile()
                    saved = json.loads(profile_path.read_text(encoding="utf-8"))
                    self.assertEqual(saved["title"], selection.transition.display_name)
                    self.assertEqual(saved["title"], saved["roadmap_transition"]["title"])
                    self.assertEqual(window.profile_title, saved["title"])
                    self.assertEqual(saved["roadmap_transition"]["target_pose_id"], target)
            finally:
                window.close()

    def test_new_transition_replaces_save_suggestion_but_keeps_custom_name_for_same_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "roadmap.json"
            path.write_text(json.dumps(self.multi_flip_payload()), encoding="utf-8")
            document = gui.load_roadmap_document(path)
            first = self.select_multi_flip_path(document, 2)
            second = self.select_multi_flip_path(document, 5)
            original = root / "my_first_calibration.json"
            original.write_text(json.dumps({
                "version": 12, "arrays": [], "title": "Stale transition title",
                "roadmap_transition": gui.roadmap_transition_metadata(first),
            }), encoding="utf-8")
            original_contents = original.read_bytes()
            with patch.object(gui.AdsController, "start"):
                window = gui.PressureControlWindow()
            try:
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(original), "")):
                    window.load_profile()
                self.assertEqual(window.profile_title, first.transition.display_name)
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as dialog:
                    window.save_profile()
                self.assertEqual(Path(dialog.call_args.args[2]), original.resolve())

                # Exercise the real roadmap-loading path, which used to clear the title.
                with (patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "")),
                      patch.object(gui, "RoadmapTransitionDialog") as chooser):
                    chooser.return_value.exec.return_value = gui.QDialog.DialogCode.Accepted
                    chooser.return_value.selected_transition = second
                    window.load_pose_roadmap()
                self.assertEqual(window.profile_title, second.transition.display_name)
                for _ in range(2):  # Cancelling must not revert the new suggestion.
                    with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as dialog:
                        window.save_profile()
                    self.assertEqual(Path(dialog.call_args.args[2]), root.resolve() / f"{second.profile_name_stem}.json")
                self.assertEqual(original.read_bytes(), original_contents)

                renamed = root / "my_second_calibration.json"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(renamed), "")):
                    window.save_profile()
                saved = json.loads(renamed.read_text(encoding="utf-8"))
                self.assertEqual(saved["title"], second.transition.display_name)
                self.assertEqual(saved["roadmap_transition"]["target_pose_id"], 5)
                window._apply_roadmap_transition(second)
                with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as dialog:
                    window.save_profile()
                self.assertEqual(Path(dialog.call_args.args[2]), renamed.resolve())

                # A new action or part also changes the calibration identity.
                alternatives = (
                    replace(second, transition=replace(second.transition, actuation="free_z")),
                    replace(second, part_name="AnotherPart"),
                )
                for selection in alternatives:
                    window._apply_roadmap_transition(selection)
                    with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as dialog:
                        window.save_profile()
                    self.assertEqual(Path(dialog.call_args.args[2]), root.resolve() / f"{selection.profile_name_stem}.json")
            finally:
                window.close()

    def test_save_without_transition_preserves_custom_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.json"
            with patch.object(gui.AdsController, "start"):
                window = gui.PressureControlWindow()
            try:
                window.profile_title = "My pressure calibration"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                    window.save_profile()
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["title"], "My pressure calibration")
                self.assertNotIn("roadmap_transition", saved)
            finally:
                window.close()

    def test_german_profile_filename_still_marks_path_as_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roadmap_path = root / "Df1a_roadmap.json"
            roadmap_path.write_text(json.dumps(existing_tests.RoadmapTransitionGuiTests.roadmap_payload()), encoding="utf-8")
            document = gui.load_roadmap_document(roadmap_path)
            path = root / "Df1a_Uebergang_1-2_free_y.json"
            path.write_text(json.dumps({"version": 12, "arrays": []}), encoding="utf-8")
            chooser = gui.RoadmapTransitionDialog(document, profile_directory=root)
            chooser._set_pose_selection(1, 2)
            self.assertIn(path.name, chooser.transition_table.item(0, 3).text())
