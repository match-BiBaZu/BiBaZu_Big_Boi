from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox
from test_batch_controller import multi_frame
from test_manual_config import make_manual_part

from bibazu_reorientation.models import BatchState, CameraFrame, ProductionMode
from bibazu_reorientation.settings import AppSettings
from bibazu_reorientation.ui.main_window import MainWindow
from bibazu_reorientation.ui.manual_setup_dialog import ManualSetupDialog, ModelClassReader


@pytest.fixture
def manual_part(tmp_path):
    return make_manual_part(tmp_path)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = MainWindow(AppSettings())
    qtbot.addWidget(result)
    monkeypatch.setattr(result, "load_yolo_model", lambda: None)
    yield result
    result.close()
    result.camera.shutdown()
    result.pressure.shutdown()


def test_manual_setup_edits_existing_classes_profiles_and_target(qtbot, manual_part):
    dialog = ManualSetupDialog(definition=manual_part)
    qtbot.addWidget(dialog)
    assert dialog.model.text() == str(manual_part.model_path)
    assert dialog.target_class.currentData() == 42
    assert [row[0].value() for row in dialog.rows] == [0, 7, 12, 42]
    assert dialog.rows[1][1].text() == "On side"
    assert dialog.rows[1][2].text() == str(manual_part.transitions[1].pressure_profile)
    assert not dialog.table.cellWidget(3, 2).isEnabled()
    dialog.target_class.setCurrentIndex(dialog.target_class.findData(7))
    assert not dialog.table.cellWidget(1, 2).isEnabled()
    assert dialog.table.cellWidget(3, 2).isEnabled()
    values = dialog._configuration_values()
    assert values["target_class_id"] == 7
    assert values["class_labels"][0] == "Face down"


def test_manual_setup_adds_removes_classes_and_rejects_duplicates(qtbot):
    dialog = ManualSetupDialog()
    qtbot.addWidget(dialog)
    dialog.add_start_pose()
    dialog.add_start_pose()
    assert dialog.target_class.count() == 2
    dialog.rows[1][0].setValue(0)
    with pytest.raises(ValueError, match="unique"):
        dialog._configuration_values()
    dialog.table.selectRow(1)
    dialog.remove_start_pose()
    assert len(dialog.rows) == dialog.target_class.count() == 1


def test_model_class_reader_populates_rows_and_preserves_assignments(
    qtbot, manual_part, monkeypatch
):
    def run(reader):
        reader.loaded.emit({0: "Front", 7: "Side", 42: "Target"})

    monkeypatch.setattr(ModelClassReader, "run", run)
    dialog = ManualSetupDialog(definition=manual_part)
    qtbot.addWidget(dialog)
    dialog.read_model_classes()
    qtbot.waitUntil(lambda: dialog._reader is None)
    assert [row[0].value() for row in dialog.rows] == [0, 7, 42]
    assert dialog.rows[1][1].text() == "Side"
    assert dialog.rows[1][2].text() == str(manual_part.transitions[1].pressure_profile)
    assert dialog.target_class.currentData() == 42
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()


def test_cancel_during_model_read_waits_for_thread_before_destroying_dialog(
    qtbot, manual_part, monkeypatch
):
    entered = threading.Event()

    def run(reader):
        entered.set()
        while not reader.isInterruptionRequested():
            threading.Event().wait(0.01)

    monkeypatch.setattr(ModelClassReader, "run", run)
    dialog = ManualSetupDialog(definition=manual_part)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.read_model_classes()
    qtbot.waitUntil(entered.is_set)
    finished = []
    dialog._reader.finished.connect(lambda: finished.append(True))
    dialog.reject()
    qtbot.waitUntil(lambda: dialog._reader is None)
    assert finished == [True]
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_manual_window_loads_and_executes_without_loading_any_roadmap(window, manual_part):
    with patch("bibazu_reorientation.ui.main_window.load_pose_roadmap", side_effect=AssertionError):
        window._load(manual_part)
    assert manual_part.part_name in window.part_label.text()
    assert "Mode:" not in window.part_label.text()
    assert window.model_label.text() == ""
    assert window._machine_parameters_confirmed
    assert window.controller.part == manual_part
    assert len(window.controller._roadmap_profiles) == 2
    window.conveyor_speed_input.setValue(125.0)
    assert window.controller.part is None
    window.apply_machine_parameters(show_error=False)
    assert window.controller.profile.conveyor_speed_mm_per_sec == 125.0


def test_manual_window_accepts_current_pressure_control_profiles(window, manual_part):
    exported = (Path(__file__).parent / "fixtures" / "pressure_profile_v13.json").read_bytes()
    for edge in manual_part.transitions:
        edge.pressure_profile.write_bytes(exported)
    window._load(manual_part)
    assert window._machine_parameters_confirmed
    assert window.controller.part == manual_part
    assert "Invalid transition profile" not in window.machine_parameter_status.text()
    assert window.machine_parameter_status.text() == ""
    assert window.machine_parameter_status.isHidden()
    assert all(
        profile.source_version == 13 for profile in window.controller._roadmap_profiles.values()
    )


def test_manual_inference_accepts_arbitrary_model_classes(window, manual_part):
    window._load(manual_part)
    with patch("bibazu_reorientation.ui.main_window.InferenceWorker") as worker:
        window._start_yolo_worker()
        config = worker.call_args.args[0]
    assert set(config.expected_class_ids) == {0, 7, 12, 42}
    assert dict(config.class_to_pose) == {0: 0, 7: 7, 12: 12, 42: 42}


def test_manual_configuration_menu_and_edit_route_to_manual_setup(window, manual_part, monkeypatch):
    def choose_manual(chooser):
        button = next(
            button for button in chooser.buttons() if button.text() == "Manual configuration"
        )
        button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", choose_manual)
    with patch("bibazu_reorientation.ui.main_window.ManualSetupDialog") as dialog:
        dialog.return_value.create.return_value = manual_part
        window.new_config_action.trigger()
        assert window.part == manual_part
        window.edit_configuration()
        assert dialog.call_args.args == (window, manual_part)
    window._cycle_state_changed(BatchState.STARTING, "Preparing")
    assert not window.new_config_action.isEnabled()
    assert not window.new_config_button.isEnabled()


@pytest.mark.parametrize("state", [BatchState.RUNNING, BatchState.FINISHING, BatchState.DRAINING])
def test_continuous_feed_is_selectable_and_stop_is_available_while_running(
    window, monkeypatch, state
):
    monkeypatch.setattr(AppSettings, "save", lambda _self: None)
    assert window.controller.production_mode == ProductionMode.CONTINUOUS
    window._cycle_state_changed(state, "Running")
    assert window.stop_button.isEnabled()
    assert window.stop_button.text() == "Stop production run"
    assert not window.production_mode_input.isEnabled()
    assert not window.handoff_slider.isEnabled()
    assert not window.input_slider.isEnabled()
    assert window.input_slider.isHidden()
    assert window.input_value.isHidden()
    window._cycle_state_changed(BatchState.OFFLINE, "Stopped")
    assert not window.input_slider.isHidden()
    assert not window.input_value.isHidden()
    window.production_mode_input.setCurrentIndex(window.production_mode_input.findData("snapshot"))
    assert window.controller.production_mode == ProductionMode.SNAPSHOT
    assert window.settings.production_mode == "snapshot"


def test_single_yellow_line_moves_with_the_handoff_slider(window, monkeypatch):
    saved, images = [], []
    monkeypatch.setattr(
        AppSettings, "save", lambda settings: saved.append(settings.handoff_line_percent)
    )
    monkeypatch.setattr(window, "_show_image", lambda image: images.append(image.copy()))
    window._camera_frame(CameraFrame(np.zeros((100, 100, 3), np.uint8), "RGB8", time.time()))
    window.handoff_slider.setValue(45)
    assert saved[-1] == 45
    assert window.handoff_value.text() == "45 %"
    assert window.controller.handoff_line_ratio == window.controller.pose_cutoff_ratio == 0.45
    np.testing.assert_array_equal(images[-1][80, 45], (250, 204, 21))
    np.testing.assert_array_equal(images[-1][80, 10], (0, 0, 0))
    np.testing.assert_array_equal(images[-1][80, 30], (0, 0, 0))


def test_handoff_slider_updates_inference_and_refuses_edits_during_run(window, monkeypatch):
    monkeypatch.setattr(AppSettings, "save", lambda _self: None)
    with patch("bibazu_reorientation.ui.main_window.InferenceWorker") as worker:
        window.inference = worker.return_value
        window.handoff_slider.setValue(40)
        worker.return_value.set_pose_cutoff_ratio.assert_called_once_with(0.4)
    window.controller.state = BatchState.RUNNING
    window.handoff_slider.setValue(50)
    assert window.controller.handoff_line_ratio == 0.4
    assert window.handoff_slider.value() == 40


def test_preview_keeps_yolo_image_without_tracking_boxes_or_consensus(window, manual_part):
    window._load(manual_part)
    frame = multi_frame((60, 7))
    images = []
    with patch.object(window, "_show_image", side_effect=images.append):
        window._inference_frame(frame)
    np.testing.assert_array_equal(images[-1], frame.image)
    assert "consensus" not in window.pose_label.text()
    assert "/5" not in window.pose_label.text()
    assert "Track" not in window.pose_label.text()


@pytest.mark.parametrize(
    "code",
    [
        "plc_airborne_sensor_skip",
        "plc_airborne_duplicate_edge",
        "plc_light_barrier_no_clear_ignored",
        "plc_light_barrier_queue_edge_ignored",
        "plc_light_barrier_order_edge_ignored",
    ],
)
def test_continuous_light_barrier_notices_have_no_warning_banner(window, code):
    window.controller.state = BatchState.RUNNING
    window._production_warning(code, "LB2 ignored while multiple parts are in transit")
    assert window.warning_banner.isHidden()
    assert not window.warning_display_timer.isActive()
    assert window.controller.state == BatchState.RUNNING


def test_input_slider_saves_updates_worker_and_stays_right_of_handoff(window, monkeypatch):
    saved = []
    monkeypatch.setattr(
        AppSettings, "save", lambda settings: saved.append(settings.input_line_percent)
    )
    with patch("bibazu_reorientation.ui.main_window.InferenceWorker") as worker:
        window.inference = worker.return_value
        window.input_slider.setValue(70)
        worker.return_value.set_input_line_ratio.assert_called_once_with(0.7)
    assert saved[-1] == 70
    assert window.input_value.text() == "70 %"
    assert window.controller.input_line_ratio == 0.7
    assert window.handoff_slider.maximum() == 69
    window.handoff_slider.setValue(60)
    assert window.input_slider.minimum() == 61
    window.controller.state = BatchState.RUNNING
    window.input_slider.setValue(80)
    assert window.input_slider.value() == 70
    assert window.controller.input_line_ratio == 0.7
