import json
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer

import pytest

from majordom_transports.services import ThreadNetwork, thread_dataset


@pytest.fixture
def border_router():
    """A border router's REST API with no network until one is put."""
    state = {"dataset": "", "puts": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = state["dataset"].encode()
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            state["puts"].append((self.path, body))
            if self.path == "/node/dataset/active":
                state["dataset"] = body if body.startswith("0e") else "0e08newnetwork"
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)  # HTTPServer would resolve the host name first
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()


async def test_returns_the_network_someone_else_formed(border_router):
    url, state = border_router
    state["dataset"] = "0e08existing"
    assert await thread_dataset(url, ThreadNetwork()) == "0e08existing"
    assert state["puts"] == []


async def test_forms_a_network_when_none_appears(border_router):
    url, state = border_router
    assert await thread_dataset(url, ThreadNetwork(), patience=0) == "0e08newnetwork"
    assert state["puts"] == [
        ("/node/dataset/active", json.dumps({"NetworkName": "MajorDom"})),
        ("/node/state", json.dumps("enable")),
    ]


async def test_joins_the_configured_network(border_router):
    url, state = border_router
    assert await thread_dataset(url, ThreadNetwork(dataset="0e08configured"), patience=0) == "0e08configured"
