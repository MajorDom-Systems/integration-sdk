"""
Entrypoint and lifecycle boundary for an integration's Controller.
See docs.majordom.io/device-integration for the full integration guide.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, final
from uuid import UUID, uuid5

from slugify import slugify

from majordom_integration_sdk.controller.platform import Context, Platform
from majordom_integration_sdk.peripherals import Need, Peripheral, PeripheralChange
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials

_NAME_HELP = (
    "The integration name is auto-derived from your controller class name; set a class-level "
    '`name` only to override it (e.g. `name = "ZigBee"`). '
    "See the example integration at https://docs.majordom.io/device-integration"
)


def _titleize(class_name: str) -> str:
    """Human-readable integration name derived from a controller class name.

    Strips a trailing "Controller" and splits CamelCase/PascalCase into spaced words:
    ``HueController`` -> ``"Hue"``, ``ZigBeeController`` -> ``"Zig Bee"``. Set an explicit
    ``name`` on the subclass to override when you want different casing (e.g. ``"ZigBee"``).
    """
    base = class_name[: -len("Controller")] if class_name.endswith("Controller") else class_name
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", base)
    return " ".join(words) or base


class AbstractController[TDevice: Device, TParameter: Parameter](Peripheral, ABC):
    """
    Base class for all integration controllers.
    Defines the interface the Hub uses to interact with an integration — the Hub instantiates the subclass and drives it by calling these methods.

    A controller is a `Peripheral` like any other: the runtime starts it once its required needs are bound and ready,
    stops it when one is lost, and hands moved needs, settings and context over to its hooks. It holds:

    - ``self.platform``: the app's services from the Hub (output, device repository, documents folder, context);
    - ``self.settings``: its validated ``Settings``, when it declares its options as a nested ``class Settings(BaseModel)``
      (annotate ``settings: Settings`` to type it); None without;
    - ``self.peripherals``: the endpoints its needs are bound to (``endpoint()``, ``endpoints()``, ``instance()``).

    In library mode, with no runtime, hand the bound needs over yourself::

        controller = HueController(platform)
        controller.peripherals = Peripherals({"mdns": Endpoint("mdns", "zeroconf", instance=zeroconf)})
    """

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Auto-derive `name` from the class name unless the subclass set one explicitly.
        if "name" not in cls.__dict__:
            cls.name = _titleize(cls.__name__)

    def __init__(self, platform: Platform):
        """Called by the Hub with the app's services. Do not change this signature."""
        self.platform = platform
        self.settings = self.Settings() if self.Settings is not None else None

    # Abstract

    @property
    @abstractmethod
    def discoveries(self) -> dict[UUID, Discovery]:
        """
        Returns the current set of unpaired, discoverable devices as a cached snapshot.
        The ID for a given physical device must remain stable.
        Do not trigger any scanning here — only return already-cached data.
        """
        # TODO: save datetime and remove expired
        return {}

    name: str
    """
    Human-readable name of the integration (e.g. "HomeKit", "ZigBee").

    Auto-derived from the controller class name by default (``HueController`` -> "Hue",
    ``ZigBeeController`` -> "Zig Bee"), so you usually don't set it. Set it on your subclass
    only to override the casing/wording:

        class ZigBeeController(AbstractController[Device, Parameter]):
            name = "ZigBee"

    It identifies the integration, so it's a constant of the class rather than per-instance
    state — the Hub reads it (and `slug()`) off the class to wire an integration's
    peripherals and platform *before* the controller exists.
    """

    needs: Sequence[Need] = ()
    """What the integration requires from its environment: everything external, which something else must provide —
    radios (``Need("zigbee", ["ezsp"])``), the Bluetooth router (``Need("ble", ["router"])``), DNS-SD
    (``Need("mdns", ["zeroconf"])``), SSDP (``Need("upnp", ["ssdp"])``) and the services its integration provides next
    to it. Bound ones arrive in ``self.peripherals`` keyed by family; in library mode hand them over yourself. A
    required need that can't be bound keeps the integration from starting, with the reason the Hub shows. Never need
    raw Bluetooth: the router holds every radio."""

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """A bound need was lost, appeared, or moved; ``self.peripherals`` is already updated.

        Return True when handled in place (e.g. Matter keeps working over IP without Thread).
        False (the default) restarts the integration. A lost required need stops it instead, until it is back.
        """
        return False

    async def settings_did_change(self, before: Any) -> bool:
        """The integration's settings changed; ``self.settings`` already holds the new ones.

        Return True when applied in place; False (the default) restarts the integration.
        """
        return False

    async def context_did_change(self, before: Context) -> bool:
        """Hub-wide facts changed (region, Wi-Fi, Thread network); ``platform.context`` holds the new ones.

        Return True when applied in place; False (the default) restarts the integration.
        """
        return False

    # TODO: review if these two are needed, test the implementation

    @property
    def device_type(self) -> type[TDevice]:
        """Override to return a Device subclass. Hub will deserialize devices into this type before passing them to other controller methods."""
        return cast(type[TDevice], Device)

    @property
    def parameter_type(self) -> type[TParameter]:
        """Override to return a Parameter subclass. Hub will deserialize parameters into this type before passing them to other controller methods."""
        return cast(type[TParameter], Parameter)

    # Lifecycle

    @abstractmethod
    async def start(self):
        """
        Starts the integration. Called once on Hub startup, or when the integration is manually enabled.
        Register discovery services, subscribe to events, and check the status of already-paired devices here.
        """

    @abstractmethod
    async def stop(self):
        """
        Stops the integration. Called once on Hub shutdown, or when the integration is manually disabled.
        Cancel running tasks and release all held resources.
        """

    # Hub -> device

    async def start_pairing_window(self, duration_sec: int, credentials: ProvidedCredentials | None = None):  # noqa: B027  (optional no-op hook, not abstract)
        """
        Temporarily enables protocol-level discovery mechanisms that are not continuously active. Must only be used when default continuous discovery options aren't available or are insufficient. Does not affect, nor does use always-on discovery channels such as mDNS or SSDP.

        Used to trigger short-lived scan/inquiry modes for transports that require explicit activation
        (e.g. Zigbee permit-join style discovery, BLE scan bursts, proprietary radios).

        Devices discovered during this window may be eligible for onboarding via the pairing API.

        If devices are not successfully paired within the window, they must be disconnected.
        """
        pass

    @abstractmethod
    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None):
        """
        Pairs a discovered device to the Hub. Called when the user initiates pairing from the UI.
        """

    @abstractmethod
    async def unpair(self, device: TDevice):
        """
        Unpairs a device from the Hub. Called when the user initiates removal from the UI.
        """

    @abstractmethod
    async def identify(self, device: TDevice):
        """
        Asks the device to produce an identifying signal (e.g. blink a light, play a sound).
        Called when the user triggers identification from the UI.
        """

    @abstractmethod
    async def fetch(self, device: TDevice):
        """
        Fetches and refreshes the current state of the device and all its parameters.
        Called by the Hub when an up-to-date snapshot is needed.
        """

    @abstractmethod
    async def send_command(self, command: DeviceCommand, device: TDevice, parameter: TParameter):
        """
        Sends a control command targeting a specific device parameter.
        Called by the Hub when the user or an automation changes a parameter value.
        """

    # helpers / services

    @final
    @classmethod
    def slug(cls) -> str:
        """Slugified name of the integration.

        Class-level, so the Hub can key an integration's storage on it before constructing
        the controller.
        """
        slug = slugify(cls.name)
        if not slug:
            raise ValueError(
                f"{cls.__name__}'s name {cls.name!r} has no letters or digits to build a slug from. {_NAME_HELP}"
            )
        return slug

    @final
    @property
    def name_slug(self) -> str:
        """Slugified name of the integration"""
        return type(self).slug()

    @final
    def namespace_uuid(self) -> UUID:
        """Namespace UUID for the integration - used to generate device and parameter UUIDs"""
        return UUID(int=0)  # TODO: consider basing it on hub's mac address

    @final
    def integration_uuid(self) -> UUID:
        """Unique identifier of the integration"""
        return uuid5(self.namespace_uuid(), self.name_slug)

    @final
    def device_uuid(self, device_id: str) -> UUID:
        """uuid of the device based on an arbitrary string identifier"""
        return uuid5(self.integration_uuid(), device_id)

    @final
    def parameter_uuid(self, device_uuid: UUID, parameter_id: str) -> UUID:
        """uuid of the parameter based on an arbitrary string identifier"""
        return uuid5(device_uuid, parameter_id)

    @final
    @property
    def documents_folder(self) -> Path:
        """Folder for storing files related to this integration that don't belong in the device database."""
        return self.platform.documents_folder

    # def add_service_coroutine(self, coroutine: Callable[[], Awaitable]): ... # FUTURE

    # def add_service_thread(self, thread: Thread): ... # FUTURE

    # def schedule_task(self, callback: Callable[[], Awaitable] | Callable[[], Any], interval: float): ... # FUTURE

    # def search_native_parameter(self, name: str) -> Parameter | None: ... # FUTURE
