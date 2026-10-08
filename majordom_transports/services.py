"""Services majordom-transports runs between radios and integrations."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

from majordom_integration_sdk import Endpoint, Need, Service
from pydantic import BaseModel, Field

_IMAGES = Path(__file__).parent / "images"


class ThreadNetwork(BaseModel):
    dataset: str | None = Field(
        None,
        title="Thread network",
        description="Active operational dataset (hex) of an existing network to join. Empty forms a new one.",
    )
    tx_power: int | None = Field(None, title="Thread transmit power, dBm", description="Empty uses the radio default.")


_OTBR_REST = Endpoint("thread", "otbr-rest", "http://127.0.0.1:8081", shared=True)

#: One multi-PAN RCP serving Zigbee (zigbeed, EZSP over TCP) and Thread (OTBR) through cpcd.
multiprotocol = Service(
    "multiprotocol",
    needs=[Need("cpc", ["uart"])],
    provides=[Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999"), _OTBR_REST],
    settings=ThreadNetwork,
    compose={
        "image": "majordom/silabs-multiprotocol:gsdk-4.4.6-arm64",
        "build": str(_IMAGES / "multiprotocol"),
        "privileged": True,
        "userns_mode": "host",
        "network_mode": "host",
        "devices": ["{links.cpc.url}:/dev/ttyACM0", "/dev/net/tun:/dev/net/tun"],
        "volumes": [
            f"{_IMAGES}/multiprotocol/cpcd.conf:/usr/local/etc/cpcd.conf:ro",
            f"{_IMAGES}/multiprotocol/entrypoint.sh:/entrypoint.sh:ro",
            "{data}/thread:/var/lib/thread",
            "{data}/zigbee:/var/lib/zigbee",
        ],
        "tmpfs": ["/run:exec,mode=1777"],
        "entrypoint": ["/bin/bash", "/entrypoint.sh"],
        "environment": {
            "BACKBONE_IF": "{host.interface}",
            "THREAD_DATASET": "{settings.dataset}",
            "HARNESS_TX_DBM": "{settings.tx_power}",
        },
        "healthcheck": {
            "test": ["CMD-SHELL", "pgrep -x cpcd && pgrep -x zigbeed && pgrep -x otbr-agent && pgrep -f tcp-listen"],
            "interval": "10s",
            "start_period": "120s",
            "retries": 3,
        },
    },
)

#: OpenThread Border Router on a plain Thread RCP.
otbr = Service(
    "otbr",
    needs=[Need("thread", ["spinel"])],
    provides=[_OTBR_REST],
    settings=ThreadNetwork,
    compose={
        "image": "openthread/border-router:latest",
        "privileged": True,
        "userns_mode": "host",
        "network_mode": "host",
        "devices": ["{links.thread.url}:/dev/ttyRCP", "/dev/net/tun:/dev/net/tun"],
        "volumes": ["{data}:/data"],
        "tmpfs": ["/run:exec,mode=1777", "/tmp:mode=1777"],  # s6-overlay runs its init from /run
        "environment": {
            "OT_RCP_DEVICE": "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate=460800&uart-flow-control",
            "OT_INFRA_IF": "{host.interface}",
            "OT_THREAD_IF": "wpan0",
            "OT_REST_LISTEN_ADDR": "0.0.0.0",
        },
        "healthcheck": {"test": ["CMD-SHELL", "ot-ctl state"], "interval": "10s", "start_period": "60s", "retries": 3},
    },
)

SERVICES = [multiprotocol, otbr]


async def thread_dataset(url: str, network: ThreadNetwork, patience: float = 15) -> str:
    """Wait for the border router at ``url`` and return its active dataset as hex TLVs. When it has
    answered for ``patience`` seconds without one (nobody else formed a network), join
    ``network.dataset`` or form a new network."""

    def call(method: str, path: str, body: str | None = None, kind: str = "text/plain") -> str:
        request = urllib.request.Request(
            url + path, method=method, data=body.encode() if body is not None else None,
            headers={"Accept": "text/plain", "Content-Type": kind},
        )  # fmt: skip
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode().strip()

    def form():
        if network.dataset:
            call("PUT", "/node/dataset/active", network.dataset)
        else:
            call("PUT", "/node/dataset/active", json.dumps({"NetworkName": "MajorDom"}), "application/json")
        call("PUT", "/node/state", json.dumps("enable"), "application/json")

    answered = 0.0
    while True:
        try:
            if active := await asyncio.to_thread(call, "GET", "/node/dataset/active"):
                return active
            answered += 1
            if answered > patience:
                await asyncio.to_thread(form)
        except OSError:
            pass  # not up yet
        await asyncio.sleep(1)
