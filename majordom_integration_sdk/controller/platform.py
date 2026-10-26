"""The host every peripheral runs on, as its ``self.platform``: the facts it knows and its resources."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from majordom_integration_sdk.controller.output import ControllerOutput, LoggingControllerOutput
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.schemas.credentials import WifiCredentials

if TYPE_CHECKING:
    from majordom_integration_sdk.peripherals import Peripheral

type MakeDeviceRepository = Callable[[str], AbstractAsyncContextManager[DeviceRepositoryProtocol]]


def _in_memory() -> MakeDeviceRepository:
    store: dict = {}  # shared by the repositories of every peripheral
    return lambda key: DeviceRepositoryMemory(key, store).session()


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
    output: ControllerOutput = field(default_factory=LoggingControllerOutput)
    region: str | None = None  # RF region for sub-GHz radios, a Z-Wave JS name: "Europe", "USA", …
    wifi: WifiCredentials | None = None  # the network to provision Wi-Fi devices onto
    thread_dataset: str | None = None  # active operational dataset of the hub's Thread network, hex TLVs

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
