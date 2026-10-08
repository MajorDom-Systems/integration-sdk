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
        "hostname": "majordom-thread",  # its mDNS daemon announces this, not the hub's own name
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
            "test": ["CMD-SHELL", "pgrep -f tcp-listen && ot-ctl state | grep -qE 'leader|router|child'"],
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
        # Pinned: a moved :latest once shipped a broken RCP serial init (no wpan0). Bump on purpose.
        "image": "openthread/border-router@sha256:acc612303d3bd5aea28621ec8f3d2cc154cdc94c370398285dc828d08c5a5cf9",
        "privileged": True,
        "userns_mode": "host",
        "network_mode": "host",
        "hostname": "majordom-thread",  # its mDNS daemon announces this, not the hub's own name
        "devices": ["{links.thread.url}:/dev/ttyRCP", "/dev/net/tun:/dev/net/tun"],
        "volumes": ["{data}:/data"],
        "tmpfs": ["/run:exec,mode=1777", "/tmp:mode=1777"],  # s6-overlay runs its init from /run
        "environment": {
            "OT_RCP_DEVICE": "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate=460800&uart-flow-control",
            "OT_INFRA_IF": "{host.interface}",
            "OT_THREAD_IF": "wpan0",
            "OT_REST_LISTEN_ADDR": "0.0.0.0",
        },
        "healthcheck": {
            "test": ["CMD-SHELL", "ot-ctl state | grep -qE 'leader|router|child'"],
            "interval": "10s",
            "start_period": "60s",
            "retries": 3,
        },
    },
)

SERVICES = [multiprotocol, otbr]

#: Services whose container forms its own Thread network (the runtime must not form a second one).
SELF_FORMING = {multiprotocol.name}


async def thread_dataset(url: str, network: ThreadNetwork, form: bool = True, patience: float = 15) -> str:
    """Wait for the border router at ``url`` and return its active dataset as hex TLVs. When ``form``
    and it has answered for ``patience`` seconds without one, join ``network.dataset`` or form a new
    network — once."""

    def call(method: str, path: str, body: str | None = None, kind: str = "text/plain") -> str:
        request = urllib.request.Request(
            url + path, method=method, data=body.encode() if body is not None else None,
            headers={"Accept": "text/plain", "Content-Type": kind},
        )  # fmt: skip
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode().strip()

    def form_network():
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
            if form and answered > patience:
                form = False
                await asyncio.to_thread(form_network)
        except OSError:
            pass  # not up yet
        await asyncio.sleep(1)
