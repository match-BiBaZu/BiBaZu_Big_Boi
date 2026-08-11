from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from PyQt6.QtCore import QTimer, pyqtSignal

from bibazu_reorientation.hardware.base import DeviceAdapter
from bibazu_reorientation.models import (
    ConnectionState,
    DiscoveredLight,
    LightCapabilities,
    LightStatus,
)

LIGHT_COMMAND_TIMEOUT_SECONDS = 3.0
LOGGER = logging.getLogger(__name__)


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    result = getattr(value, name, default)
    return result() if callable(result) else result


def _as_discovered(device: Any) -> DiscoveredLight:
    name = str(_attribute(device, "name", "") or "Unnamed BLE device")
    address = str(_attribute(device, "address", "") or "")
    rssi_raw = _attribute(device, "rssi", None)
    if rssi_raw is None:
        advertisement = _attribute(device, "advertisement_data", None)
        rssi_raw = _attribute(advertisement, "rssi", None)
    try:
        rssi = int(rssi_raw) if rssi_raw is not None else None
    except (TypeError, ValueError):
        rssi = None
    return DiscoveredLight(name=name, address=address, rssi=rssi, raw=device)


def _looks_like_neewer(light: DiscoveredLight) -> bool:
    name = light.name.upper()
    return any(token in name for token in ("NEEWER", "RGB660", "RGB 660", "NW-", "ZN-"))


class LightAdapter(DeviceAdapter):
    """Non-blocking Neewer adapter with serialized commands and reconnects."""

    status_changed = pyqtSignal(object)
    devices_discovered = pyqtSignal(object)

    def __init__(
        self,
        name: str,
        address: str = "",
        excluded_addresses: Callable[[], set[str]] | None = None,
        *,
        auto_reconnect: bool = True,
    ) -> None:
        super().__init__(name)
        self.address = address
        self._excluded_addresses = excluded_addresses or set
        self._auto_reconnect = auto_reconnect
        self._light: Any = None
        self.status = LightStatus(address=address or "–")
        self._desired_connection = False
        self._operation_task: asyncio.Task[Any] | None = None
        self._reconnect_task: asyncio.Task[Any] | None = None
        self._command_busy = False
        self._last_command_started_at = 0.0
        self._monitor = QTimer(self)
        self._monitor.setInterval(2000)
        self._monitor.timeout.connect(self._check_connection)
        self._monitor.start()

    def _start_task(self, coroutine: Awaitable[Any]) -> asyncio.Task[Any] | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            self._emit_error("No running Qt/asyncio event loop is available for Bluetooth")
            return None
        return loop.create_task(coroutine)

    def connect_device(self) -> None:
        if self.state in {
            ConnectionState.DISCOVERING,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
        }:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        self._desired_connection = True
        self._operation_task = self._start_task(self._discover_and_connect())

    async def connect_async(self) -> None:
        if self.state in {
            ConnectionState.DISCOVERING,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
        }:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        self._desired_connection = True
        await self._discover_and_connect()

    async def _scan(self) -> list[DiscoveredLight]:
        from neewerlite import NeewerScanner

        try:
            raw_devices = await NeewerScanner.scan(timeout=5.0)
        except TypeError:
            raw_devices = await NeewerScanner.scan()
        return [_as_discovered(device) for device in raw_devices]

    def _select(self, devices: list[DiscoveredLight]) -> DiscoveredLight:
        excluded = {address.casefold() for address in self._excluded_addresses() if address}
        if self.address:
            for device in devices:
                if (
                    device.address.casefold() == self.address.casefold()
                    and device.address.casefold() not in excluded
                ):
                    return device
        supported = [
            device
            for device in devices
            if _looks_like_neewer(device) and device.address.casefold() not in excluded
        ]
        if not supported:
            raise RuntimeError(
                "No matching RGB660/NEEWER light found. Turn on the panel, enable "
                "its Bluetooth icon, and disconnect the smartphone app."
            )
        return sorted(supported, key=lambda item: item.rssi or -999, reverse=True)[0]

    async def _discover_and_connect(self) -> None:
        try:
            self._set_state(ConnectionState.DISCOVERING, "Scanning for BLE panels")
            devices = await self._scan()
            self.devices_discovered.emit(devices)
            selected = self._select(devices)
            await self._connect_selected(selected)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit_error(self._connection_error_text(exc))
            self._schedule_reconnect()

    @staticmethod
    def _connection_error_text(exc: Exception) -> str:
        detail = str(exc) or type(exc).__name__
        if "Device with address" in detail and "was not found" in detail:
            return (
                "The panel was discovered, but Windows could not open its Bluetooth "
                "GATT connection. Close other light apps, disconnect the smartphone "
                "app, and power-cycle the panel before retrying. Details: " + detail
            )
        return detail

    async def _connect_selected(self, selected: DiscoveredLight) -> None:
        from neewerlite import NeewerLight

        self._set_state(
            ConnectionState.CONNECTING,
            f"Connecting {selected.name} ({selected.address})",
        )
        profile_name = "RGB660" if "660" in selected.name.upper() else selected.name
        connection_target = selected.raw if selected.raw is not None else selected.address
        light = NeewerLight(connection_target, name=profile_name)
        try:
            await light.connect()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await light.disconnect()
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await light.disconnect()
            raise
        self._light = light
        self._command_busy = False
        self.address = selected.address
        self.status.name = selected.name
        self.status.address = selected.address
        self.status.rssi = selected.rssi
        self.status.connected = True
        self.status.power = _attribute(light, "is_on", None)
        self.status.capabilities = LightCapabilities()
        self.status.values_are_confirmed_commands = False
        self.status_changed.emit(self.status)
        self._set_state(ConnectionState.CONNECTED, selected.address)

    def _check_connection(self) -> None:
        if self._light is None or self.state is not ConnectionState.CONNECTED:
            return
        client = _attribute(self._light, "client", None)
        connected = bool(client is not None and _attribute(client, "is_connected", False))
        if not connected:
            self.status.connected = False
            self.status.values_are_confirmed_commands = False
            self.status_changed.emit(self.status)
            self._light = None
            self._emit_error("Bluetooth connection to the panel was interrupted")
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if not self._desired_connection or not self._auto_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._start_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 1.0
        while self._desired_connection and self._light is None:
            await asyncio.sleep(delay)
            try:
                devices = await self._scan()
                selected = self._select(devices)
                await self._connect_selected(selected)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit_error(f"Reconnect failed: {exc}")
                delay = min(delay * 2.0, 30.0)

    def set_cct(self, brightness: int, kelvin: int) -> None:
        brightness = max(0, min(100, int(brightness)))
        kelvin = max(3200, min(5600, int(kelvin)))

        async def command() -> None:
            await self._light.set_cct(kelvin, brightness, gm=50)
            self.status.mode = "CCT"
            self.status.brightness = brightness
            self.status.cct_kelvin = kelvin

        self._run_command(command)

    def set_hsi(self, brightness: int, hue: int, saturation: int) -> None:
        brightness = max(0, min(100, int(brightness)))
        hue = max(0, min(360, int(hue)))
        saturation = max(0, min(100, int(saturation)))

        async def command() -> None:
            await self._light.set_rgb(hue, saturation, brightness)
            self.status.mode = "HSI"
            self.status.brightness = brightness
            self.status.hue = hue
            self.status.saturation = saturation

        self._run_command(command)

    def set_power(self, enabled: bool) -> None:
        async def command() -> None:
            method = self._light.turn_on if enabled else self._light.turn_off
            await method()
            self.status.power = enabled

        self._run_command(command)

    def _run_command(self, operation: Callable[[], Awaitable[None]]) -> bool:
        if self._light is None or self.state is not ConnectionState.CONNECTED:
            self._emit_error("Light command rejected because the panel is not connected")
            return False
        if self._command_busy:
            self._emit_error("Light command rejected because another command is still running")
            return False
        self._command_busy = True
        self._last_command_started_at = time.monotonic()

        async def execute() -> None:
            try:
                await asyncio.wait_for(operation(), timeout=LIGHT_COMMAND_TIMEOUT_SECONDS)
                self.status.values_are_confirmed_commands = True
                self.status.last_command_confirmed_at = time.time()
                self.status.last_command_duration_ms = (
                    time.monotonic() - self._last_command_started_at
                ) * 1000.0
                self.status_changed.emit(self.status)
            except Exception as exc:
                failed_light, self._light = self._light, None
                if failed_light is not None:
                    with contextlib.suppress(Exception):
                        await failed_light.disconnect()
                self.status.connected = False
                self.status.values_are_confirmed_commands = False
                self.status_changed.emit(self.status)
                if isinstance(exc, TimeoutError):
                    self._emit_error(
                        f"Light command timed out after {LIGHT_COMMAND_TIMEOUT_SECONDS:g} "
                        "seconds; reconnecting Bluetooth"
                    )
                else:
                    self._emit_error(f"Light command failed: {exc}")
                self._schedule_reconnect()
            finally:
                self._command_busy = False

        self._operation_task = self._start_task(execute())
        if self._operation_task is None:
            self._command_busy = False
            return False
        return True

    def disconnect_device(self) -> None:
        self._desired_connection = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None
        if self._operation_task is not None and not self._operation_task.done():
            self._operation_task.cancel()
        self._command_busy = False
        self._operation_task = self._start_task(self.disconnect_async())

    async def disconnect_async(self) -> None:
        light, self._light = self._light, None
        if light is not None:
            with contextlib.suppress(Exception):
                await light.disconnect()
        self.status.connected = False
        self.status.values_are_confirmed_commands = False
        self._command_busy = False
        self.status_changed.emit(self.status)
        self._set_state(ConnectionState.DISCONNECTED)

    async def shutdown(self) -> None:
        self._desired_connection = False
        tasks = [
            task
            for task in (self._operation_task, self._reconnect_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=1.0)
            if pending:
                LOGGER.error("%s BLE task(s) did not stop during shutdown", len(pending))
        self._operation_task = None
        self._reconnect_task = None
        disconnect_task = asyncio.create_task(self.disconnect_async())
        _done, pending = await asyncio.wait({disconnect_task}, timeout=1.0)
        if pending:
            disconnect_task.cancel()
            self.status.connected = False
            self._set_state(ConnectionState.DISCONNECTED, "Forced local disconnect")
