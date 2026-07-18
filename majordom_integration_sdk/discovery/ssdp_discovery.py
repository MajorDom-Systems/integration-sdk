import asyncio
import logging
import socket
import struct
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import NamedTuple, Protocol

logger = logging.getLogger(__name__)


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
    async def ssdp_did_discover_service(self, ssdp: "SSDPDiscoveryService", info: "SSDPDiscoveryInfo"): ...
    async def ssdp_did_update_service(self, ssdp: "SSDPDiscoveryService", info: "SSDPDiscoveryInfo"): ...
    async def ssdp_did_remove_service(self, ssdp: "SSDPDiscoveryService", info: "SSDPDiscoveryInfo"): ...


@dataclass
class _KnownService:
    info: SSDPDiscoveryInfo
    last_seen_at: float  # monotonic — updated on every scan reply


@dataclass
class _Subscriber:
    listener: SSDPDiscoveryListener
    search_target: str
    mcast_group: str
    port: int
    socket: socket.socket
    known: dict[str, _KnownService] = field(default_factory=dict)  # USN -> _KnownService


class _Endpoint(NamedTuple):
    mcast_group: str
    port: int


@dataclass
class _SocketSubscribers:
    scan_socket: socket.socket  # ephemeral, unbound — sends M-SEARCH and reads unicast replies (blocking + timeout)
    search_subscribers: dict[str, list[_Subscriber]] = field(
        default_factory=lambda: defaultdict(list)
    )  # search_target -> list of Subscriber


class SSDPDiscoveryService:
    def __init__(self):
        # Single dict: Endpoint -> SocketSubscribers(socket, [Subscriber])
        self._endpoints_subscribers: dict[_Endpoint, _SocketSubscribers] = dict()
        self._is_running = False
        self._tasks: list[asyncio.Task] = []
        self._mx = 2  # MX - maximum wait time (timeout) seconds. Used to even scan responses across time and reduce network load.
        self._scan_interval: float = 10.0  # seconds between M-SEARCH bursts
        self._missed_scans_evict: int = 3  # evict after this many consecutive scans with no reply
        self._ignorelist: set[str] = self._local_ips()

    # FUTURE: filter type
    def register(
        self,
        listener: SSDPDiscoveryListener,
        search_target: str,
        mcast_group: str = "239.255.255.250",
        port: int = 1900,
    ) -> Callable[[], None]:
        """
        Args:
            listener: Object that will receive discovery events.
            search_target: SSDP ST (Search Target) field to specify the type of device or service to discover.
                Common ST templates:
                    - "uuid:<device-UUID>": discover a device by its UUID
                    - "urn:schemas-upnp-org:device:<DeviceType>:<Version>": discover devices of a specific type
                    - "urn:schemas-upnp-org:service:<ServiceType>:<Version>": discover specific service types
                    - "upnp:rootdevice": discover root devices
                    - "ssdp:all": discover all devices/services
                Examples:
                    - "uuid:550e8400-e29b-41d4-a716-446655440000"
                    - "urn:schemas-upnp-org:device:MediaServer:1"
            mcast_group: Multicast group address for SSDP (default: "239.255.255.250"). Don't change it unless you know what you're doing.
            port: Port for SSDP (default: 1900). Don't change it unless you know what you're doing.
        """

        endpoint = _Endpoint(mcast_group, port)
        if endpoint not in self._endpoints_subscribers:
            scan_sock = self._create_scan_socket()
            self._endpoints_subscribers[endpoint] = _SocketSubscribers(scan_sock)

        subscriber = _Subscriber(
            listener, search_target, mcast_group, port, self._endpoints_subscribers[endpoint].scan_socket
        )
        self._endpoints_subscribers[endpoint].search_subscribers[search_target].append(subscriber)

        def cancel():
            subscribers = self._endpoints_subscribers.get(endpoint, _SocketSubscribers(None)).search_subscribers  # type: ignore[arg-type]
            if search_target in subscribers:
                try:
                    subscribers[search_target].remove(subscriber)
                except ValueError:
                    pass

        return cancel

    # Putting workers together

    async def start(self):
        self._is_running = True
        self._tasks = [
            asyncio.create_task(self._start_scanning()),
            asyncio.create_task(self._start_expiry_checks()),
            asyncio.create_task(self._start_listening_notify()),
        ]

    def perform_scan(self):
        """Triggers an immediate M-SEARCH burst on all registered endpoints. Use from start_pairing_window."""
        asyncio.create_task(self._scan_and_read())

    async def stop(self):
        self._is_running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for es in self._endpoints_subscribers.values():
            es.scan_socket.close()

    # Private

    async def _start_scanning(self):
        """Send M-SEARCH bursts and collect unicast replies; repeat every scan_interval."""
        while self._is_running:
            await self._scan_and_read()
            await asyncio.sleep(self._scan_interval)

    async def _start_expiry_checks(self):
        while self._is_running:
            await asyncio.sleep(self._scan_interval)
            await self._evict_expired()

    async def _start_listening_notify(self):
        """Listens for unsolicited SSDP NOTIFY advertisements on the multicast group."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 1900))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self._get_mreq("239.255.255.250"))
        sock.setblocking(False)
        loop = asyncio.get_event_loop()
        try:
            while self._is_running:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 2048)
                except (BlockingIOError, OSError):
                    await asyncio.sleep(0.5)
                    continue

                if addr[0] in self._ignorelist:
                    continue

                msg = data.decode(errors="ignore")
                if not msg.startswith("NOTIFY"):
                    continue

                response = self._parse_ssdp_response(msg)
                nts = response.get("NTS", "")
                usn = response.get("USN")
                st = response.get("NT")  # NOTIFY uses NT instead of ST

                if usn is None or st is None:
                    continue

                for es in self._endpoints_subscribers.values():
                    subscribers = es.search_subscribers.get(st, [])
                    if not subscribers:
                        continue

                    if nts == "ssdp:byebye":
                        for subscriber in subscribers:
                            if usn in subscriber.known:
                                ks = subscriber.known.pop(usn)
                                logger.debug(f"SSDP byebye: {ks.info.service_name} ({ks.info.addr})")
                                await subscriber.listener.ssdp_did_remove_service(self, ks.info)
                    elif nts == "ssdp:alive":
                        info = SSDPDiscoveryInfo(
                            addr=addr[0],
                            host=response.get("HOST"),
                            search_target=st,
                            service_name=usn,
                            server=response.get("SERVER"),
                            cache_control=response.get("CACHE-CONTROL"),
                            location=response.get("LOCATION"),
                            response=response,
                        )
                        await asyncio.gather(*[self._notify_subscriber(s, usn, info) for s in subscribers])
        finally:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, self._get_mreq("239.255.255.250"))
            sock.close()

    # SSDP Implementation

    def _collect_scan_replies(self, sock: socket.socket, mx: int) -> list[tuple[bytes, tuple[str, int]]]:
        """Blocking: reads all unicast M-SEARCH replies within the MX window. Runs in executor."""
        replies: list[tuple[bytes, tuple[str, int]]] = []
        sock.settimeout(mx + 0.5)  # slightly over MX so we catch late responders
        deadline = monotonic() + mx + 0.5
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
                replies.append((data, addr))
            except TimeoutError:
                break
            except OSError:
                break
        return replies

    async def _notify_subscriber(self, subscriber: _Subscriber, usn: str, info: SSDPDiscoveryInfo):
        now = monotonic()
        if usn not in subscriber.known:
            subscriber.known[usn] = _KnownService(info, now)
            logger.debug(f"SSDP discovered: {info.service_name} ({info.addr})")
            await subscriber.listener.ssdp_did_discover_service(self, info)
        else:
            known = subscriber.known[usn]
            subscriber.known[usn] = _KnownService(info, now)
            if info.location != known.info.location or info.server != known.info.server:
                logger.debug(f"SSDP updated: {info.service_name} ({info.addr})")
                await subscriber.listener.ssdp_did_update_service(self, info)

    async def _evict_expired(self):
        now = monotonic()
        missed_scans_cutoff = now - self._scan_interval * self._missed_scans_evict
        for es in self._endpoints_subscribers.values():
            for subscribers in es.search_subscribers.values():
                for subscriber in subscribers:
                    expired = [usn for usn, ks in subscriber.known.items() if ks.last_seen_at < missed_scans_cutoff]
                    for usn in expired:
                        ks = subscriber.known.pop(usn)
                        logger.debug(f"SSDP expired: {ks.info.service_name} ({ks.info.addr})")
                        await subscriber.listener.ssdp_did_remove_service(self, ks.info)

    async def _scan_and_read(self):
        """Send all M-SEARCH requests then collect replies for each endpoint."""
        loop = asyncio.get_running_loop()
        for endpoint, es in self._endpoints_subscribers.items():
            search_targets = list(es.search_subscribers.keys())
            for search_target in search_targets:
                query = self._ssdp_query_template(
                    ip=endpoint.mcast_group, port=str(endpoint.port), mx=str(self._mx), st=search_target
                )
                logger.debug(f"SSDP scan: {search_target} on {endpoint.mcast_group}:{endpoint.port}")
                es.scan_socket.sendto(query.encode(), (endpoint.mcast_group, endpoint.port))
            replies = await loop.run_in_executor(None, self._collect_scan_replies, es.scan_socket, self._mx)
            await self._process_scan_replies(endpoint, search_targets, replies)

    async def _process_scan_replies(
        self, endpoint: _Endpoint, search_targets: list[str], replies: list[tuple[bytes, tuple[str, int]]]
    ):
        for data, addr in replies:
            if addr[0] in self._ignorelist:
                continue
            response = self._parse_ssdp_response(data.decode(errors="ignore"))
            # 200 OK replies omit ST — fall back to matching against all search targets for this endpoint
            response_st = response.get("ST")
            # USN is standard UPnP but Yeelight (and others) omit it — fall back to Location then addr
            usn = response.get("USN") or response.get("LOCATION") or addr[0]
            search_subscribers = self._endpoints_subscribers[endpoint].search_subscribers
            candidates = [response_st] if response_st else search_targets
            for st in candidates:
                subscribers = search_subscribers.get(st, [])
                if not subscribers:
                    continue
                info = SSDPDiscoveryInfo(
                    addr=addr[0],
                    host=response.get("HOST"),
                    search_target=st,
                    service_name=usn,
                    server=response.get("SERVER"),
                    cache_control=response.get("CACHE-CONTROL"),
                    location=response.get("LOCATION"),
                    response=response,
                )
                await asyncio.gather(*[self._notify_subscriber(subscriber, usn, info) for subscriber in subscribers])

    # SSDP config

    def _create_scan_socket(self) -> socket.socket:
        # Ephemeral unbound socket — OS assigns a random source port.
        # Devices send M-SEARCH replies back to that source port (unicast),
        # so we must read replies on the same socket we sent from.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return sock

    def _get_mreq(self, mcast_group) -> bytes:
        return struct.pack("4sL", socket.inet_aton(mcast_group), socket.INADDR_ANY)

    def _ssdp_query_template(self, ip: str, port: str, mx: str, st: str) -> str:
        return f'M-SEARCH * HTTP/1.1\r\nMAN: "ssdp:discover"\r\nHOST: {ip}:{port}\r\nMX: {mx}\r\nST: {st}\r\n\r\n'

    def _parse_ssdp_response(self, response: str) -> dict[str, str]:
        lines = response.split("\r\n")
        data = {}
        for line in lines:
            if ":" in line:
                key, value = line.split(":", 1)
                data[key.strip().upper()] = value.strip()
        return data

    # Helpers

    def _local_ips(self) -> set[str]:
        """Returns all local IPv4 addresses to exclude from discovery responses."""
        ips: set[str] = {"127.0.0.1"}
        try:
            infos = socket.getaddrinfo(None, None, socket.AF_INET, socket.SOCK_DGRAM, 0, socket.AI_PASSIVE)
            ips.update(str(info[4][0]) for info in infos if info[4][0] != "0.0.0.0")
        except Exception:
            pass
        return ips


if __name__ == "__main__":
    # logging.basicConfig()
    # logger.setLevel(logging.DEBUG)

    async def main():
        ssdp = SSDPDiscoveryService()

        class SSDPListener:
            async def ssdp_did_discover_service(self, ssdp: SSDPDiscoveryService, info: SSDPDiscoveryInfo):
                print(f"Found: {info.service_name} @ {info.addr} location={info.location}")

            async def ssdp_did_update_service(self, ssdp: SSDPDiscoveryService, info: SSDPDiscoveryInfo):
                print(f"Updated: {info.service_name} @ {info.addr}")

            async def ssdp_did_remove_service(self, ssdp: SSDPDiscoveryService, info: SSDPDiscoveryInfo):
                print(f"Removed: {info.service_name} @ {info.addr}")

        ssdp.register(SSDPListener(), "wifi_bulb", port=1982)

        await ssdp.start()

        while True:
            await asyncio.sleep(1)

    asyncio.run(main())
