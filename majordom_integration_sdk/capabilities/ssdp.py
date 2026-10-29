"""The ``upnp/ssdp`` contract: hearing UPnP devices and services over SSDP.

Integrations need it as `UPNP_SSDP` and get the runtime's listener at ``self.peripherals.instance(UPNP_SSDP)``: an
`SSDP`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from majordom_integration_sdk.peripherals.need import Need


@dataclass
class SSDPDiscoveryInfo:
    addr: str
    host: str | None
    search_target: str | None
    service_name: str | None
    server: str | None
    cache_control: str | None
    location: str | None
    response: dict[str, str]


class SSDPDiscoveryListener(Protocol):
    async def ssdp_did_discover_service(self, ssdp: SSDP, info: SSDPDiscoveryInfo): ...
    async def ssdp_did_update_service(self, ssdp: SSDP, info: SSDPDiscoveryInfo): ...
    async def ssdp_did_remove_service(self, ssdp: SSDP, info: SSDPDiscoveryInfo): ...


class SSDP(Protocol):
    def register(
        self,
        listener: SSDPDiscoveryListener,
        search_target: str,
        mcast_group: str = "239.255.255.250",
        port: int = 1900,
    ) -> Callable[[], None]:
        """Calls ``listener`` about what answers ``search_target`` (``"ssdp:all"``, a device or service type) as it
        comes, changes and goes. Returns a cancel function — call it on stop to deregister."""
        ...


UPNP_SSDP = Need[SSDP]("upnp", ["ssdp"])  # optional: dataclasses.replace(it, is_required=False)
