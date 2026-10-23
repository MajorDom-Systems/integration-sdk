import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from typing import Any

import pytest

from majordom_integration_sdk import Endpoint
from majordom_integration_sdk.runtime.peripherals.services import builtin as services
from majordom_integration_sdk.runtime.peripherals.services.builtin import (
    ThreadNetwork,
    active_dataset,
    form_network,
    otbr,
    silabs_multiprotocol,
)


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
    await form_network(url, ThreadNetwork())
    assert state["puts"] == []


async def test_forms_a_network_when_none_appears(border_router):
    url, state = border_router
    await form_network(url, ThreadNetwork(), patience=0)
    assert await active_dataset(url) == "0e08newnetwork"
    assert state["puts"] == [
        ("/node/dataset/active", json.dumps({"NetworkName": "MajorDom"})),
        ("/node/state", json.dumps("enable")),
    ]


async def test_joins_the_configured_network(border_router):
    url, state = border_router
    await form_network(url, ThreadNetwork(dataset="0e08configured"), patience=0)
    assert await active_dataset(url) == "0e08configured"


async def test_forms_only_once(border_router):
    url, state = border_router
    state["stuck"] = True  # forming takes a while: the dataset doesn't appear yet
    await asyncio.wait_for(form_network(url, ThreadNetwork(), patience=0), 3)
    assert len(state["puts"]) == 2  # one dataset, one enable
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(active_dataset(url), 2)


async def test_only_a_border_router_that_doesnt_form_itself_is_provisioned(border_router, monkeypatch):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0)
    rest = Endpoint("thread", "otbr-rest", url, shared=True)
    await asyncio.wait_for(silabs_multiprotocol.provision(rest, ThreadNetwork()), 2)  # its container forms the network
    assert state["puts"] == []
    await asyncio.wait_for(otbr.provision(rest, ThreadNetwork()), 3)
    assert len(state["puts"]) == 2


async def test_border_routers_tell_their_thread_network(border_router):
    url, state = border_router
    state["dataset"] = "0e08existing"
    assert await otbr.learn(Endpoint("thread", "otbr-rest", url)) == {"thread_dataset": "0e08existing"}
    assert await silabs_multiprotocol.learn(Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")) == {}
