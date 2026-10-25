"""What the Hub (or the standalone runner) provides every controller: the app's services and Hub-wide facts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path

from majordom_integration_sdk.controller.output import ControllerOutput
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.schemas.credentials import WifiCredentials


@dataclass(frozen=True)
class Context:
    """Hub-wide facts an integration may need to set devices up."""

    region: str | None = None  # RF region for sub-GHz radios, a Z-Wave JS name: "Europe", "USA", …
    wifi: WifiCredentials | None = None  # the network to provision Wi-Fi devices onto
    thread_dataset: str | None = None  # active operational dataset of the hub's Thread network, hex TLVs


@dataclass(frozen=True)
class Platform:
    """The app's services the Hub gives every integration alike, ``peripherals(platform)``: its controller's
    ``self.platform``, which the controller scopes to itself (`AbstractController.device_repository`,
    `AbstractController.documents_folder`)."""

    output: ControllerOutput
    # One unit of work on the device repository, scoped to the devices of the integration named
    make_device_repository: Callable[[str], AbstractAsyncContextManager[DeviceRepositoryProtocol]]
    # Where each integration gets a folder of its own, by its slug, for files that don't belong in the device
    # database (certificates, protocol caches, …): the Hub's, or a local one under the standalone dev runner.
    documents_folder: Path
    context: Context = field(default_factory=Context)  # kept current by the Hub, which merges what the runtime learns

    def copy(self, **kwargs) -> Platform:
        return replace(self, **kwargs)
