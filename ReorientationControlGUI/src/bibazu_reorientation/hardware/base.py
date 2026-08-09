from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from bibazu_reorientation.models import ConnectionState


class DeviceAdapter(QObject):
    state_changed = pyqtSignal(object, str)
    error = pyqtSignal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.state = ConnectionState.DISCONNECTED

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self.state = state
        self.state_changed.emit(state, detail)

    def _emit_error(self, detail: str) -> None:
        self._set_state(ConnectionState.ERROR, detail)
        self.error.emit(detail)
