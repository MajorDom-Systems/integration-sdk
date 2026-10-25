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
    """The app's services the Hub gives each integration, ``peripherals(platform)``: its controller's
    ``self.platform``."""

    output: ControllerOutput
    make_device_repository: Callable[[], AbstractAsyncContextManager[DeviceRepositoryProtocol]]
    # The directory this integration may write files into that don't belong in the
    # device database (certificates, protocol caches, …). The Hub computes and injects
    # it per integration; the standalone dev runner points it at a local folder.
    documents_folder: Path
    context: Context = field(default_factory=Context)  # kept current by the Hub, which merges what the runtime learns

    def copy(self, **kwargs) -> Platform:
        return replace(self, **kwargs)
