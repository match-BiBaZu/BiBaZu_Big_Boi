from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from PyQt6.QtCore import QTimer, pyqtSignal

from bibazu_reorientation.hardware.base import DeviceAdapter
from bibazu_reorientation.models import ConnectionState, LightStatus


class LightAdapter(DeviceAdapter):
    status_changed = pyqtSignal(object)

    def __init__(self, name: str, address: str = "") -> None:
        super().__init__(name)
        self.address = address
        self._light: Any = None
        self.status = LightStatus(address=address)
        self._task: asyncio.Task[Any] | None = None
        self._monitor = QTimer(self)
        self._monitor.setInterval(2000)
        self._monitor.timeout.connect(self._check_connection)
        self._monitor.start()

    def _check_connection(self) -> None:
        if self._light is None or self.state is not ConnectionState.CONNECTED:
            return
        client = getattr(self._light, "client", None)
        connected = getattr(client, "is_connected", False)
        connected = connected() if callable(connected) else connected
        if not connected:
            self.status.connected = False
            self.status.values_are_confirmed_commands = False
            self.status_changed.emit(self.status)
            self._light = None
            self._emit_error("Bluetooth-Verbindung zum Panel wurde unterbrochen")

    def connect_device(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._connect())

    async def _connect(self) -> None:
        self._set_state(ConnectionState.CONNECTING, "Bluetooth-Verbindung")
        try:
            from neewerlite import NeewerLight, NeewerScanner

            devices = await NeewerScanner.scan(timeout=5.0)
            candidates = [d for d in devices if not self.address or str(d.address) == self.address]
            if not candidates:
                raise RuntimeError("Keine passende Neewer-Leuchte gefunden")
            device = candidates[0]
            self.address = str(device.address)
            self._light = NeewerLight(device, name=self.name)
            await asyncio.wait_for(self._light.connect(), 5.0)
            self.status.address = self.address
            self.status.name = str(getattr(device, "name", self.name))
            self.status.connected = True
            self.status.values_are_confirmed_commands = False
            self._set_state(ConnectionState.CONNECTED, self.address)
            self.status_changed.emit(self.status)
        except Exception as exc:
            self._emit_error(str(exc))

    def set_cct(self, brightness: int, kelvin: int) -> None:
        self._schedule("CCT", brightness, kelvin)

    def set_hsi(self, brightness: int, hue: int, saturation: int) -> None:
        self._schedule("HSI", brightness, hue, saturation)

    def set_power(self, enabled: bool) -> None:
        self._schedule("POWER", enabled)

    def _schedule(self, command: str, *args: Any) -> None:
        self._task = asyncio.get_running_loop().create_task(self._command(command, *args))

    async def _command(self, command: str, *args: Any) -> None:
        if self._light is None:
            self._emit_error("Leuchte ist nicht verbunden")
            return
        try:
            if command == "CCT":
                brightness, kelvin = args
                await asyncio.wait_for(self._light.set_cct(kelvin, brightness, gm=50), 3.0)
                self.status.mode, self.status.brightness, self.status.cct_kelvin = command, *args
            elif command == "HSI":
                brightness, hue, saturation = args
                await asyncio.wait_for(self._light.set_rgb(hue, saturation, brightness), 3.0)
                self.status.mode = command
                self.status.brightness, self.status.hue, self.status.saturation = args
            else:
                method = self._light.turn_on if args[0] else self._light.turn_off
                await asyncio.wait_for(method(), 3.0)
                self.status.power = bool(args[0])
            self.status.values_are_confirmed_commands = True
            self.status_changed.emit(self.status)
        except Exception as exc:
            self._emit_error(str(exc))

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._light is not None:
            with contextlib.suppress(Exception):
                await self._light.disconnect()
        self.status.connected = False
        self._set_state(ConnectionState.DISCONNECTED)
