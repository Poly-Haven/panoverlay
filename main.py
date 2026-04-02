from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QRect, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from data import OverlayModel, load_overlay_model
from overlay import OverlayWindow


DEFAULT_PROJECT = Path(r"C:\Panos\witsand_beac_rocks_01\work\witsand_beac_rocks_01_greg.pts")
CONFIG_PATH = Path(__file__).with_name("panoverlay.config.json")
POLL_INTERVAL_MS = 1000


class ControlPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("panoverlay")
        self.overlay = OverlayWindow()
        self.config = load_config()
        self.project_path: Path | None = None
        self.last_mtime_ns: int | None = None

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select a PTGui .pts or .ptgui file")
        self.path_edit.setReadOnly(True)

        self.status_label = QLabel("No project loaded")
        self.status_label.setWordWrap(True)

        self.edit_button = QPushButton("Edit overlay box")
        self.edit_button.clicked.connect(self.toggle_edit_mode)

        self.relative_checkbox = QCheckBox("Relative distances")
        self.relative_checkbox.setChecked(bool(self.config.get("relative_distances", False)))
        self.relative_checkbox.toggled.connect(self.on_relative_distances_toggled)
        self.overlay.set_relative_distances(self.relative_checkbox.isChecked())

        browse_button = QPushButton("Open project")
        browse_button.clicked.connect(self.open_project_dialog)

        reload_button = QPushButton("Reload now")
        reload_button.clicked.connect(lambda: self.reload_project(show_error=True))

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_button)
        path_row.addWidget(reload_button)

        layout = QVBoxLayout()
        layout.addLayout(path_row)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.relative_checkbox)
        layout.addWidget(self.status_label)
        self.setLayout(layout)
        self.resize(560, 140)

        self.restore_overlay_geometry()
        self.overlay.boxChanged.connect(self.on_overlay_box_changed)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_project_file)
        self.poll_timer.start(POLL_INTERVAL_MS)

        if DEFAULT_PROJECT.exists():
            self.load_project(DEFAULT_PROJECT, show_error=False)

        self.show()
        self.raise_()
        self.activateWindow()

    def open_project_dialog(self):
        start_dir = str(self.project_path.parent if self.project_path else DEFAULT_PROJECT.parent)
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select PTGui project",
            start_dir,
            "PTGui Project (*.pts *.ptgui);;All Files (*)",
        )
        if file_path:
            self.load_project(Path(file_path), show_error=True)

    def load_project(self, project_path: Path, *, show_error: bool):
        try:
            model = load_overlay_model(project_path)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Failed to load project: {exc}")
            if show_error:
                QMessageBox.critical(self, "panoverlay", f"Could not load project.\n\n{exc}")
            return

        self.apply_model(model)

    def apply_model(self, model: OverlayModel):
        self.project_path = model.project_path
        self.path_edit.setText(str(model.project_path))
        self.last_mtime_ns = self._read_mtime(model.project_path)
        self.overlay.set_model(model)
        self.status_label.setText(
            f"Loaded {model.project_path.name} | images: {len(model.images)} | pairs: {len(model.pairs)}"
        )

    def reload_project(self, *, show_error: bool):
        if self.project_path is None:
            return
        self.load_project(self.project_path, show_error=show_error)

    def poll_project_file(self):
        if self.project_path is None:
            return
        current_mtime = self._read_mtime(self.project_path)
        if current_mtime is None:
            self.status_label.setText(f"Waiting for project file: {self.project_path}")
            return
        if self.last_mtime_ns is None:
            self.last_mtime_ns = current_mtime
            return
        if current_mtime != self.last_mtime_ns:
            self.load_project(self.project_path, show_error=False)

    def toggle_edit_mode(self):
        edit_mode = self.overlay.toggle_edit_mode()
        self.edit_button.setText("Lock overlay box" if edit_mode else "Edit overlay box")
        self.raise_()
        self.activateWindow()

    def restore_overlay_geometry(self):
        overlay_box = self.config.get("overlay_box")
        rect = rect_from_config(overlay_box)
        if rect is not None:
            self.overlay.set_box_rect(rect)
        else:
            self.save_config()

    def on_overlay_box_changed(self, rect: QRect):
        self.config["overlay_box"] = rect_to_config(rect)
        save_config(self.config)

    def on_relative_distances_toggled(self, checked: bool):
        self.overlay.set_relative_distances(checked)
        self.config["relative_distances"] = checked
        save_config(self.config)

    def save_config(self):
        self.config["overlay_box"] = rect_to_config(self.overlay.get_box_rect())
        self.config["relative_distances"] = self.relative_checkbox.isChecked()
        save_config(self.config)

    def closeEvent(self, event):
        self.save_config()
        self.overlay.close()
        super().closeEvent(event)

    @staticmethod
    def _read_mtime(project_path: Path) -> int | None:
        try:
            return project_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}

    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict):
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def rect_to_config(rect: QRect) -> dict[str, int]:
    return {
        "x": rect.x(),
        "y": rect.y(),
        "width": rect.width(),
        "height": rect.height(),
    }


def rect_from_config(payload) -> QRect | None:
    if not isinstance(payload, dict):
        return None

    try:
        x = int(payload["x"])
        y = int(payload["y"])
        width = int(payload["width"])
        height = int(payload["height"])
    except (KeyError, TypeError, ValueError):
        return None

    if width <= 0 or height <= 0:
        return None
    return QRect(x, y, width, height)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("panoverlay")
    panel = ControlPanel()
    panel.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
