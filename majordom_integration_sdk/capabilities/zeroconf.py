"""The ``mdns/zeroconf`` contract: browsing DNS-SD services over mDNS.

Integrations need it as `ZEROCONF` and get the runtime's browser at ``self.peripherals.instance(ZEROCONF)``: a
`Zeroconf`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from majordom_integration_sdk.peripherals.need import Need

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncZeroconf


@dataclass
class ZeroconfDiscoveryInfo:
    """
    * `type_`: fully qualified service type name
    * `name`: fully qualified service name
    * `port`: port that the service runs on
    * `weight`: weight of the service
    * `priority`: priority of the service
    * `properties`: dictionary of properties as raw bytes. Keys with `None` values are value-less attributes.
    * `decoded_properties`: `properties` decoded to `str` — use this for most integrations.
    * `text`: raw TXT record bytes — use only if you need to parse the record manually.
    * `server`: fully qualified name for service host (defaults to name)
    * `host_ttl`: ttl used for A/SRV records
    * `other_ttl`: ttl used for PTR/TXT records
    * `addresses` and `parsed_addresses`: List of IP addresses (either as bytes, network byte order,
        or in parsed form as text; at most one of those parameters can be provided)
    * interface_index: scope_id or zone_id for IPv6 link-local addresses i.e. an identifier of the interface
        where the peer is connected to
    """

    type_: str
    name: str
    server: str | None
    port: int | None
    addresses: list[bytes] | None
    parsed_addresses: list[str] | None
    weight: int | None
    priority: int | None
    properties: dict[bytes, bytes | None]
    decoded_properties: dict[str, str | None]
    text: bytes
    host_ttl: int | None
    other_ttl: int | None
    interface_index: int | None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ZeroconfDiscoveryInfo):
            return NotImplemented
        # exclude host_ttl / other_ttl — they change on every re-announcement
        return (
            self.type_ == other.type_
            and self.name == other.name
            and self.server == other.server
            and self.port == other.port
            and self.addresses == other.addresses
            and self.parsed_addresses == other.parsed_addresses
            and self.weight == other.weight
            and self.priority == other.priority
            and self.properties == other.properties
            and self.text == other.text
            and self.interface_index == other.interface_index
        )


class ZeroconfDiscoveryListener(Protocol):
    async def zeroconf_did_discover_service(self, zeroconf: Zeroconf, info: ZeroconfDiscoveryInfo): ...
    async def zeroconf_did_update_service(self, zeroconf: Zeroconf, info: ZeroconfDiscoveryInfo): ...
    async def zeroconf_did_remove_service(self, zeroconf: Zeroconf, type_: str, name: str):
        """Fired on mDNS goodbye packets (TTL=0) or when a silent departure is confirmed:
        A/AAAA records expired (~120s TTL) and a follow-up async_request got no response.
        For faster detection, implement liveness checks at the pairing/connection layer."""
        ...


class Zeroconf(Protocol):
    def register(self, listener: ZeroconfDiscoveryListener, services: set[str]) -> Callable[[], None]:
        """Calls ``listener`` about the services of the given types (``"_hap._tcp.local."``) as they come, change
        and go. Returns a cancel function — call it on stop to deregister."""
        ...

    @property
    def async_zeroconf(self) -> AsyncZeroconf | None:
        """The zeroconf instance it browses with, for libraries that take one (aiohomekit); None until it starts."""
        ...


ZEROCONF = Need[Zeroconf]("mdns", ["zeroconf"])  # optional: dataclasses.replace(it, is_required=False)
