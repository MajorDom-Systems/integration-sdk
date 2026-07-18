import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from zeroconf import BadTypeInNameException, ServiceListener, Zeroconf, current_time_millis
from zeroconf._dns import DNSPointer
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
from zeroconf.const import _CLASS_IN, _TYPE_A, _TYPE_AAAA, _TYPE_PTR

logger = logging.getLogger(__name__)

_RESOLVE_DELAY = 0.5  # seconds — coalesces burst PTR/SRV/TXT/A packets into one resolve
_RESOLVE_TIMEOUT_MS = 3_000
_CACHE_POLL_INTERVAL = 30  # seconds — how often to reconcile our state against zeroconf's PTR cache
_EVICT_GRACE_MS = 10_000  # ms — grace window after A/AAAA expiry before probing; allows zeroconf to renew


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
    async def zeroconf_did_discover_service(
        self, zeroconf: "ZeroconfDiscoveryService", info: ZeroconfDiscoveryInfo
    ): ...
    async def zeroconf_did_update_service(self, zeroconf: "ZeroconfDiscoveryService", info: ZeroconfDiscoveryInfo): ...
    async def zeroconf_did_remove_service(self, zeroconf: "ZeroconfDiscoveryService", type_: str, name: str):
        """Fired on mDNS goodbye packets (TTL=0) or when a silent departure is confirmed:
        A/AAAA records expired (~120s TTL) and a follow-up async_request got no response.
        For faster detection, implement liveness checks at the pairing/connection layer."""
        ...


class ZeroconfDiscoveryService:
    @property
    def async_zeroconf(self) -> AsyncZeroconf | None:
        return self._async_zeroconf

    def __init__(self):
        self._async_zeroconf: AsyncZeroconf | None = None
        self._browsers: set[AsyncServiceBrowser] = set()
        self._adapters: list[_ServiceListenerAdapter] = []
        self._poll_task: asyncio.Task | None = None
        # registrations made before start() — flushed on start
        self._pending: list[tuple[set[str], ZeroconfDiscoveryListener, int]] = []
        self._pending_id: int = 0

    # FUTURE: filter type
    def register(self, listener: ZeroconfDiscoveryListener, services: set[str]) -> Callable[[], None]:
        if self._async_zeroconf is None:
            reg_id = self._pending_id
            self._pending_id += 1
            self._pending.append((services, listener, reg_id))  # internal order unchanged

            def cancel_pending():
                for i, (_, _, rid) in enumerate(self._pending):
                    if rid == reg_id:
                        del self._pending[i]
                        break

            return cancel_pending

        browser, adapter = self._make_browser(services, listener)

        def cancel():
            adapter.cancel_all_pending()
            self._browsers.discard(browser)
            self._adapters.remove(adapter)
            asyncio.create_task(browser.async_cancel())

        return cancel

    async def start(self):
        self._async_zeroconf = AsyncZeroconf()
        for services, listener, _ in self._pending:
            self._make_browser(services, listener)
        self._pending.clear()
        self._poll_task = asyncio.create_task(self._cache_poll_loop())
        logger.debug(f"Zeroconf started, {len(self._browsers)} browser(s) active")

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        for adapter in self._adapters:
            adapter.cancel_all_pending()
        for browser in list(self._browsers):
            await browser.async_cancel()
        self._browsers.clear()
        self._adapters.clear()
        if self._async_zeroconf is not None:
            await self._async_zeroconf.async_close()
            self._async_zeroconf = None
        logger.debug("Zeroconf stopped")

    async def _cache_poll_loop(self):
        while True:
            await asyncio.sleep(_CACHE_POLL_INTERVAL)
            if self._async_zeroconf is None:
                continue
            zc = self._async_zeroconf.zeroconf
            now = current_time_millis()
            for adapter in list(self._adapters):
                adapter.sync_with_cache(zc, now)

    def _make_browser(
        self, services: set[str], listener: ZeroconfDiscoveryListener
    ) -> tuple[AsyncServiceBrowser, "_ServiceListenerAdapter"]:
        assert self._async_zeroconf is not None
        adapter = _ServiceListenerAdapter(self, listener, services)
        browser = AsyncServiceBrowser(
            self._async_zeroconf.zeroconf,
            list(services),
            listener=adapter,
        )
        self._browsers.add(browser)
        self._adapters.append(adapter)
        return browser, adapter


class _ServiceListenerAdapter(ServiceListener):
    def __init__(
        self, zeroconf_discovery: ZeroconfDiscoveryService, listener: ZeroconfDiscoveryListener, services: set[str]
    ):
        self._zc_service = zeroconf_discovery
        self._listener = listener
        self._services = services
        self._loop = asyncio.get_running_loop()
        self._resolve_later_queue: dict[str, asyncio.TimerHandle] = {}
        self._known_names: dict[str, ZeroconfDiscoveryInfo] = {}  # name → last known info

    def add_service(self, zc: Zeroconf, type_: str, name: str):
        self._schedule_resolve(type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str):
        self._schedule_resolve(type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str):
        # goodbye packet — cancel pending resolve and notify immediately
        if handle := self._resolve_later_queue.pop(name, None):
            handle.cancel()
        self._known_names.pop(name, None)
        logger.debug(f"Zeroconf goodbye received: {name}")
        asyncio.create_task(self._listener.zeroconf_did_remove_service(self._zc_service, type_, name))

    def cancel_all_pending(self):
        while self._resolve_later_queue:
            _, handle = self._resolve_later_queue.popitem()
            handle.cancel()

    def sync_with_cache(self, zc: Zeroconf, now: float):
        """Reconcile _known_names against the live cache.

        When A/AAAA records expire, we confirm via async_request before evicting —
        expiry alone is unreliable (device may have just re-announced). A failed
        async_request is the definitive "no goodbye" departure signal.
        Rediscovery scans live PTR records for names we don't know yet.
        """
        # confirm-evict: A/AAAA expired → probe network; evict only if unreachable
        for name, last_info in list(self._known_names.items()):
            if last_info.server is None:
                continue
            a_records = list(zc.cache.get_all_by_details(last_info.server, _TYPE_A, _CLASS_IN))
            aaaa_records = list(zc.cache.get_all_by_details(last_info.server, _TYPE_AAAA, _CLASS_IN))
            all_address_records = a_records + aaaa_records
            if not all_address_records or all(
                r.get_expiration_time(100) + _EVICT_GRACE_MS <= now for r in all_address_records
            ):
                if name not in self._resolve_later_queue:
                    logger.debug(f"Zeroconf A/AAAA expired, confirming: {name}")
                    asyncio.create_task(self._confirm_evict(name, last_info.type_))

        # rediscover: scan live PTR records for unknown names
        for type_ in self._services:
            for record in zc.cache.get_all_by_details(type_, _TYPE_PTR, _CLASS_IN):
                ptr = cast(DNSPointer, record)
                if (
                    not ptr.is_expired(now)
                    and ptr.alias not in self._known_names
                    and ptr.alias not in self._resolve_later_queue
                ):
                    logger.debug(f"Zeroconf cache rediscovery: {ptr.alias}")
                    self._schedule_resolve(type_, ptr.alias)

    async def _confirm_evict(self, name: str, type_: str):
        """Probe the network for a device whose A/AAAA records expired. Evict only if unreachable."""
        assert self._zc_service._async_zeroconf is not None
        zc = self._zc_service._async_zeroconf.zeroconf
        info = AsyncServiceInfo(type_, name)
        if await info.async_request(zc, _RESOLVE_TIMEOUT_MS):
            # device responded — it's alive, update via normal resolve path
            logger.debug(f"Zeroconf confirm-evict: {name} is alive, refreshing")
            discovery_info = _build_info(info)
            if discovery_info and name in self._known_names:
                if discovery_info != self._known_names[name]:
                    self._known_names[name] = discovery_info
                    await self._listener.zeroconf_did_update_service(self._zc_service, discovery_info)
                else:
                    self._known_names[name] = discovery_info  # refresh server silently
        else:
            # no response — confirmed departed
            if name in self._known_names:
                self._known_names.pop(name)
                logger.debug(f"Zeroconf eviction confirmed (no response): {name}")
                await self._listener.zeroconf_did_remove_service(self._zc_service, type_, name)

    def _schedule_resolve(self, type_: str, name: str):
        # already queued — let the existing timer fire, burst coalescing
        if name in self._resolve_later_queue:
            return
        try:
            info = AsyncServiceInfo(type_, name)
        except BadTypeInNameException as ex:
            logger.debug(f"Ignoring record with bad type in name: {name}: {ex}")
            return
        handle = self._loop.call_later(
            _RESOLVE_DELAY,
            lambda: asyncio.create_task(self._do_resolve(name, info)),
        )
        self._resolve_later_queue[name] = handle

    async def _do_resolve(self, name: str, info: AsyncServiceInfo):
        self._resolve_later_queue.pop(name, None)

        assert self._zc_service._async_zeroconf is not None
        zc = self._zc_service._async_zeroconf.zeroconf

        # cache-first: free if warm, network round-trip only if cold
        if not info.load_from_cache(zc):
            if not await info.async_request(zc, _RESOLVE_TIMEOUT_MS):
                logger.debug(f"Zeroconf resolve failed (no data): {name}")
                return

        discovery_info = _build_info(info)
        if discovery_info is None:
            return

        if name in self._known_names:
            if discovery_info != self._known_names[name]:
                self._known_names[name] = discovery_info
                await self._listener.zeroconf_did_update_service(self._zc_service, discovery_info)
            else:
                self._known_names[name] = discovery_info  # refresh silently
        else:
            self._known_names[name] = discovery_info
            await self._listener.zeroconf_did_discover_service(self._zc_service, discovery_info)


def _build_info(info: AsyncServiceInfo) -> ZeroconfDiscoveryInfo | None:
    if info.port is None:
        return None
    return ZeroconfDiscoveryInfo(
        type_=info.type,
        name=info.name,
        server=info.server,
        port=info.port,
        addresses=list(info.addresses),
        parsed_addresses=info.parsed_addresses(),
        weight=info.weight,
        priority=info.priority,
        properties=info.properties,
        decoded_properties=info.decoded_properties,
        text=info.text,
        host_ttl=info.host_ttl,
        other_ttl=info.other_ttl,
        interface_index=info.interface_index,
    )


if __name__ == "__main__":
    logging.basicConfig()
    logger.setLevel(logging.DEBUG)

    async def main():
        discovery = ZeroconfDiscoveryService()

        class HAPListener:
            async def zeroconf_did_discover_service(
                self, zeroconf: ZeroconfDiscoveryService, info: ZeroconfDiscoveryInfo
            ):
                print(f"Found: {info.name} @ {info.parsed_addresses}:{info.port}")

            async def zeroconf_did_update_service(
                self, zeroconf: ZeroconfDiscoveryService, info: ZeroconfDiscoveryInfo
            ):
                print(f"Updated: {info.name} @ {info.parsed_addresses}:{info.port}")

            async def zeroconf_did_remove_service(self, zeroconf: ZeroconfDiscoveryService, type_: str, name: str):
                print(f"Removed: {name}")

        discovery.register(HAPListener(), {"_hap._tcp.local."})
        await discovery.start()

        while True:
            await asyncio.sleep(1)

    asyncio.run(main())
