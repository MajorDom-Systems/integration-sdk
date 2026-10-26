"""The one ``output`` every peripheral of a runtime shares."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from majordom_integration_sdk.controller.output import ControllerOutput, IntegrationError
from majordom_integration_sdk.peripherals import Peripheral
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.abstract_controller import AbstractController
    from majordom_integration_sdk.runtime.runtime import Runtime


class HostOutput(ControllerOutput):
    """What every peripheral of a runtime tells, as its ``output``: its lifecycle goes to the runtime, a controller's
    reports to ``controller_output`` (the Hub). A controller that can't go on (an error not ``is_still_running``)
    fails in the runtime too, and one that changed what it provides has the runtime read it again."""

    def __init__(self, runtime: Runtime, controller_output: ControllerOutput):
        self.runtime, self.controller_output = runtime, controller_output

    # PeripheralOutput

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        await self.runtime.peripheral_did_change_provided(peripheral)

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        await self.runtime.peripheral_did_learn(peripheral, facts)

    async def peripheral_did_stop(self, peripheral: Peripheral):
        await self.runtime.peripheral_did_stop(peripheral)

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        await self.runtime.peripheral_did_fail(peripheral, reason)

    # ControllerOutput

    async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery):
        await self.controller_output.controller_did_receive_discovery(controller, discovery)

    async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery):
        await self.controller_output.controller_did_update_discovery(controller, discovery)

    async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID):
        await self.controller_output.controller_did_lose_discovery(controller, discovery_id)

    async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID):
        await self.controller_output.controller_did_connect_device(controller, device_id)

    async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
        await self.controller_output.controller_did_lose_device(controller, device_id)

    async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]):
        await self.controller_output.controller_did_receive_events(controller, events)

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError):
        await self.controller_output.controller_did_encounter_error(controller, error)
        if not error.is_still_running:
            await self.runtime.peripheral_did_fail(controller, error.message)

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str):
        await self.controller_output.controller_did_resolve_error(controller, error_id)

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        await self.controller_output.controller_did_emit_notification(controller, notification)

    async def controller_did_change_provided(self, controller: AbstractController):
        await self.runtime.peripheral_did_change_provided(controller)
