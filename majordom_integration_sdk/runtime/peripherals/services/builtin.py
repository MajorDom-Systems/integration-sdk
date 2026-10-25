"""The containers the SDK brings between radios and integrations: Thread border routers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from majordom_integration_sdk.peripherals import Endpoint, Need, Service

_IMAGES = Path(__file__).parent / "images"


class ThreadNetwork(BaseModel):
    dataset: str | None = Field(
        None,
        title="Thread network",
        description="Active operational dataset (hex) of an existing network to join. Empty forms a new one.",
    )
    tx_power: int | None = Field(None, title="Thread transmit power, dBm", description="Empty uses the radio default.")


_OTBR_REST = Endpoint("thread", "otbr-rest", "http://127.0.0.1:{ports.rest}", is_shared=True)
FORM_PATIENCE = 15  # seconds a border router answers without a network before one is formed on it


class _BorderRouter(Service):
    """Its Thread network reaches integrations as ``context.thread_dataset``; its container forms it."""

    async def learn(self, endpoint: Endpoint) -> Mapping[str, Any]:
        return {"thread_dataset": await active_dataset(endpoint.url)} if endpoint.dialect == "otbr-rest" else {}


class _FormingBorderRouter(_BorderRouter):
    """A border router that comes up without a network: the runtime joins the configured one or forms one."""

    settings: ThreadNetwork | None

    async def provision(self, endpoint: Endpoint):
        await form_network(endpoint.url, self.settings or ThreadNetwork(), FORM_PATIENCE)


#: One multi-PAN RCP serving Zigbee (zigbeed, EZSP over TCP) and Thread (OTBR) through cpcd.
silabs_multiprotocol = _BorderRouter(
    "silabs-multiprotocol",
    needs=[Need("multipan", ["silabs-cpc"])],
    provides=[Endpoint("zigbee", "ezsp", "socket://127.0.0.1:{ports.ezsp}"), _OTBR_REST],
    settings=ThreadNetwork,
    ports={"ezsp": 9999, "rest": 8081},
    compose={
        "image": "majordom/silabs-multiprotocol:gsdk-4.4.6-arm64",
        "build": str(_IMAGES / "multiprotocol"),
        "privileged": True,
        "userns_mode": "host",
        "network_mode": "host",
        "hostname": "majordom-thread",  # its mDNS daemon announces this, not the hub's own name
        # a privileged container sees the host's /dev: a common name (ttyACM0) would be the host's own device
        "devices": ["{peripherals.multipan.url}:/dev/ttyRCP", "/dev/net/tun:/dev/net/tun"],
        "volumes": [
            f"{_IMAGES}/multiprotocol/cpcd.conf:/usr/local/etc/cpcd.conf:ro",
            f"{_IMAGES}/multiprotocol/entrypoint.sh:/entrypoint.sh:ro",
            f"{_IMAGES}/thread-source/run:/thread-source:ro",
            "{data}/thread:/var/lib/thread",
            "{data}/zigbee:/var/lib/zigbee",
        ],
        "tmpfs": ["/run:exec,mode=1777"],
        "entrypoint": ["/bin/bash", "/entrypoint.sh"],
        "environment": {
            "RADIO_BAUD": "{peripherals.multipan.params[baud]}",
            "RADIO_FLOW_CONTROL": "{peripherals.multipan.params[flow_control]}",
            "BACKBONE_IF": "{host.interface}",
            "EZSP_TCP_PORT": "{ports.ezsp}",
            "OTBR_REST_PORT": "{ports.rest}",
            "THREAD_DATASET": "{settings.dataset}",
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

#: OpenThread Border Router on a plain Thread RCP.
otbr = _FormingBorderRouter(
    "otbr",
    needs=[Need("thread", ["spinel"])],
    provides=[_OTBR_REST],
    settings=ThreadNetwork,
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
            "OT_REST_LISTEN_ADDR": "0.0.0.0",
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


async def active_dataset(url: str) -> str:
    """Wait for the border router at ``url`` to have a network; its active dataset as hex TLVs."""
    while True:
        with contextlib.suppress(OSError):  # not up yet
            if active := await asyncio.to_thread(_call, url, "GET", "/node/dataset/active"):
                return active
        await asyncio.sleep(1)


async def form_network(url: str, network: ThreadNetwork, patience: float = FORM_PATIENCE):
    """Once the border router at ``url`` has answered for ``patience`` seconds without a network, join
    ``network.dataset`` or form a new network — once."""

    def form():
        if network.dataset:
            _call(url, "PUT", "/node/dataset/active", network.dataset)
        else:
            _call(url, "PUT", "/node/dataset/active", json.dumps({"NetworkName": "MajorDom"}), "application/json")
        _call(url, "PUT", "/node/state", json.dumps("enable"), "application/json")

    answered = 0.0
    while True:
        try:
            if await asyncio.to_thread(_call, url, "GET", "/node/dataset/active"):
                return
            answered += 1
            if answered > patience:
                await asyncio.to_thread(form)
                return
        except OSError:
            pass  # not up yet
        await asyncio.sleep(1)
