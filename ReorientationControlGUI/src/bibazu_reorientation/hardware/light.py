from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QTimer, pyqtSignal

from bibazu_reorientation.hardware.base import DeviceAdapter
from bibazu_reorientation.models import ConnectionState, LightStatus


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    result = getattr(value, name, default)
    return result() if callable(result) else result


def _looks_like_neewer(device: Any) -> bool:
    name = str(_attribute(device, "name", "") or "").upper()
    return any(token in name for token in ("NEEWER", "RGB660", "RGB 660", "NW-", "ZN-"))


class LightAdapter(DeviceAdapter):
    status_changed = pyqtSignal(object)

    def __init__(
        self,
        name: str,
        address: str = "",
        excluded_addresses: Callable[[], set[str]] | None = None,
    ) -> None:
        super().__init__(name)
        self.address = address
        self._light: Any = None
        self.status = LightStatus(address=address)
        self._excluded_addresses = excluded_addresses or set
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

    async def connect_async(self) -> None:
        if self.state is ConnectionState.CONNECTED:
            return
        await self._connect()

    async def _connect(self) -> None:
        self._set_state(ConnectionState.CONNECTING, "Bluetooth-Verbindung")
        try:
            from neewerlite import NeewerLight, NeewerScanner

            try:
                devices = await NeewerScanner.scan(timeout=5.0)
            except TypeError:
                devices = await NeewerScanner.scan()
            excluded = {address.casefold() for address in self._excluded_addresses() if address}
            candidates = [
                device
                for device in devices
                if (not self.address or str(device.address).casefold() == self.address.casefold())
                and str(device.address).casefold() not in excluded
                and (bool(self.address) or _looks_like_neewer(device))
            ]
            if not candidates:
                raise RuntimeError(
                    "Keine passende RGB660/NEEWER-Leuchte gefunden. Panel einschalten, "
                    "Bluetooth-Symbol aktivieren und die Smartphone-App trennen."
                )
            device = sorted(
                candidates,
                key=lambda item: int(_attribute(item, "rssi", -999) or -999),
                reverse=True,
            )[0]
            self.address = str(device.address)
            device_name = str(_attribute(device, "name", ""))
            profile_name = "RGB660" if "660" in device_name.upper() else self.name
            # Unter Windows muss das beim Scan erhaltene BLEDevice weitergegeben werden.
            # Nur die Adresse reicht bei zufälligen WinRT-Adressen häufig nicht aus.
            self._light = NeewerLight(device, name=profile_name)
            await self._light.connect()
            self.status.address = self.address
            self.status.name = str(_attribute(device, "name", self.name) or self.name)
            self.status.connected = True
            self.status.values_are_confirmed_commands = False
            self._set_state(ConnectionState.CONNECTED, self.address)
            self.status_changed.emit(self.status)
        except Exception as exc:
            self._emit_error(str(exc) or type(exc).__name__)

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
            self._emit_error(str(exc) or type(exc).__name__)

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
