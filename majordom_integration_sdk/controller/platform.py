"""What the Hub (or the standalone runner) provides every controller besides its peripherals: the app's services."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from majordom_integration_sdk.controller.output import ControllerOutput
from majordom_integration_sdk.peripherals import Endpoint
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.schemas.credentials import WifiCredentials


class ServiceControl(Protocol):
    """What an integration may do to the services it brings (its ``services``), through the runtime."""

    async def restart(self, service: str) -> None:
        """Restart one of them, e.g. after reconfiguring it. The integration keeps running: reconnect."""
        ...


@dataclass(frozen=True)
class NoServiceControl:
    """No runtime runs services here: library mode, the standalone runner without the runtime."""

    async def restart(self, service: str) -> None:
        raise NotImplementedError(f"Nothing runs {service} here: restart it yourself")


class Offers(Protocol):
    """Endpoints an integration offers others while it runs, e.g. ESPHome's Bluetooth proxies as ``ble/esphome-proxy``
    sources of the Bluetooth router. The runtime binds them like any other and tells whoever gets or loses one."""

    async def offer(self, endpoint: Endpoint) -> None: ...
    async def withdraw(self, endpoint: Endpoint) -> None: ...


@dataclass(frozen=True)
class NoOffers:
    """No runtime binds offers here: nothing takes them."""

    async def offer(self, endpoint: Endpoint) -> None:
        pass

    async def withdraw(self, endpoint: Endpoint) -> None:
        pass


@dataclass(frozen=True)
class Context:
    """Hub-wide facts an integration may need to set devices up."""

    region: str | None = None  # RF region for sub-GHz radios, a Z-Wave JS name: "Europe", "USA", …
    wifi: WifiCredentials | None = None  # the network to provision Wi-Fi devices onto
    thread_dataset: str | None = None  # active operational dataset of the hub's Thread network, hex TLVs


@dataclass(frozen=True)
class Platform:
    """The app's services injected by the Hub into every controller on construction, as ``self.platform``."""

    output: ControllerOutput
    make_device_repository: Callable[[], AbstractAsyncContextManager[DeviceRepositoryProtocol]]
    # The directory this integration may write files into that don't belong in the
    # device database (certificates, protocol caches, …). The Hub computes and injects
    # it per integration; the standalone dev runner points it at a local folder.
    documents_folder: Path

    settings: BaseModel | None = None  # validated instance of the controller's `settings` model
    context: Context = field(default_factory=Context)
    services: ServiceControl = field(default_factory=NoServiceControl)  # restarts the integration's `services`
    offers: Offers = field(default_factory=NoOffers)  # endpoints the integration lends others while it runs

    def copy(self, **kwargs) -> Platform:
        return replace(self, **kwargs)
