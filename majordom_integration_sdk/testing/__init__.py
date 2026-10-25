"""Shared test doubles for integration test suites.

Both the Hub's own integration tests and third-party ``integration-*`` packages import these
instead of reinventing them, so every integration is exercised through the same harness:

- :class:`RecordingControllerOutput` — a spy implementing every ``ControllerOutput``
  callback, capturing what a controller reports back to the Hub.
- :class:`FakePeripheral` — a peripheral that records how it is run: what it needs and provides (endpoints
  and the peripherals it brings along) is set per test, its starts can fail, its hooks answer as told.
- :class:`RecordingPeripheralOutput` — what a peripheral reports to the runtime; :class:`RecordingRuntimeOutput` —
  what the runtime reports to the Hub.
- :class:`FakeCompose` — the container host without Docker, for the services an integration provides.
- Offline doubles of every capability (no radios/sockets touched): :class:`FakeBLESource` for the
  Bluetooth router to hear through (``router.add_source(source)``), :class:`FakeBLERouter`,
  :class:`FakeZeroconf` (the ``discovery`` extra) and :class:`FakeSSDP`. Each provides itself as
  ``.endpoint``, to bind as the controller's peripherals: ``Peripherals({"ble": router.endpoint})``.
- :func:`build_test_platform` — assembles a controller ``Platform`` wired with the recording
  output, an in-memory device repository and temp-dir storage, so a test can drive a controller
  exactly as the Hub would.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from majordom_integration_sdk.capabilities.ble import BLEConnection, OnAdvertisement
from majordom_integration_sdk.controller.output import ControllerOutput, IntegrationError
from majordom_integration_sdk.controller.platform import Context, Platform
from majordom_integration_sdk.peripherals import Endpoint, Need, Peripheral, PeripheralChange, Service
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.ssdp import SSDPDiscoveryService
from majordom_integration_sdk.runtime.peripherals.services.compose import Compose
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.abstract_controller import AbstractController as _Controller
    from majordom_integration_sdk.runtime import PeripheralStatus


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

    async def controller_did_encounter_error(self, controller: _Controller, error: IntegrationError):
        self.errors[error.id] = error
        self.error_log.append(error)

    async def controller_did_resolve_error(self, controller: _Controller, error_id: str):
        self.errors.pop(error_id, None)
        self.error_log.append(error_id)

    async def controller_did_emit_notification(self, controller: _Controller, notification: Notification):
        self.notifications.append(notification)


class FakePeripheral(Peripheral):
    """A peripheral that records how it is run (``calls``: "start", "stop") and the changes it hears. Its first
    ``failures`` starts raise; ``answer`` is what its hooks return, or an exception they raise. Change ``provided``,
    then tell ``runtime``, as a real one does when a stick is plugged in."""

    def __init__(
        self,
        name: str,
        needs: Sequence[Need] = (),
        provides: Sequence[Endpoint | Peripheral] = (),
        *,
        is_on_demand: bool = False,
        failures: int = 0,
        answer: bool | Exception = False,
        settings: type | None = None,
    ):
        self.name, self.needs, self.provided = name, list(needs), list(provides)
        self.is_on_demand = is_on_demand
        self.failures, self.answer = failures, answer
        self.Settings = settings
        self.calls: list[str] = []
        self.changes: list[PeripheralChange | tuple[str, Any]] = []  # needs moved; ("settings"|"context", before)

    @property
    def provides(self) -> Sequence[Endpoint | Peripheral]:
        return self.provided

    async def start(self):
        self.calls.append("start")
        if self.failures:
            self.failures -= 1
            raise RuntimeError(f"{self.name} broke")

    async def stop(self):
        self.calls.append("stop")

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        self.changes.append(change)
        return self._answer()

    async def settings_did_change(self, before: Any) -> bool:
        self.changes.append(("settings", before))
        return self._answer()

    async def context_did_change(self, before: Any) -> bool:
        self.changes.append(("context", before))
        return self._answer()

    def _answer(self) -> bool:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def __repr__(self) -> str:
        return f"FakePeripheral({self.name!r})"


class RecordingPeripheralOutput:
    """Captures what peripherals report to whatever runs them: set it as a peripheral's ``runtime``. It hands over
    ``context``, set it as a test needs."""

    def __init__(self):
        self.context = Context()
        self.changed: list[Peripheral] = []
        self.learned: list[tuple[Peripheral, Mapping[str, Any]]] = []
        self.stopped: list[Peripheral] = []
        self.failed: list[tuple[Peripheral, str]] = []

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        self.changed.append(peripheral)

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        self.learned.append((peripheral, facts))

    async def peripheral_did_stop(self, peripheral: Peripheral):
        self.stopped.append(peripheral)

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        self.failed.append((peripheral, reason))


class RecordingRuntimeOutput:
    """Captures what a runtime reports to the Hub: every status in order, the last one by name, what was learned, the
    ones gone."""

    def __init__(self):
        self.statuses: list[tuple[str, PeripheralStatus]] = []
        self.learned: list[tuple[str, Mapping[str, Any]]] = []
        self.disappeared: list[Peripheral] = []

    def peripheral_did_change_status(self, peripheral: Peripheral, status: PeripheralStatus):
        self.statuses.append((peripheral.name, status))

    def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        self.learned.append((peripheral.name, facts))

    def peripheral_did_disappear(self, peripheral: Peripheral):
        self.disappeared.append(peripheral)

    def last(self, name: str) -> PeripheralStatus | None:
        return next((status for n, status in reversed(self.statuses) if n == name), None)


class FakeCompose(Compose):
    """Docker Compose without Docker: it renders, keeps ports and writes ``compose.json`` as the real one, and a
    container runs and answers once applied. ``calls`` are the docker commands it would have run."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, ...]] = []

    async def _docker(self, *args: str) -> str:
        self.calls.append(args)
        return "\n".join(self._running) if "ps" in args else ""

    async def _is_up(self, service: Service) -> bool:
        return service.name in self._running


class FakeSSDP(SSDPDiscoveryService):
    """Real registration bookkeeping, but ``start``/``stop`` touch no network."""

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeBLERouter(BLEService):
    """A router with no radio unless given `FakeBLESource`s; ``start``/``stop`` run no background tasks."""

    async def start(self):
        await self._follow_bound()
        self._is_running = True
        await self._start_sources()

    async def stop(self):
        self._is_running = False
        await self._stop_sources()


def __getattr__(name: str):
    if name == "FakeZeroconf":  # the discovery extra (zeroconf): only for the suites that use it
        from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService

        class FakeZeroconf(ZeroconfDiscoveryService):
            """Real registration bookkeeping, but ``start``/``stop`` touch no network."""

            async def start(self):
                pass

            async def stop(self):
                pass

        globals()[name] = FakeZeroconf
        return FakeZeroconf
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class FakeBLESource:
    """A Bluetooth radio for tests: ``advertise`` feeds the service it serves, ``connect`` returns
    ``connection`` (or fails when there is none). Use with ``BLEService(sources=[...])``."""

    def __init__(
        self,
        name: str = "fake",
        connection: BLEConnection | None = None,
        is_pairable: bool = False,
        free_slots: int | None = None,
    ):
        self.name = name
        self.connection = connection
        self.is_pairable = is_pairable
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
    "FakePeripheral",
    "RecordingPeripheralOutput",
    "RecordingRuntimeOutput",
    "FakeBLESource",
    "FakeBLERouter",
    "FakeZeroconf",
    "FakeSSDP",
    "FakeCompose",
    "build_test_platform",
]
