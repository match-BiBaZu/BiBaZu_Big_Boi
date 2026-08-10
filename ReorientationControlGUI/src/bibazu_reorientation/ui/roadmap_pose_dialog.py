"""Clickable thumbnail chooser for robust roadmap poses."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bibazu_reorientation.mesh_preview import render_mesh_preview
from bibazu_reorientation.roadmap import StablePoseRoadmap, StableRoadmapPose


class RoadmapPoseDialog(QDialog):
    def __init__(self, roadmap: StablePoseRoadmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.roadmap = roadmap
        self.selected_pose: StableRoadmapPose | None = None
        self.setWindowTitle(f"Stabile Zielpose auswählen · {roadmap.part_name}")
        self.setModal(True)
        self.resize(920, 650)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(
            f"<b>{self.roadmap.part_name}</b> · stabile Roadmap-Posen · "
            f"CAD-Status: {self.roadmap.cad_status}"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 17px; padding: 8px;")
        layout.addWidget(title)

        hint = QLabel(
            "Eine Pose anklicken, um sie als physische Zielpose der aktuell "
            "gewählten YOLO-Zielklasse zu übernehmen."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "background:#edf7ff;border:1px solid #7aa7d9;border-radius:5px;padding:8px;"
        )
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        cards = QWidget()
        grid = QGridLayout(cards)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for index, pose in enumerate(self.roadmap.robust_poses):
            grid.addWidget(self._pose_button(pose), index // 4, index % 4)
        scroll.setWidget(cards)
        layout.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("Schließen")
        close_button.clicked.connect(self.reject)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _pose_button(self, pose: StableRoadmapPose) -> QToolButton:
        button = QToolButton()
        button.setObjectName(f"roadmap_pose_{pose.pose_id}")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(190, 130))
        button.setFixedSize(210, 195)
        pixmap = QPixmap()
        if pose.thumbnail_png:
            pixmap.loadFromData(pose.thumbnail_png, "PNG")
        if pixmap.isNull() and self.roadmap.mesh_path is not None:
            try:
                pixmap = render_mesh_preview(
                    self.roadmap.mesh_path,
                    width=190,
                    height=130,
                    quaternion_xyzw=pose.quaternion_xyzw,
                    caption=f"CAD · Pose {pose.pose_id}",
                )
            except (OSError, ValueError):
                pixmap = QPixmap()
        if not pixmap.isNull():
            button.setIcon(
                QIcon(
                    pixmap.scaled(
                        190,
                        130,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            )
        equivalent_ids = "/".join(str(value) for value in pose.equivalent_pose_ids)
        preview_note = "" if not pixmap.isNull() else "\n(kein Vorschaubild)"
        button.setText(f"Pose {pose.pose_id}\nIDs: {equivalent_ids}{preview_note}")
        button.setToolTip(
            f"Boden: {pose.floor_contact}; Wand: {pose.wall_contact}\nKlicken zum Übernehmen"
        )
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QToolButton {background:white;border:3px solid #1677c8;"
            "border-radius:8px;padding:6px;font-weight:600;color:#123a58;}"
            "QToolButton:hover {background:#dcefff;border-color:#075b9b;}"
            "QToolButton:pressed {background:#bddfff;}"
        )
        button.clicked.connect(lambda _checked=False, selected=pose: self._select(selected))
        return button

    def _select(self, pose: StableRoadmapPose) -> None:
        self.selected_pose = pose
        self.accept()
