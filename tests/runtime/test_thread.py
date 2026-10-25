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
    form_network,
    otbr,
    silabs_multiprotocol,
)
from majordom_integration_sdk.testing import RecordingPeripheralOutput


@pytest.fixture
def border_router():
    """A border router's REST API with no network until one is put."""
    state: dict[str, Any] = {"dataset": "", "puts": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = state["dataset"].encode()
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            state["puts"].append((self.path, body))
            if self.path == "/node/dataset/active" and not state.get("stuck"):
                state["dataset"] = body if body.startswith("0e") else "0e08newnetwork"
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)  # HTTPServer would resolve the host name first
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()


async def test_returns_the_network_someone_else_formed(border_router):
    url, state = border_router
    state["dataset"] = "0e08existing"
    assert await active_dataset(url) == "0e08existing"
    await form_network(url, None)
    assert state["puts"] == []


async def test_forms_a_network_when_none_appears(border_router):
    url, state = border_router
    await form_network(url, None, patience=0)
    assert await active_dataset(url) == "0e08newnetwork"
    assert state["puts"] == [
        ("/node/dataset/active", json.dumps({"NetworkName": "MajorDom"})),
        ("/node/state", json.dumps("enable")),
    ]


async def test_joins_the_given_network(border_router):
    url, state = border_router
    await form_network(url, "0e08given", patience=0)
    assert await active_dataset(url) == "0e08given"


async def test_forms_only_once(border_router):
    url, state = border_router
    state["stuck"] = True  # forming takes a while: the dataset doesn't appear yet
    await asyncio.wait_for(form_network(url, None, patience=0), 3)
    assert len(state["puts"]) == 2  # one dataset, one enable
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(active_dataset(url), 2)


async def test_only_a_border_router_that_doesnt_form_itself_is_provisioned(border_router, monkeypatch):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0)
    rest = Endpoint("thread", "otbr-rest", url, is_shared=True)
    await asyncio.wait_for(silabs_multiprotocol().provision(rest), 2)  # its container forms the network
    assert state["puts"] == []
    await asyncio.wait_for(otbr().provision(rest), 3)
    assert len(state["puts"]) == 2


async def test_a_border_router_joins_the_hubs_thread_network(border_router, monkeypatch):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0)
    router, output = otbr(), RecordingPeripheralOutput()
    router.runtime, output.context = output, Context(thread_dataset="0e08hub")  # learned from the stick before
    await asyncio.wait_for(router.provision(Endpoint("thread", "otbr-rest", url, is_shared=True)), 3)
    assert await active_dataset(url) == "0e08hub"


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
