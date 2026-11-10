"""
Entrypoint and lifecycle boundary for an integration's Controller.
See docs.majordom.io/device-integration for the full integration guide.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, cast, final
from uuid import UUID, uuid5

from pydantic import BaseModel
from slugify import slugify

from majordom_integration_sdk.controller.output import ControllerOutput, LoggingControllerOutput
from majordom_integration_sdk.peripherals import Need, Peripheral, PeripheralChange
from majordom_integration_sdk.platform import Platform
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, DeviceState, Discovery, Parameter, ProvidedCredentials

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


class AbstractController[
    TDevice: Device,
    TParameter: Parameter,
    TSettings: BaseModel = Any,
    TDeviceState: DeviceState = DeviceState,
](Peripheral[TSettings], ABC):
    """
    Base class for all integration controllers.
    Defines the interface the Hub uses to interact with an integration — its package's ``peripherals(platform)`` builds the subclass, the runtime starts and stops it, and the Hub drives it by calling these methods.

    A controller is a `Peripheral` like any other: the runtime starts it once its required needs are bound to what
    runs, stops it when one is lost, and hands moved needs, settings and platform facts over to its hooks. Its integration
    package exports it next to the servers it needs, which run while it does, given the platform the Hub builds for
    the integration::

        def peripherals(platform: Platform) -> list[Peripheral]:
            return [matterjs_server(), MatterController(platform)]

    It holds:

    - ``self.platform``: the host, the same for every peripheral, given to its constructor: its facts (``region``,
      ``wifi``, ``thread_dataset``), its device repository and a documents folder, scoped to the controller
      (``self.platform.device_repository(self)``, ``self.platform.documents_folder(self)``);
    - ``self.output``: what it reports (``controller_did_…``: discoveries, devices, events, errors, notifications,
      what it provides), which the runtime relays to the Hub; it logs them in library mode until you give yours;
    - ``self.settings``: its validated ``Settings``, declared as a nested ``class Settings(BaseModel)`` named as the
      third type parameter, ``AbstractController[Device, Parameter, "HueController.Settings"]``; `NoSettings` without;
    - its device-state model, the fourth type parameter (`DeviceState` by default), returned by ``device_state_type``:
      what ``pair_device`` is given as its draft and returns;
    - ``self.peripherals``: the endpoints its needs are bound to (``endpoint()``, ``endpoints()``, ``instance()``).

    In library mode, with no runtime, give it any settings, hand the bound needs over yourself and, to hear what it
    reports rather than read it in the log, your output::

        controller = HueController(platform, settings=HueController.Settings(bridge="192.168.1.2"))
        controller.peripherals = Peripherals({"mdns": Endpoint("mdns", "zeroconf", instance=zeroconf)})
        controller.output = MyOutput()  # a ControllerOutput
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Auto-derive `name` from the class name unless the subclass set one explicitly.
        if "name" not in cls.__dict__:
            cls.name = _titleize(cls.__name__)

    # A controller reports through the controller calls alone: the runtime's output answers the peripheral ones too
    output: ControllerOutput = LoggingControllerOutput()

    def __init__(self, platform: Platform, *, settings: TSettings | None = None):
        """``settings`` hold until the user changes them (the runtime keeps them): the defaults of ``Settings`` without.
        Keep these parameters when you override it."""
        self.platform = platform
        self.settings = settings if settings is not None else self.Settings()

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
    state: its `slug()` is the controller's ``key``, which the runtime knows it by and the
    platform scopes its devices (``Device.integration``) and documents folder by.
    """

    @property
    def key(self) -> str:
        return self.slug()

    needs: Sequence[Need] = ()
    """What the integration requires from its environment: everything external, which something else must provide —
    radios (``Need("zigbee", ["ezsp"])``), the Bluetooth router (`BLE_ROUTER`), DNS-SD (`ZEROCONF`), SSDP
    (`UPNP_SSDP`) and the services its integration provides next to it. Bound ones arrive in ``self.peripherals``
    keyed by family, an in-process one typed by its need: ``self.peripherals.instance(BLE_ROUTER)``; in library mode
    hand them over yourself. A
    required need that can't be bound keeps the integration from starting (the Hub tells the user what it waits for, the
    log why); an optional one arrives once its provider runs, as it appears (`needed_peripheral_did_change`). Never need
    raw Bluetooth: the router holds every radio."""

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """A bound need was lost, appeared, or moved; ``self.peripherals`` is already updated.

        Return True when handled in place (e.g. Matter keeps working over IP without Thread).
        False (the default) restarts the integration. A lost required need stops it instead, until it is back.
        """
        return False

    async def settings_did_change(self, before: TSettings) -> bool:
        """The integration's settings changed; ``self.settings`` already holds the new ones.

        Return True when applied in place; False (the default) restarts the integration.
        """
        return False

    async def platform_facts_did_change(self, before: Platform) -> bool:
        """The host's facts changed (region, Wi-Fi, Thread network); ``self.platform`` holds the new ones.

        Return True (the default) when they don't concern it or were applied in place; False restarts the
        integration. One that reads them only when it acts (pairing a device) needs no override.
        """
        return True

    # TODO: review if these two are needed, test the implementation

    @property
    def device_type(self) -> type[TDevice]:
        """Override to return a Device subclass. Hub will deserialize devices into this type before passing them to other controller methods."""
        return cast(type[TDevice], Device)

    @property
    def device_state_type(self) -> type[TDeviceState]:
        """Override to return a DeviceState subclass. Hub will validate the pairing draft into this type before passing it to ``pair_device``."""
        return cast(type[TDeviceState], DeviceState)

    @property
    def parameter_type(self) -> type[TParameter]:
        """Override to return a Parameter subclass. Hub will deserialize parameters into this type before passing them to other controller methods."""
        return cast(type[TParameter], Parameter)

    # Lifecycle

    @abstractmethod
    async def start(self):
        """
        Starts the integration. Called by the runtime once its required needs are bound to what runs, and again after a stop: a restart, a hook it didn't take, a retry after a failure, the user enabling it.
        Register discovery services, subscribe to events, and check the status of already-paired devices here. Raise when it can't start: it is tried again later, then failed.
        """

    @abstractmethod
    async def stop(self):
        """
        Stops the integration. Called by the runtime on Hub shutdown, when it is disabled, loses a required need or restarts, and after a start that failed or was cut short.
        Cancel running tasks and release all held resources, also after a partial start, and leave the object clean for the next ``start``: nothing in flight (a pairing) may take resources after it.
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
    async def pair_device(
        self, discovery: Discovery, draft: TDeviceState, credentials: ProvidedCredentials | None
    ) -> TDeviceState:
        """
        Pairs a discovered device to the Hub and returns ``draft`` filled in. Called when the user initiates pairing
        from the UI.

        The Hub builds ``draft`` (no record exists yet): the user's fields (name, room, icon, category, note), ``id``
        the discovery's, ``integration`` this controller's key. When the user picked an existing record to pair onto,
        it carries that record's data, its ``id`` and ``integration_data`` included: decide what in it is stale.

        Fill in what the device is: ``parameters``, ``integration_data``, the final ``id`` (``device_uuid``),
        ``available``, … and return it. The Hub saves it and then reports the device connected and fetches it, so:

        - don't use the device repository: nothing is saved during pairing;
        - report nothing about this device (no ``controller_did_connect_device``, no events) until the Hub saved it;
        - once the final id is known, call ``check_device_id`` before anything irreversible;
        - fill the draft in place *before* each irreversible step (a key about to be set, a node about to be
          included): on a failure the Hub calls ``unpair(draft)`` to roll back, which can only undo what the draft
          says ran.

        Raise when it can't pair (`MissingCredentialsError` before touching the device when credentials are lacking).
        """

    @abstractmethod
    async def unpair(self, device: TDevice):
        """
        Unpairs a device from the Hub. Called when the user initiates removal from the UI.

        Also the rollback of a failed pairing: the Hub calls it with the draft as ``pair_device`` left it, at any
        stage. Undo in reverse order of pairing, tolerate any partial state (an id not final yet, no
        ``integration_data``, a device gone), never wait for the user, and go as far as you can: it is best effort,
        within the Hub's timeout.
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
        Brings the device to a known-good state; called after pairing, on a manual refresh, and to revive a device
        that is unavailable. Idempotent:

        - check the device answers and read its current values, reported through the normal events;
        - check its event subscription and subscribe again only when it is gone, never a second one;
        - finish in bounded time; a sleeping battery device that doesn't answer is not a failure.
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
        """Slugified name of the integration: the controller's ``key``, which the runtime and the Hub know it by and its
        devices and documents folder are scoped by.
        """
        slug = slugify(cls.name)
        if not slug:
            raise ValueError(
                f"{cls.__name__}'s name {cls.name!r} has no letters or digits to build a slug from. {_NAME_HELP}"
            )
        return slug

    @final
    def namespace_uuid(self) -> UUID:
        """Namespace UUID for the integration - used to generate device and parameter UUIDs"""
        return UUID(int=0)  # TODO: consider basing it on hub's mac address

    @final
    def integration_uuid(self) -> UUID:
        """Unique identifier of the integration"""
        return uuid5(self.namespace_uuid(), self.key)

    @final
    def device_uuid(self, device_id: str) -> UUID:
        """uuid of the device based on a string unique to that device (MAC, serial number): a name or an address two
        devices share gives them the same id, and the second can't be paired."""
        return uuid5(self.integration_uuid(), device_id)

    @final
    async def check_device_id(self, device_id: UUID, draft: TDeviceState):
        """Raises `DeviceIdTakenError` when another record holds ``device_id``, the final id of the device ``draft``
        pairs. The record the user picked to pair onto (``draft.id``) is not another. Optional: call it in
        ``pair_device`` as soon as the final id is known, before anything irreversible."""
        await self.platform.check_device_id(device_id, draft)

    @final
    def parameter_uuid(self, device_uuid: UUID, parameter_id: str) -> UUID:
        """uuid of the parameter based on an arbitrary string identifier"""
        return uuid5(device_uuid, parameter_id)

    # def add_service_coroutine(self, coroutine: Callable[[], Awaitable]): ... # FUTURE

    # def add_service_thread(self, thread: Thread): ... # FUTURE

    # def schedule_task(self, callback: Callable[[], Awaitable] | Callable[[], Any], interval: float): ... # FUTURE

    # def search_native_parameter(self, name: str) -> Parameter | None: ... # FUTURE
