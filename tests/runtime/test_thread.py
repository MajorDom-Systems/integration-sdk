import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from typing import Any

import pytest

from majordom_integration_sdk import Context, Endpoint, Peripherals, Service
from majordom_integration_sdk.runtime.peripherals.services import builtin as services
from majordom_integration_sdk.runtime.peripherals.services.builtin import (
    active_dataset,
    join_network,
    otbr,
    silabs_multiprotocol,
)
from majordom_integration_sdk.testing import RecordingPeripheralOutput


def dataset(network: str, channel: int = 15) -> str:
    """An active dataset's hex TLVs: its timestamp, channel and Extended PAN ID (the network)."""
    return f"0e080000000000010000000300{channel:04x}0208{network}"


HUB, OTHER = dataset("dead00beef00cafe"), dataset("0123456789abcdef")
ENABLE, DISABLE = ("/node/state", json.dumps("enable")), ("/node/state", json.dumps("disable"))
PENDING = "/node/dataset/pending"


def rest(url: str) -> Endpoint:
    return Endpoint("thread", "otbr-rest", url, is_shared=True)


@pytest.fixture
def border_router():
    """A border router's REST API with no network until one is put, and no change of it pending."""
    state: dict[str, Any] = {"dataset": "", "pending": "", "puts": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = state["pending" if self.path == PENDING else "dataset"].encode()
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            state["puts"].append((self.path, body))
            if self.path == "/node/dataset/active" and not state.get("stuck"):
                state["dataset"] = body if body.startswith("0e") else "0e08newnetwork"
            elif self.path == PENDING:
                state["pending"] = body
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)  # HTTPServer would resolve the host name first
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


async def test_returns_the_network_someone_else_formed(border_router):
    url, state = border_router
    state["dataset"] = "0e08existing"
    assert await active_dataset(url) == "0e08existing"
    await join_network(url, None)
    assert state["puts"] == []


async def test_forms_a_network_when_none_appears(border_router):
    url, state = border_router
    await join_network(url, None, patience=0)
    assert await active_dataset(url) == "0e08newnetwork"
    assert state["puts"] == [("/node/dataset/active", json.dumps({"NetworkName": "MajorDom"})), ENABLE]


async def test_joins_the_given_network(border_router):
    url, state = border_router
    await join_network(url, "0e08given", patience=0)
    assert await active_dataset(url) == "0e08given"


async def test_joins_the_given_network_in_place_of_another_it_has(border_router):
    url, state = border_router
    state["dataset"] = OTHER  # its container formed one before the hub's was known
    await join_network(url, HUB)
    assert state["puts"] == [DISABLE, ("/node/dataset/active", HUB), ENABLE]  # written only while Thread is off


async def test_keeps_the_given_network_after_it_moved(border_router):
    url, state = border_router
    state["dataset"] = dataset("dead00beef00cafe", channel=20)  # the same network, on another channel since
    await join_network(url, HUB)
    assert state["puts"] == []


async def test_forms_only_once(border_router):
    url, state = border_router
    state["stuck"] = True  # forming takes a while: the dataset doesn't appear yet
    await asyncio.wait_for(join_network(url, None, patience=0), 3)
    assert len(state["puts"]) == 2  # one dataset, one enable
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(active_dataset(url), 2)


@pytest.mark.parametrize(("router", "formed"), [(otbr, ""), (silabs_multiprotocol, OTHER)])
async def test_a_border_router_is_provisioned_onto_the_hubs_thread_network(border_router, monkeypatch, router, formed):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0)
    state["dataset"] = formed  # the multiprotocol container forms one of its own, as it warms the radio up
    service, output = router(), RecordingPeripheralOutput()
    service.runtime, output.context = output, Context(thread_dataset=HUB)  # learned from the stick before
    await asyncio.wait_for(service.provision(Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")), 1)
    await asyncio.wait_for(service.provision(rest(url)), 3)
    assert await active_dataset(url) == HUB
    assert PENDING not in [path for path, _ in state["puts"]]  # on the same channel


async def test_a_multiprotocol_router_moves_the_network_it_joins_to_zigbees_channel(border_router):
    url, state = border_router
    state["dataset"] = dataset("0123456789abcdef", channel=15)  # its container formed it on Zigbee's channel
    service, output = silabs_multiprotocol(), RecordingPeripheralOutput()
    service.runtime, output.context = output, Context(thread_dataset=dataset("dead00beef00cafe", channel=20))
    await asyncio.wait_for(service.provision(rest(url)), 3)

    assert state["dataset"] == output.context.thread_dataset  # joined
    pending = state["pending"]  # the devices on it follow: a new active and a pending timestamp, and a delay
    assert "000300000f" in pending and "0208dead00beef00cafe" in pending and "3308" in pending and "3404" in pending


async def test_a_standalone_router_keeps_the_channel_of_the_network_it_joins(border_router):
    url, state = border_router
    state["dataset"] = dataset("0123456789abcdef", channel=15)
    service, output = otbr(), RecordingPeripheralOutput()
    service.runtime, output.context = output, Context(thread_dataset=dataset("dead00beef00cafe", channel=20))
    await asyncio.wait_for(service.provision(rest(url)), 3)
    assert state["pending"] == ""


async def test_learns_the_network_only_once_a_channel_move_landed(border_router):
    url, state = border_router
    moved = dataset("dead00beef00cafe", channel=15)
    state["dataset"], state["pending"] = dataset("dead00beef00cafe", channel=20), moved
    learning = asyncio.create_task(active_dataset(url))
    await asyncio.sleep(1.5)
    assert not learning.done()  # Matter would commission devices onto the channel it leaves

    state["dataset"], state["pending"] = moved, ""
    assert await asyncio.wait_for(learning, 3) == moved


class Answering:
    """Runs nothing: the border router at ``url`` answers already."""

    def __init__(self, url: str):
        self.url = url

    def provided(self, service: Service) -> list[Endpoint]:
        return [replace(endpoint, url=self.url) for endpoint in service.templates]

    async def up(self, service: Service):
        pass

    async def down(self, service: Service):
        pass

    def is_current(self, service: Service) -> bool:
        return True


@pytest.mark.parametrize("router", [otbr, silabs_multiprotocol])
async def test_a_border_router_tells_its_thread_network_once_it_serves(border_router, router):
    url, state = border_router
    state["dataset"] = "0e08existing"
    service, output = router(), RecordingPeripheralOutput()
    service.peripherals = Peripherals({"container": Endpoint("container", "compose", instance=Answering(url))})
    service.runtime = output
    await asyncio.wait_for(service.start(), 3)
    assert output.learned == [(service, {"thread_dataset": "0e08existing"})]
