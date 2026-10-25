"""Endpoints you provide yourself."""

from __future__ import annotations

from collections.abc import Sequence

from majordom_integration_sdk.peripherals import Endpoint, Peripheral


class Pinned(Peripheral):
    """Endpoints you run yourself, one per family: a ``socket://`` EZSP stick on the network, a matter-server of your
    own. Give it to the runtime first, so they win over anything found.

        stick = Endpoint("zigbee", "ezsp", "socket://192.168.1.5:6638")
        Runtime(data, peripherals=[Pinned(stick), *default_peripherals()])
    """

    name = "pinned"

    def __init__(self, *endpoints: Endpoint):
        self._endpoints = {endpoint.family: endpoint for endpoint in endpoints}

    @property
    def provides(self) -> Sequence[Endpoint]:
        return list(self._endpoints.values())

    async def pin(self, endpoint: Endpoint):
        """Provide ``endpoint`` for its family, in place of the one pinned before."""
        self._endpoints[endpoint.family] = endpoint
        await self.runtime.peripheral_did_change_provided(self)

    async def unpin(self, family: str):
        self._endpoints.pop(family, None)
        await self.runtime.peripheral_did_change_provided(self)
