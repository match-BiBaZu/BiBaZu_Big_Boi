from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.mesh_preview import (
    load_mesh_triangles,
    render_triangles_preview,
    slerp_quaternion,
)
from bibazu_reorientation.roadmap import PoseRoadmap, RoadmapPose, RoadmapTransition


class TransitionPreviewDialog(QDialog):
    """Animated orientation preview for one directed roadmap transition."""

    def __init__(
        self,
        roadmap: PoseRoadmap,
        transition: RoadmapTransition,
        parent: QWidget | None = None,
        mesh_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.roadmap = roadmap
        self.transition = transition
        self.mesh_path = mesh_path if mesh_path is not None else roadmap.mesh_path
        self.start_pose = roadmap.pose(transition.from_pose)
        self.end_pose = roadmap.pose(transition.to_pose)
        self._triangles = None
        self._animation_error = ""
        self._hold_ticks = 0
        self.setWindowTitle(
            f"Transition preview · Pose {transition.from_pose} → Pose {transition.to_pose}"
        )
        self.resize(980, 760)
        self._build_ui()
        self._load_geometry()
        self._render_static_views()
        self._render_progress(0)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._advance)
        self.finished.connect(lambda _result: self.timer.stop())
        if self._triangles is not None:
            self.timer.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(
            f"<b>Pose {self.transition.from_pose} → Pose {self.transition.to_pose}</b>"
            f" · {self.transition.actuation or 'actuated transition'}"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:18px;padding:6px;")
        layout.addWidget(title)

        angle = (
            "not specified"
            if self.transition.signed_angle_deg is None
            else f"{self.transition.signed_angle_deg:+.1f}°"
        )
        direction_symbol = "↺" if (self.transition.signed_angle_deg or 0.0) >= 0 else "↻"
        metadata = QLabel(
            f"Commanded rotation: <b>{direction_symbol} {angle}</b> · "
            f"Edge ID: <code>{self.transition.edge_id}</code>"
        )
        metadata.setAlignment(Qt.AlignmentFlag.AlignCenter)
        metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(metadata)

        if self.transition.transition_kind == "multi_reorientation":
            via = " → ".join(map(str, self.transition.via_pose_ids))
            category = QLabel(
                f"<b>Multiple reorientation ({self.transition.flip_count} flips)</b> "
                f"via pose(s) {via}. Experimental and non-preferred; use this as one "
                "direct calibrated profile when it is more robust than composing the "
                "individual flip profiles."
            )
            category.setWordWrap(True)
            category.setStyleSheet("background:#fff3cd;color:#664d03;padding:7px;")
            layout.addWidget(category)

        endpoints = QHBoxLayout()
        self.start_image = self._image_label()
        self.end_image = self._image_label()
        endpoints.addLayout(
            self._pose_column(f"Start · Pose {self.start_pose.pose_id}", self.start_image)
        )
        arrow = QLabel(f"<span style='font-size:42px'>{direction_symbol} →</span>")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        endpoints.addWidget(arrow)
        endpoints.addLayout(
            self._pose_column(f"Target · Pose {self.end_pose.pose_id}", self.end_image)
        )
        layout.addLayout(endpoints)

        self.animation = self._image_label(minimum_width=600, minimum_height=330)
        layout.addWidget(self.animation, 1, Qt.AlignmentFlag.AlignCenter)
        self.progress_text = QLabel("Animation progress: 0%")
        self.progress_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_text)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._render_progress)
        self.slider.sliderPressed.connect(self._pause_for_scrubbing)
        layout.addWidget(self.slider)

        controls = QHBoxLayout()
        self.play_button = QPushButton("Pause")
        self.play_button.clicked.connect(self._toggle_playback)
        restart = QPushButton("Restart")
        restart.clicked.connect(self._restart)
        self.loop = QCheckBox("Loop animation")
        self.loop.setChecked(True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        controls.addWidget(self.play_button)
        controls.addWidget(restart)
        controls.addWidget(self.loop)
        controls.addStretch(1)
        controls.addWidget(close)
        layout.addLayout(controls)

        note = QLabel(
            "Orientation interpolation for visual guidance only. It does not simulate the "
            "physical trajectory through the chute."
        )
        note.setWordWrap(True)
        note.setStyleSheet("background:#fff3cd;color:#664d03;padding:7px;")
        layout.addWidget(note)

    @staticmethod
    def _image_label(minimum_width: int = 330, minimum_height: int = 220) -> QLabel:
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(minimum_width, minimum_height)
        label.setStyleSheet(
            "background:#111827;color:#94a3b8;border:1px solid #334155;border-radius:7px;"
        )
        return label

    @staticmethod
    def _pose_column(title: str, image: QLabel) -> QVBoxLayout:
        column = QVBoxLayout()
        label = QLabel(f"<b>{title}</b>")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(label)
        column.addWidget(image)
        return column

    def _load_geometry(self) -> None:
        if self.mesh_path is None:
            self._animation_error = "No CAD model is referenced by this roadmap."
            return
        try:
            self._triangles = load_mesh_triangles(self.mesh_path)
        except (OSError, ValueError) as exc:
            self._animation_error = f"CAD animation unavailable: {exc}"

    def _pose_pixmap(self, pose: RoadmapPose, width: int, height: int) -> QPixmap:
        if self._triangles is not None:
            return render_triangles_preview(
                self._triangles,
                width,
                height,
                quaternion_xyzw=pose.quaternion_xyzw,
                caption=f"Roadmap pose {pose.pose_id}",
            )
        pixmap = QPixmap()
        if pose.thumbnail_png:
            pixmap.loadFromData(pose.thumbnail_png, "PNG")
        return pixmap

    @staticmethod
    def _set_pixmap(label: QLabel, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            label.setText("Preview image unavailable")
            return
        label.setText("")
        label.setPixmap(
            pixmap.scaled(
                label.minimumSize(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _render_static_views(self) -> None:
        self._set_pixmap(self.start_image, self._pose_pixmap(self.start_pose, 330, 220))
        self._set_pixmap(self.end_image, self._pose_pixmap(self.end_pose, 330, 220))

    def _render_progress(self, value: int) -> None:
        progress = min(1.0, max(0.0, value / 1000.0))
        self.progress_text.setText(f"Animation progress: {progress:.0%}")
        if self._triangles is None:
            self.animation.setText(
                f"{self._animation_error}\nUse the start and target images above."
            )
            self.slider.setEnabled(False)
            self.play_button.setEnabled(False)
            return
        quaternion = slerp_quaternion(
            self.start_pose.quaternion_xyzw,
            self.end_pose.quaternion_xyzw,
            progress,
        )
        frame = render_triangles_preview(
            self._triangles,
            600,
            330,
            quaternion_xyzw=quaternion,
            caption=f"Pose {self.start_pose.pose_id} → {self.end_pose.pose_id} · {progress:.0%}",
        )
        self._set_pixmap(self.animation, frame)

    def _advance(self) -> None:
        if self.slider.value() >= self.slider.maximum():
            if not self.loop.isChecked():
                self.timer.stop()
                self.play_button.setText("Play")
                return
            self._hold_ticks += 1
            if self._hold_ticks >= 12:
                self._hold_ticks = 0
                self.slider.setValue(0)
            return
        self.slider.setValue(min(self.slider.maximum(), self.slider.value() + 20))

    def _toggle_playback(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("Play")
        elif self._triangles is not None:
            self.timer.start()
            self.play_button.setText("Pause")

    def _restart(self) -> None:
        self._hold_ticks = 0
        self.slider.setValue(0)
        if self._triangles is not None:
            self.timer.start()
            self.play_button.setText("Pause")

    def _pause_for_scrubbing(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("Play")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        self.timer.stop()
        super().closeEvent(event)
