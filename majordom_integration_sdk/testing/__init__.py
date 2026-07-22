"""Shared test doubles for integration test suites.

Both the Hub's own integration tests and third-party ``integration-*`` packages import these
instead of reinventing them, so every integration is exercised through the same harness:

- :class:`RecordingControllerOutput` — a spy implementing every ``ControllerOutput``
  callback, capturing what a controller reports back to the Hub.
- Offline discovery-service doubles (no radios/sockets touched).
- :func:`build_test_dependencies` — assembles an ``AbstractController.Dependencies`` wired
  with the recording output, an in-memory device repository, temp-dir storage, and the
  discovery doubles, so a test can drive a controller exactly as the Hub would.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from majordom_integration_sdk.controller.abstract_controller import AbstractController, ControllerOutput
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryService
from majordom_integration_sdk.discovery.ssdp_discovery import SSDPDiscoveryService
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryService
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event

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
        self.errors: list[tuple[str, bool]] = []  # (message, still_running)

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

    async def controller_did_encounter_error(self, controller: _Controller, message: str, still_running: bool):
        self.errors.append((message, still_running))


class FakeZeroconfDiscoveryService(ZeroconfDiscoveryService):
    """Real registration bookkeeping, but ``start``/``stop`` touch no network."""

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeSSDPDiscoveryService(SSDPDiscoveryService):
    async def start(self):
        pass

    async def stop(self):
        pass


class FakeBLEDiscoveryService(BLEDiscoveryService):
    async def start(self):
        pass

    async def stop(self):
        pass


def build_test_dependencies(
    documents_folder: Path | None = None,
    integration: str | None = None,
) -> AbstractController.Dependencies:
    """Assemble controller ``Dependencies`` wired entirely with offline test doubles.

    Pass ``documents_folder`` to control where the controller writes files; a fresh temp dir
    is used otherwise. Pass ``integration`` to scope the in-memory repository to that
    integration's devices (access-control guard), mirroring how the Hub injects it. The
    recording output is reachable as ``deps.output``.
    """
    repository = DeviceRepositoryMemory(integration=integration)
    folder = documents_folder or Path(tempfile.mkdtemp(prefix="majordom-sdk-test-"))
    folder.mkdir(parents=True, exist_ok=True)
    return AbstractController.Dependencies(
        output=RecordingControllerOutput(),
        make_device_repository=repository.session,
        documents_folder=folder,
        zeroconf_discovery_service=FakeZeroconfDiscoveryService(),
        ssdp_discovery_service=FakeSSDPDiscoveryService(),
        ble_discovery_service=FakeBLEDiscoveryService(),
    )


__all__ = [
    "RecordingControllerOutput",
    "FakeZeroconfDiscoveryService",
    "FakeSSDPDiscoveryService",
    "FakeBLEDiscoveryService",
    "build_test_dependencies",
]
