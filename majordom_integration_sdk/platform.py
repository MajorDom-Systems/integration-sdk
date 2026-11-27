"""The host every peripheral runs on, as its ``self.platform``: the facts it knows and its resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.schemas.credentials import WifiCredentials
from majordom_integration_sdk.schemas.device import DeviceState
from majordom_integration_sdk.thread import dataset_channel

if TYPE_CHECKING:
    from majordom_integration_sdk.peripherals import Peripheral

type MakeDeviceRepository = Callable[[str], AbstractAsyncContextManager[DeviceRepositoryProtocol]]
type CheckDeviceId = Callable[[UUID, DeviceState], Awaitable[None]]
type AdoptThreadNetwork = Callable[[], Awaitable[None]]
# Where the Thread network came from: formed by a border router of the Hub's, or imported from the user's (iOS)
type ThreadOrigin = Literal["own", "imported"]

NETWORK_FORGOTTEN = ".network-forgotten"  # in a peripheral's folder: it forgot its radio's network since the reset


def _in_memory() -> MakeDeviceRepository:
    store: dict[UUID, dict[str, Any]] = {}  # shared by the repositories of every peripheral
    return lambda key: DeviceRepositoryMemory(key, store).session()


async def _any_id_is_free(physical_id: UUID, draft: DeviceState):
    pass


async def _no_host_to_adopt():
    raise LookupError("No host keeps a Thread network to adopt")


@dataclass(frozen=True, kw_only=True)
class Platform:
    """The host every peripheral runs on, the same object for all of them as ``self.platform``: a controller is given
    it, ``peripherals(platform)``, and the runtime sets it on the others. It holds the facts the host knows as plain
    fields, and its resources, scoped to the peripheral passed::

        async with self.platform.device_repository(self) as repository:
            ...
        cache = self.platform.documents_folder(self) / "cache.json"

    Facts change only by replacement: the host builds a new platform (``dataclasses.replace``) and gives it to the
    runtime (`Runtime.update_platform`), which points every peripheral at it and tells the ones that run
    (``platform_facts_did_change``). Read ``self.platform`` when you need a fact: a platform kept holds the old ones.
    """

    # Where each peripheral gets a folder of its own, by its key: the Hub's, or a local one under the dev runner
    documents_root: Path
    # One unit of work on the device repository, scoped to the devices of the peripheral keyed; in memory by default
    make_device_repository: MakeDeviceRepository = field(default_factory=_in_memory)
    # Raises `DeviceIdTakenError` when a record other than the one ``draft`` pairs onto (``draft.id``) holds the
    # physical id; the Hub's checks every record. Library mode has no other records to collide with: every id is free
    check_device_id: CheckDeviceId = _any_id_is_free
    region: str | None = None  # RF region for sub-GHz radios, a Z-Wave JS name: "Europe", "USA", …
    wifi: WifiCredentials | None = None  # the network to provision Wi-Fi devices onto
    thread_dataset: str | None = None  # active operational dataset of the hub's Thread network, hex TLVs
    # The host was factory reset: a network a radio keeps in its own memory (a Z-Wave controller's nodes and home ID,
    # an EZSP coordinator's network) belongs to the Hub before it, whose keys are gone
    factory_reset: bool = False
    # A multi-PAN radio's border router moves its own network to Zigbee's channel; an imported one (other ecosystems'
    # devices on it) stays, until the user lets the Hub move it: the host adopts it then (``adopt_thread_network``)
    thread_origin: ThreadOrigin = "own"
    adopt_thread_network: AdoptThreadNetwork = _no_host_to_adopt

    @property
    def thread_channel(self) -> int | None:
        """The channel of the hub's Thread network, from its dataset."""
        return None if self.thread_dataset is None else dataset_channel(self.thread_dataset)

    def documents_folder(self, peripheral: Peripheral) -> Path:
        """The folder of ``peripheral``, by its key, created on use: what it keeps across restarts that doesn't belong
        in the device database (certificates, protocol caches, a container's data)."""
        folder = self.documents_root / peripheral.key
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def device_repository(self, peripheral: Peripheral) -> AbstractAsyncContextManager[DeviceRepositoryProtocol]:
        """One unit of work on the device repository, scoped to the devices of ``peripheral``, by its key: a
        controller's is its slug, the ``integration`` of every `Device` it keeps."""
        return self.make_device_repository(peripheral.key)

    def forgets_network(self, peripheral: Peripheral) -> bool:
        """Whether ``peripheral`` is to forget the network its radio keeps in its own memory and start a new one: once
        after a factory reset, until it tells it did (`network_forgotten`)."""
        return self.factory_reset and not (self.documents_folder(peripheral) / NETWORK_FORGOTTEN).exists()

    def network_forgotten(self, peripheral: Peripheral):
        """``peripheral``'s radio forgot its network: it keeps the one it starts from now on, the next reset aside."""
        (self.documents_folder(peripheral) / NETWORK_FORGOTTEN).touch()
