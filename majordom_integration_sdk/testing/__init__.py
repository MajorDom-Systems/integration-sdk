"""Shared test doubles for integration test suites.

Both the Hub's own integration tests and third-party ``integration-*`` packages import these
instead of reinventing them, so every integration is exercised through the same harness:

- :class:`RecordingControllerOutput` — a spy implementing every ``ControllerOutput``
  callback, capturing what a controller reports back to the Hub.
- Offline doubles of every capability (no radios/sockets touched): :class:`FakeBLESource` for the
  Bluetooth router to hear through, :class:`FakeBLERouter`, :class:`FakeDNSSD` (the ``discovery``
  extra) and :class:`FakeSSDP`. Each offers itself as ``.endpoint``, to pass in the controller's
  peripherals: ``Controller(platform, {"ble": router.endpoint})``. :class:`FakeOffers` takes
  what an integration offers, lending its Bluetooth sources to a router as the runtime would.
- :func:`build_test_platform` — assembles a controller ``Platform`` wired with the recording
  output, an in-memory device repository and temp-dir storage, so a test can drive a controller
  exactly as the Hub would.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from majordom_integration_sdk.capabilities.ble import BLEConnection, OnAdvertisement
from majordom_integration_sdk.controller.output import ControllerOutput, IntegrationError
from majordom_integration_sdk.controller.platform import Platform
from majordom_integration_sdk.peripherals import Endpoint, PeripheralChange
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.ssdp import SSDPDiscoveryService
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.abstract_controller import AbstractController as _Controller


class RecordingControllerOutput(ControllerOutput):
    """Captures every callback a controller makes, for assertions in tests."""

    def __init__(self):
        self.received_discoveries: list[Discovery] = []
        self.updated_discoveries: list[Discovery] = []
        self.lost_discoveries: list[UUID] = []
        self.connected_devices: list[UUID] = []
        self.lost_devices: list[UUID] = []
        self.events: list[Event] = []
        self.errors: dict[str, IntegrationError] = {}  # the active ones, by id
        self.error_log: list[IntegrationError | str] = []  # every report, and the id of every resolve
        self.notifications: list[Notification] = []

    async def controller_did_receive_discovery(self, controller: _Controller, discovery: Discovery):
        self.received_discoveries.append(discovery)

    async def controller_did_update_discovery(self, controller: _Controller, discovery: Discovery):
        self.updated_discoveries.append(discovery)

    async def controller_did_lose_discovery(self, controller: _Controller, discovery_id: UUID):
        self.lost_discoveries.append(discovery_id)

    async def controller_did_connect_device(self, controller: _Controller, device_id: UUID):
        self.connected_devices.append(device_id)

    async def controller_did_lose_device(self, controller: _Controller, device_id: UUID):
        self.lost_devices.append(device_id)

    async def controller_did_receive_events(self, controller: _Controller, events: Iterable[Event]):
        self.events.extend(events)

    async def controller_did_encounter_error(self, controller: _Controller, error: IntegrationError) -> None:
        self.errors[error.id] = error
        self.error_log.append(error)

    async def controller_did_resolve_error(self, controller: _Controller, error_id: str) -> None:
        self.errors.pop(error_id, None)
        self.error_log.append(error_id)

    async def controller_did_emit_notification(self, controller: _Controller, notification: Notification):
        self.notifications.append(notification)


class FakeSSDP(SSDPDiscoveryService):
    """Real registration bookkeeping, but ``start``/``stop`` touch no network."""

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeBLERouter(BLEService):
    """A router with no radio unless given `FakeBLESource`s; ``start``/``stop`` run no background tasks."""

    async def start(self):
        self._is_running = True
        await self._start_sources()

    async def stop(self):
        self._is_running = False
        await self._stop_sources()


class FakeOffers:
    """What an integration offers, recorded; with a ``router``, offered ``ble`` sources are lent to it as the runtime
    binds them. Use as ``platform.copy(offers=FakeOffers(router))``."""

    def __init__(self, router: BLEService | None = None):
        self.offered: list[Endpoint] = []
        self._router = router

    async def offer(self, endpoint: Endpoint) -> None:
        await self._change(
            [*(e for e in self.offered if (e.family, e.url) != (endpoint.family, endpoint.url)), endpoint]
        )

    async def withdraw(self, endpoint: Endpoint) -> None:
        await self._change([e for e in self.offered if (e.family, e.url) != (endpoint.family, endpoint.url)])

    async def _change(self, offered: list[Endpoint]):
        before, self.offered = self.offered, offered
        if self._router:
            lent = [[e for e in endpoints if e.family == "ble"] for endpoints in (before, offered)]
            await self._router.peripheral_did_change(PeripheralChange("ble", *lent))


def __getattr__(name: str):
    if name == "FakeDNSSD":  # the discovery extra (zeroconf): only for the suites that use it
        from majordom_integration_sdk.runtime.peripherals.providers.dns_sd import ZeroconfDiscoveryService

        class FakeDNSSD(ZeroconfDiscoveryService):
            """Real registration bookkeeping, but ``start``/``stop`` touch no network."""

            async def start(self):
                pass

            async def stop(self):
                pass

        globals()[name] = FakeDNSSD
        return FakeDNSSD
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class FakeBLESource:
    """A Bluetooth radio for tests: ``advertise`` feeds the service it serves, ``connect`` returns
    ``connection`` (or fails when there is none). Use with ``BLEService(sources=[...])``."""

    def __init__(
        self,
        name: str = "fake",
        connection: BLEConnection | None = None,
        can_pair: bool = False,
        free_slots: int | None = None,
    ):
        self.name = name
        self.connection = connection
        self.can_pair = can_pair
        self.free_slots = free_slots
        self.on_advertisement: OnAdvertisement | None = None
        self.connected: list[str] = []

    async def start(self, on_advertisement: OnAdvertisement):
        self.on_advertisement = on_advertisement

    async def stop(self):
        self.on_advertisement = None

    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection:
        if self.connection is None:
            raise ConnectionError(f"{self.name} can't reach {address}")
        self.connected.append(address)
        return self.connection

    def advertise(
        self,
        address: str,
        rssi: int = -60,
        local_name: str | None = None,
        service_uuids: list[str] | None = None,
        service_data: dict[str, bytes] | None = None,
        manufacturer_data: dict[int, bytes] | None = None,
    ):
        from bleak.backends.device import BLEDevice  # the ble extra
        from bleak.backends.scanner import AdvertisementData

        assert self.on_advertisement, f"{self.name} isn't listening"
        adv = AdvertisementData(
            local_name, manufacturer_data or {}, service_data or {}, service_uuids or [], None, rssi, ()
        )
        self.on_advertisement(BLEDevice(address, local_name, self.name), adv)  # details: the radio


def build_test_platform(
    documents_folder: Path | None = None,
    integration: str | None = None,
) -> Platform:
    """Assemble a controller ``Platform`` wired entirely with offline test doubles.

    Pass ``documents_folder`` to control where the controller writes files; a fresh temp dir
    is used otherwise. Pass ``integration`` to scope the in-memory repository to that
    integration's devices (access-control guard), mirroring how the Hub injects it. The
    recording output is reachable as ``platform.output``.
    """
    repository = DeviceRepositoryMemory(integration=integration)
    folder = documents_folder or Path(tempfile.mkdtemp(prefix="majordom-sdk-test-"))
    folder.mkdir(parents=True, exist_ok=True)
    return Platform(
        output=RecordingControllerOutput(), make_device_repository=repository.session, documents_folder=folder
    )


__all__ = [
    "RecordingControllerOutput",
    "FakeBLESource",
    "FakeBLERouter",
    "FakeDNSSD",
    "FakeSSDP",
    "FakeOffers",
    "build_test_platform",
]
