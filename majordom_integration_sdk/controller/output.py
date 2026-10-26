"""What an integration reports back to the Hub, through its ``self.output``."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationError:
    """A problem with the whole integration, active until it is resolved or the integration restarts."""

    # Stable per condition, e.g. "server-unreachable": links a report to its resolve. Scoped to the integration;
    # the "hub-" prefix is reserved for the errors the Hub reports on its behalf.
    id: str
    message: str  # plain language, with any call-to-action in the text
    # False when it can't go on: it is stopped, and started again after each retry delay, then failed
    is_still_running: bool = True


class ControllerOutput(Protocol):
    """
    Defines the callback interface for passing data from an integration back to the Hub: a controller's
    ``self.output``, which the runtime sets (it relays to the Hub). Without one, in library mode, it logs
    every call (`LoggingControllerOutput`); give it yours: ``controller.output = MyOutput()``.
    """

    async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery): ...
    async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery): ...
    async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID): ...

    async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID): ...

    async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
        """Paired device became unreachable while the Hub is running (not just at startup/reboot)."""
        ...

    async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]):
        """Report device-domain events (e.g. DeviceParameterChange) observed by the controller."""
        ...

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError):
        """A user-facing problem the integration hit — surfaced to the user, not the developer.

        Use this for things the user should know about or can act on (a wrong setup, an unplugged
        radio); developer/technical detail goes to the logs instead. ``error.message`` is plain
        language — include any call-to-action right in the text. Problems scoped to one device
        belong on that device's ``last_error``, not here.

        The error stays active until ``controller_did_resolve_error`` with its ``id``, or until the
        integration stops. Reporting an active ``id`` again replaces it, so it is fine to report on
        every failed attempt; several ids can be active at once.

        A controller that can't go on reports an error that isn't ``is_still_running``: it is stopped,
        started again after each of the runtime's retry delays (5, 30, 120 s), then failed until the
        user restarts it, ``error.message`` saying why: report it right from the task that found it (the runtime
        stops the controller in a task of its own). One that can't start raises from ``start``.
        """
        ...

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str):
        """The problem reported with ``error_id`` is gone. Resolving an id that isn't active does nothing."""
        ...

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        """Surface a general user-facing notification — a floating tile in the app. Unlike
        ``controller_did_encounter_error`` (specifically for failures/health), this is for anything
        informational or advisory: a firmware update is available, a device needs a physical action,
        a routine finished. The ``Notification`` carries the message plus its callout ``type``
        (info/warning/error), delivery ``priority``, and ``ttl`` (seconds before auto-dismiss).
        """
        ...

    async def controller_did_change_provided(self, controller: AbstractController):
        """What it ``provides`` changed on its own, e.g. ESPHome's Bluetooth proxies as nodes come and go: the
        runtime reads it again. The runtime's output takes this one itself: the Hub's needn't implement it."""
        return


class LoggingControllerOutput(ControllerOutput):
    """A ``ControllerOutput`` that logs every callback: the Hub's stand-in for dev runs and library mode."""

    async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info(f"discovered {discovery.device_name} ({discovery.transport}) id={discovery.id}")

    async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info(f"updated discovery {discovery.id}")

    async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID):
        logger.info(f"lost discovery {discovery_id}")

    async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID):
        logger.info(f"connected device {device_id}")

    async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
        logger.info(f"lost device {device_id}")

    async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]):
        for event in events:
            logger.info(f"event {type(event).__name__}: {event!r}")

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError):
        stopped = "" if error.is_still_running else ", it can't go on"
        logger.error(f"integration error {error.id}{stopped}: {error.message}")

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str):
        logger.info(f"integration error {error_id} resolved")

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        logger.info(f"notification [{notification.type.value}/{notification.priority.value}]: {notification.message}")

    async def controller_did_change_provided(self, controller: AbstractController):
        logger.info(f"{controller.key} changed what it provides")


# Last, once the names the controller takes from here exist: the annotations above then evaluate
from majordom_integration_sdk.controller.abstract_controller import AbstractController  # noqa: E402
