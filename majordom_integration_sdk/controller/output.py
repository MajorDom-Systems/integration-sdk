"""What an integration reports back to the Hub."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.abstract_controller import AbstractController


@dataclass(frozen=True)
class IntegrationError:
    """A problem with the whole integration, active until it is resolved or the integration restarts."""

    id: str  # stable per condition, e.g. "server-unreachable", "stick-unplugged": links a report to its resolve
    message: str  # plain language, with any call-to-action in the text
    still_running: bool = True  # False: the integration stopped, and the Hub restarts it


class ControllerOutput(Protocol):
    """
    Defines the callback interface for passing data from an integration back to the Hub.
    An instance is injected by the Hub into every controller as ``platform.output``.
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

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError) -> None:
        """A user-facing problem the integration hit — surfaced to the user, not the developer.

        Use this for things the user should know about or can act on (a wrong setup, an unplugged
        radio); developer/technical detail goes to the logs instead. ``error.message`` is plain
        language — include any call-to-action right in the text. Problems scoped to one device
        belong on that device's ``last_error``, not here.

        The error stays active until ``controller_did_resolve_error`` with its ``id``, or until the
        integration stops. Reporting an active ``id`` again replaces it, so it is fine to report on
        every failed attempt; several ids can be active at once.

        ``still_running`` is ``False`` when the controller can no longer run and is now inactive
        — the Hub reflects it as a failed integration and restarts it; the user is told it failed
        (and why, if actionable) without a technical dump.
        """
        ...

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str) -> None:
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
