"""The containers the SDK brings between radios and integrations: Thread border routers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from majordom_integration_sdk.peripherals import Endpoint, Need, Service

_IMAGES = Path(__file__).parent / "images"


class ThreadRadio(BaseModel):
    tx_power: int | None = Field(None, title="Thread transmit power, dBm", description="Empty uses the radio default.")


_OTBR_REST = Endpoint("thread", "otbr-rest", "http://127.0.0.1:{ports.rest}", is_shared=True)
FORM_PATIENCE = 15  # seconds a border router answers without a network before one is formed on it
POLL = 1  # seconds between asks of a border router that isn't there yet
MOVE_DELAY = 10_000  # ms before a network moves to another channel, so its devices move along, as the entrypoints do
_CHANNEL, _EXTENDED_PAN_ID, _ACTIVE_TIMESTAMP, _PENDING_TIMESTAMP, _DELAY_TIMER = 0, 2, 14, 51, 52  # dataset TLVs


class _BorderRouter[S: BaseModel = Any](Service[S]):
    """On the hub's Thread network, ``platform.thread_dataset``: set up over its REST API to join it, or to form one
    when there is none yet; it tells the network it has once it serves. The network is never rendered into its
    container, so learning it recreates nothing."""

    async def provision(self, endpoint: Endpoint):
        if endpoint.dialect == _OTBR_REST.dialect:
            await join_network(endpoint.url, self.platform.thread_dataset, FORM_PATIENCE)

    async def start(self):
        await super().start()
        rest = next(endpoint for endpoint in self.provides if endpoint.dialect == _OTBR_REST.dialect)
        await self._align(rest.url)
        await self.output.peripheral_did_learn(self, {"thread_dataset": await active_dataset(rest.url)})

    async def _align(self, url: str):
        """Put the network it joined where its radio needs it, before it is told: nowhere in particular here."""
        return


class _MultiPanBorderRouter(_BorderRouter[ThreadRadio]):
    """One multi-PAN radio listens on one channel: the Thread network follows Zigbee's, the one its container put
    Thread on before it answered (it forms there, and moves a network it restored on another)."""

    _zigbee_channel: int | None = None  # Zigbee's channel, read before the network is joined

    async def provision(self, endpoint: Endpoint):
        if endpoint.dialect == _OTBR_REST.dialect:
            self._zigbee_channel = await network_channel(endpoint.url)
            await super().provision(endpoint)

    async def _align(self, url: str):
        if self._zigbee_channel is not None:
            await move_channel(url, self._zigbee_channel, FORM_PATIENCE)  # one that can't land fails the start


def silabs_multiprotocol() -> Service[ThreadRadio]:
    """One multi-PAN RCP serving Zigbee (zigbeed, EZSP over TCP) and Thread (OTBR) through cpcd."""
    return _MultiPanBorderRouter(
        "silabs-multiprotocol",
        needs=[Need("multipan", ["silabs-cpc"])],
        provides=[Endpoint("zigbee", "ezsp", "socket://127.0.0.1:{ports.ezsp}"), _OTBR_REST],
        settings=ThreadRadio,
        ports={"ezsp": 9999, "rest": 8081},
        compose={
            "image": "majordom/multiprotocol:sisdk-2025.6.3-avahi-arm64",
            "build": str(_IMAGES / "multiprotocol-sisdk"),
            "privileged": True,
            "userns_mode": "host",
            "network_mode": "host",
            "hostname": "majordom-thread",  # its mDNS daemon announces this, not the hub's own name
            # a privileged container sees the host's /dev: a common name (ttyACM0) would be the host's own device
            "devices": ["{peripherals.multipan.url}:/dev/ttyRCP", "/dev/net/tun:/dev/net/tun"],
            "volumes": [
                f"{_IMAGES}/multiprotocol-sisdk/entrypoint.sh:/entrypoint.sh:ro",
                f"{_IMAGES}/thread-source/run:/thread-source:ro",
                # the avahi build publishes through the host's avahi-daemon, on the host's bus
                "{host.avahi:?/run/dbus:/run/dbus}",
                "{data}/thread:/var/lib/thread",
                "{data}/zigbee:/var/lib/zigbee",
            ],
            "tmpfs": ["/run:exec,mode=1777"],
            "entrypoint": ["/bin/bash", "/entrypoint.sh"],
            "environment": {
                "RADIO_DEVICE": "/dev/ttyRCP",
                "RADIO_BAUD": "{peripherals.multipan.params[baud]}",
                "RADIO_HARDFLOW": "{peripherals.multipan.params[flow_control]:?true}"
                "{peripherals.multipan.params[flow_control]:!false}",
                "BACKBONE_IF": "{host.interface}",
                "EZSP_TCP_PORT": "{ports.ezsp}",
                "OTBR_REST_PORT": "{ports.rest}",
                "LISTEN_ADDRESS": "{host.listen_address}",
                "THREAD_TX_DBM": "{settings.tx_power}",
            },
            "healthcheck": {
                "test": ["CMD-SHELL", "pgrep -f tcp-listen && ot-ctl state | grep -qE 'leader|router|child'"],
                "interval": "10s",
                "start_period": "120s",
                "retries": 3,
            },
        },
    )


def otbr() -> Service:
    """OpenThread Border Router on a plain Thread RCP."""
    return _BorderRouter(
        "otbr",
        needs=[Need("thread", ["spinel"])],
        provides=[_OTBR_REST],
        ports={"rest": 8081},
        compose={
            # Pinned: a moved :latest once shipped a broken RCP serial init (no wpan0). Bump on purpose.
            "image": "openthread/border-router@sha256:acc612303d3bd5aea28621ec8f3d2cc154cdc94c370398285dc828d08c5a5cf9",
            "privileged": True,
            "userns_mode": "host",
            "network_mode": "host",
            "hostname": "majordom-thread",  # its mDNS daemon announces this, not the hub's own name
            "devices": ["{peripherals.thread.url}:/dev/ttyRCP", "/dev/net/tun:/dev/net/tun"],
            "volumes": ["{data}:/data", f"{_IMAGES}/thread-source:/etc/services.d/thread-source:ro"],
            "tmpfs": ["/run:exec,mode=1777", "/tmp:mode=1777"],  # s6-overlay runs its init from /run
            "environment": {
                "OT_RCP_DEVICE": "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate={peripherals.thread.params[baud]}"
                "{peripherals.thread.params[flow_control]:&uart-flow-control}",
                "OT_INFRA_IF": "{host.interface}",
                "OT_THREAD_IF": "wpan0",
                "OT_REST_LISTEN_ADDR": "{host.listen_address}",
                "OT_REST_LISTEN_PORT": "{ports.rest}",
            },
            "healthcheck": {
                "test": ["CMD-SHELL", "ot-ctl state | grep -qE 'leader|router|child'"],
                "interval": "10s",
                "start_period": "60s",
                "retries": 3,
            },
        },
    )


def _call(url: str, method: str, path: str, body: str | None = None, kind: str = "text/plain") -> str:
    request = urllib.request.Request(
        url + path, method=method, data=body.encode() if body is not None else None,
        headers={"Accept": "text/plain", "Content-Type": kind},
    )  # fmt: skip
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode().strip()


def _datasets(url: str) -> tuple[str, str]:
    """The active and the pending dataset of the border router at ``url`` as hex TLVs, empty when it has none; one
    pending for another network (the one it had before it joined) is none: it moves nothing of this one."""
    pending = _call(url, "GET", "/node/dataset/pending")  # first: a move landing meanwhile is in the active one
    active = _call(url, "GET", "/node/dataset/active")
    if pending and _tlvs(pending).get(_EXTENDED_PAN_ID) != _tlvs(active).get(_EXTENDED_PAN_ID):
        pending = ""
    return active, pending


async def active_dataset(url: str) -> str:
    """Wait for the border router at ``url`` to have a network, and no change of it pending (a channel move landing);
    its active dataset as hex TLVs."""
    while True:
        with contextlib.suppress(OSError):  # not up yet
            active, pending = await asyncio.to_thread(_datasets, url)
            if active and not pending:
                return active
        await asyncio.sleep(POLL)


async def network_channel(url: str) -> int:
    """Wait for the border router at ``url`` to have a network; the channel it is on, or moving to."""
    while True:
        with contextlib.suppress(OSError):  # not up yet
            active, pending = await asyncio.to_thread(_datasets, url)
            if active:
                return _channel(pending or active)
        await asyncio.sleep(POLL)


async def move_channel(url: str, channel: int, patience: float = FORM_PATIENCE):
    """Move the network of the border router at ``url`` to ``channel`` through a pending dataset, which its devices
    follow in `MOVE_DELAY`, unless it is there or on its way; TimeoutError once it couldn't for ``patience`` seconds."""
    waited = 0.0
    while True:
        try:
            active, pending = await asyncio.to_thread(_datasets, url)
            if _channel(pending or active) != channel:
                await asyncio.to_thread(_call, url, "PUT", "/node/dataset/pending", _moved(active, channel))
            return
        except OSError as error:  # not up, or not attached yet: a pending dataset needs a network to go to
            if waited >= patience:
                raise TimeoutError(f"The Thread network didn't move to Zigbee's channel {channel}: {error}") from None
        waited += POLL
        await asyncio.sleep(POLL)


async def join_network(url: str, dataset: str | None, patience: float = FORM_PATIENCE):
    """Have the border router at ``url`` join the network of ``dataset`` (its active dataset, hex TLVs) in place of
    another one it has, or once it has answered for ``patience`` seconds without one; with no ``dataset``, form a new
    network then — once. Thread is on once it returns."""

    def put(is_running: bool):
        if is_running:  # the active dataset is written only while Thread is off
            _call(url, "PUT", "/node/state", json.dumps("disable"), "application/json")
        if dataset:
            _call(url, "PUT", "/node/dataset/active", dataset)
        else:
            _call(url, "PUT", "/node/dataset/active", json.dumps({"NetworkName": "MajorDom"}), "application/json")
        enable()

    def enable():
        _call(url, "PUT", "/node/state", json.dumps("enable"), "application/json")

    answered = 0.0
    while True:
        try:
            if active := await asyncio.to_thread(_call, url, "GET", "/node/dataset/active"):
                if dataset and _tlvs(active)[_EXTENDED_PAN_ID] != _tlvs(dataset)[_EXTENDED_PAN_ID]:
                    await asyncio.to_thread(put, True)
                elif "disabled" in await asyncio.to_thread(_call, url, "GET", "/node/state"):
                    await asyncio.to_thread(enable)  # a join or a forming whose enable failed, tried again
                return
            answered += POLL
            if answered > patience:
                await asyncio.to_thread(put, False)
                return
        except OSError:
            pass  # not up yet
        await asyncio.sleep(POLL)


def _tlvs(dataset: str) -> dict[int, bytes]:
    """A dataset's TLVs by type: the Extended PAN ID tells networks apart, whatever moved since (the channel)."""
    data, tlvs = bytes.fromhex(dataset), {}
    while len(data) >= 2:
        tlvs[data[0]], data = data[2 : 2 + data[1]], data[2 + data[1] :]
    return tlvs


def _channel(dataset: str) -> int:
    return int.from_bytes(_tlvs(dataset)[_CHANNEL][1:])  # after the channel page


def _moved(active: str, channel: int) -> str:
    """A pending dataset moving the network of ``active`` to ``channel`` in `MOVE_DELAY` ms."""
    tlvs = _tlvs(active)
    # newer than the network's own, which a clock still behind (no RTC, before NTP) wouldn't be: seconds, then no
    # ticks and not authoritative
    seconds = max(int(time.time()), (int.from_bytes(tlvs.get(_ACTIVE_TIMESTAMP, b"")) >> 16) + 1)
    now = (seconds << 16).to_bytes(8)
    moved = {_CHANNEL: b"\0" + channel.to_bytes(2), _ACTIVE_TIMESTAMP: now, _PENDING_TIMESTAMP: now}
    tlvs = tlvs | moved | {_DELAY_TIMER: MOVE_DELAY.to_bytes(4)}
    return b"".join(bytes([kind, len(value)]) + value for kind, value in tlvs.items()).hex()
