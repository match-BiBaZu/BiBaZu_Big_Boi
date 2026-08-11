from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

import bibazu_reorientation.hardware.light as light_module
from bibazu_reorientation.hardware.light import LightAdapter
from bibazu_reorientation.models import ConnectionState, DiscoveredLight


class FakeScanner:
    devices = [SimpleNamespace(name="NEEWER-RGB660", address="AA:BB", rssi=-40)]

    @classmethod
    async def scan(cls, timeout: float = 5.0):
        return cls.devices


class FakeLight:
    instances: list[FakeLight] = []

    def __init__(self, target, name: str = "") -> None:
        self.target = target
        self.name = name
        self.client = SimpleNamespace(is_connected=False)
        self.is_on = True
        self.commands: list[tuple[object, ...]] = []
        self.instances.append(self)

    async def connect(self) -> None:
        self.client.is_connected = True

    async def disconnect(self) -> None:
        self.client.is_connected = False

    async def turn_on(self) -> None:
        self.commands.append(("power", True))

    async def turn_off(self) -> None:
        self.commands.append(("power", False))

    async def set_cct(self, kelvin: int, brightness: int, gm: int = 50) -> None:
        self.commands.append(("cct", kelvin, brightness, gm))

    async def set_rgb(self, hue: int, saturation: int, brightness: int) -> None:
        self.commands.append(("hsi", hue, saturation, brightness))


async def wait_for(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_connect_uses_discovered_device_and_does_not_change_output(
    qtbot, monkeypatch
) -> None:
    FakeLight.instances.clear()
    monkeypatch.setitem(
        sys.modules,
        "neewerlite",
        SimpleNamespace(NeewerScanner=FakeScanner, NeewerLight=FakeLight),
    )
    adapter = LightAdapter("Panel", auto_reconnect=False)

    await adapter.connect_async()

    light = FakeLight.instances[-1]
    assert adapter.state is ConnectionState.CONNECTED
    assert light.target is FakeScanner.devices[0]
    assert light.commands == []
    assert adapter.status.power is True
    await adapter.shutdown()


def test_stale_preferred_address_falls_back_but_excludes_other_panel() -> None:
    adapter = LightAdapter(
        "Panel",
        "STALE",
        excluded_addresses=lambda: {"AA:01"},
        auto_reconnect=False,
    )
    devices = [
        DiscoveredLight("NEEWER-RGB660 PRO", "AA:01", -20),
        DiscoveredLight("NEEWER-RGB660 PRO", "AA:02", -50),
    ]

    assert adapter._select(devices).address == "AA:02"


def test_windows_gatt_cache_error_has_operator_guidance() -> None:
    message = LightAdapter._connection_error_text(
        RuntimeError("Device with address AA:BB was not found.")
    )

    assert "power-cycle the panel" in message
    assert "smartphone app" in message


@pytest.mark.asyncio
async def test_command_timeout_disconnects_panel_instead_of_leaving_stale_connection(
    qtbot, monkeypatch
) -> None:
    class HangingLight(FakeLight):
        async def set_cct(self, kelvin: int, brightness: int, gm: int = 50) -> None:
            await asyncio.sleep(1.0)

    FakeLight.instances.clear()
    monkeypatch.setattr(light_module, "LIGHT_COMMAND_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setitem(
        sys.modules,
        "neewerlite",
        SimpleNamespace(NeewerScanner=FakeScanner, NeewerLight=HangingLight),
    )
    adapter = LightAdapter("Panel", auto_reconnect=False)
    await adapter.connect_async()
    light = FakeLight.instances[-1]

    adapter.set_cct(35, 4200)
    await wait_for(lambda: adapter.state is ConnectionState.ERROR)

    assert not light.client.is_connected
    assert not adapter.status.connected
    assert adapter._light is None
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_connection_and_releases_client(
    qtbot, monkeypatch
) -> None:
    class SlowConnectLight(FakeLight):
        started = asyncio.Event()

        async def connect(self) -> None:
            self.client.is_connected = True
            self.started.set()
            await asyncio.sleep(10.0)

    FakeLight.instances.clear()
    monkeypatch.setitem(
        sys.modules,
        "neewerlite",
        SimpleNamespace(NeewerScanner=FakeScanner, NeewerLight=SlowConnectLight),
    )
    adapter = LightAdapter("Panel", auto_reconnect=False)
    adapter.connect_device()
    await asyncio.wait_for(SlowConnectLight.started.wait(), 1.0)
    light = FakeLight.instances[-1]

    adapter.disconnect_device()
    await wait_for(lambda: adapter.state is ConnectionState.DISCONNECTED)

    assert not light.client.is_connected
    assert not adapter._desired_connection
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_blocking_winrt_connect_does_not_block_gui_event_loop(qtbot, monkeypatch) -> None:
    class BlockingConnectLight(FakeLight):
        async def connect(self) -> None:
            # Models a WinRT call which blocks its event-loop thread internally.
            time.sleep(0.2)
            self.client.is_connected = True

    FakeLight.instances.clear()
    monkeypatch.setitem(
        sys.modules,
        "neewerlite",
        SimpleNamespace(NeewerScanner=FakeScanner, NeewerLight=BlockingConnectLight),
    )
    adapter = LightAdapter("Panel", auto_reconnect=False)

    connection = asyncio.create_task(adapter.connect_async())
    started = time.monotonic()
    await asyncio.sleep(0.03)

    assert time.monotonic() - started < 0.15
    assert not connection.done()
    await asyncio.wait_for(connection, 1.0)
    assert adapter.state is ConnectionState.CONNECTED
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_async_connect_timeout_returns_error(qtbot, monkeypatch) -> None:
    class HangingConnectLight(FakeLight):
        async def connect(self) -> None:
            await asyncio.sleep(10.0)

    FakeLight.instances.clear()
    monkeypatch.setattr(light_module, "LIGHT_CONNECT_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setitem(
        sys.modules,
        "neewerlite",
        SimpleNamespace(NeewerScanner=FakeScanner, NeewerLight=HangingConnectLight),
    )
    adapter = LightAdapter("Panel", auto_reconnect=False)

    await asyncio.wait_for(adapter.connect_async(), 1.0)

    assert adapter.state is ConnectionState.ERROR
    assert not adapter.status.connected
    await adapter.shutdown()
