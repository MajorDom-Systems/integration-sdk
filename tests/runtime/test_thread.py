import asyncio
import contextlib
import json
import threading
from collections.abc import Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from typing import Any, cast

import pytest

from majordom_integration_sdk import Endpoint, Peripheral, Peripherals, Service
from majordom_integration_sdk.runtime.peripherals.services import builtin as services
from majordom_integration_sdk.runtime.peripherals.services.builtin import (
    active_dataset,
    join_network,
    move_channel,
    network_channel,
    otbr,
    silabs_multiprotocol,
)
from majordom_integration_sdk.testing import RecordingPeripheralOutput, build_test_platform


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
    """A border router's REST API with no network until one is put, and no change of it pending: one put lands at
    once, one pending while ``landing`` lands right after its active dataset is read. It refuses puts to the path
    ``refused`` names, and fails each put in ``failing`` once."""
    state: dict[str, Any] = {"dataset": "", "pending": "", "thread": "leader", "puts": [], "failing": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            read = {PENDING: state["pending"], "/node/state": json.dumps(state["thread"])}
            body = read.get(self.path, state["dataset"]).encode()
            self.send_response(200 if body else 204)
            self.end_headers()
            self.wfile.write(body)
            if self.path == "/node/dataset/active" and state.get("landing"):
                state["dataset"], state["pending"], state["landing"] = state["pending"], "", False

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            state["puts"].append((self.path, body))
            if self.path == state.get("refused") or (self.path, body) in state["failing"]:
                with contextlib.suppress(ValueError):
                    state["failing"].remove((self.path, body))
                self.send_response(409)
                self.end_headers()
                return
            if self.path == "/node/state":
                state["thread"] = "disabled" if body == json.dumps("disable") else "leader"
            if self.path == "/node/dataset/active" and not state.get("stuck"):
                state["dataset"] = body if body.startswith("0e") else "0e08newnetwork"
            elif self.path == PENDING:
                state["dataset"] = body  # its delay passed
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)  # HTTPServer would resolve the host name first
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
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


async def test_a_join_whose_enable_failed_once_turns_thread_on_when_tried_again(border_router):
    url, state = border_router
    state["dataset"], state["failing"] = OTHER, [ENABLE]  # otbr-agent busy a moment
    await join_network(url, HUB)
    assert state["puts"] == [DISABLE, ("/node/dataset/active", HUB), ENABLE, ENABLE]
    assert state["thread"] == "leader"


async def test_a_forming_whose_enable_failed_once_turns_thread_on_when_tried_again(border_router):
    url, state = border_router
    state["thread"], state["failing"] = "disabled", [ENABLE]  # no network yet: Thread is off
    await join_network(url, None, patience=0)
    assert state["puts"][-2:] == [ENABLE, ENABLE] and state["thread"] == "leader"


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
        await asyncio.wait_for(active_dataset(url), 0.2)


@pytest.mark.parametrize(("router", "formed"), [(otbr, ""), (silabs_multiprotocol, OTHER)])
async def test_a_border_router_is_provisioned_onto_the_hubs_thread_network(border_router, monkeypatch, router, formed):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0)
    state["dataset"] = formed  # the multiprotocol container forms one of its own, as it warms the radio up
    service = serving(router(), url, thread_dataset=HUB)  # learned from the stick before
    await asyncio.wait_for(service.start(), 3)
    assert await active_dataset(url) == HUB
    assert PENDING not in [path for path, _ in state["puts"]]  # on the same channel


async def test_a_multiprotocol_router_moves_the_network_it_joins_to_zigbees_channel(border_router):
    url, state = border_router
    state["dataset"] = dataset("0123456789abcdef", channel=15)  # its container formed it on Zigbee's channel
    service = serving(silabs_multiprotocol(), url, thread_dataset=dataset("dead00beef00cafe", channel=20))
    await asyncio.wait_for(service.start(), 3)

    assert state["puts"][1] == ("/node/dataset/active", service.platform.thread_dataset)  # joined
    pending = dict(state["puts"])[PENDING]  # the devices on it follow: new active and pending timestamps, a delay
    assert "000300000f" in pending and "0208dead00beef00cafe" in pending and "3308" in pending and "3404" in pending


async def test_a_change_pending_on_another_network_moves_nothing_of_the_one_it_joins(border_router):
    url, state = border_router
    restored = dataset("0123456789abcdef", channel=15)
    state["dataset"] = state["pending"] = restored  # a move of the network it restored, left pending
    service = serving(silabs_multiprotocol(), url, thread_dataset=dataset("dead00beef00cafe", channel=20))
    await asyncio.wait_for(service.start(), 3)

    assert "000300000f" in dict(state["puts"])[PENDING]  # the hub's network moved to Zigbee's channel all the same
    assert learned(service) == [(service, {"thread_dataset": state["dataset"]})]


async def test_a_channel_move_that_cant_land_fails_the_start(border_router, monkeypatch):
    url, state = border_router
    monkeypatch.setattr(services, "FORM_PATIENCE", 0.1)
    state["dataset"], state["refused"] = dataset("0123456789abcdef", channel=15), PENDING  # e.g. not attached
    service = serving(silabs_multiprotocol(), url, thread_dataset=dataset("dead00beef00cafe", channel=20))
    with pytest.raises(TimeoutError, match="channel 15"):
        await asyncio.wait_for(service.start(), 3)
    assert learned(service) == []


async def test_a_standalone_router_keeps_the_channel_of_the_network_it_joins(border_router):
    url, state = border_router
    state["dataset"] = dataset("0123456789abcdef", channel=15)
    service = serving(otbr(), url, thread_dataset=dataset("dead00beef00cafe", channel=20))
    await asyncio.wait_for(service.start(), 3)
    assert PENDING not in [path for path, _ in state["puts"]]


def test_a_move_is_stamped_after_the_network_it_moves_with_the_clock_behind(monkeypatch):
    monkeypatch.setattr(services.time, "time", lambda: 1000)  # a Pi without a clock of its own, before NTP
    stamped = (2_000_000_000 << 16).to_bytes(8).hex()  # by an earlier move, with the clock right
    moved = services._tlvs(services._moved(f"0e08{stamped}000300000f0208dead00beef00cafe", 20))
    assert int.from_bytes(moved[services._ACTIVE_TIMESTAMP]) >> 16 == 2_000_000_001  # newer: the devices take it
    assert moved[services._PENDING_TIMESTAMP] == moved[services._ACTIVE_TIMESTAMP]


async def test_the_channel_is_the_one_its_network_is_on_or_moving_to(border_router):
    url, state = border_router
    reading = asyncio.create_task(network_channel(url))
    await asyncio.sleep(0.05)
    assert not reading.done()  # no network yet

    state["dataset"] = dataset("dead00beef00cafe", channel=20)
    assert await asyncio.wait_for(reading, 3) == 20
    state["pending"] = dataset("dead00beef00cafe", channel=15)
    assert await network_channel(url) == 15


async def test_a_channel_move_is_tried_again_until_the_border_router_takes_it(border_router):
    url, state = border_router
    state["dataset"], state["refused"] = dataset("dead00beef00cafe", channel=20), PENDING  # not attached yet
    moving = asyncio.create_task(move_channel(url, 15))
    await asyncio.sleep(0.05)
    state["refused"] = None
    await asyncio.wait_for(moving, 3)
    assert [path for path, _ in state["puts"]].count(PENDING) > 1 and "000300000f" in state["dataset"]


async def test_learns_the_network_only_once_a_channel_move_landed(border_router):
    url, state = border_router
    moved = dataset("dead00beef00cafe", channel=15)
    state["dataset"], state["pending"] = dataset("dead00beef00cafe", channel=20), moved
    learning = asyncio.create_task(active_dataset(url))
    await asyncio.sleep(0.2)
    assert not learning.done()  # Matter would commission devices onto the channel it leaves

    state["dataset"], state["pending"] = moved, ""
    assert await asyncio.wait_for(learning, 3) == moved


async def test_a_move_landing_between_two_reads_is_learned_once_landed(border_router):
    url, state = border_router
    moved = dataset("dead00beef00cafe", channel=15)
    state["dataset"], state["pending"], state["landing"] = dataset("dead00beef00cafe", channel=20), moved, True
    assert await asyncio.wait_for(active_dataset(url), 3) == moved


class Answering:
    """Runs nothing: the border router at ``url`` answers already, and is set up as the runtime's containers are."""

    def __init__(self, url: str):
        self.url = url

    def provided(self, service: Service) -> list[Endpoint]:
        return [replace(endpoint, url=self.url) for endpoint in service.templates]

    async def up(self, service: Service):
        for endpoint in service.provides:
            await service.provision(endpoint)

    async def down(self, service: Service):
        pass

    def is_current(self, service: Service) -> bool:
        return True


def serving[S: Service](service: S, url: str, **facts: Any) -> S:
    """``service`` on the border router at ``url``, the platform holding ``facts``; what it tells recorded."""
    service.platform = build_test_platform(**facts)
    service.peripherals = Peripherals({"container": Endpoint("container", "compose", instance=Answering(url))})
    service.output = RecordingPeripheralOutput()
    return service


def learned(service: Service) -> list[tuple[Peripheral, Mapping[str, Any]]]:
    return cast(RecordingPeripheralOutput, service.output).learned


@pytest.mark.parametrize("router", [otbr, silabs_multiprotocol])
async def test_a_border_router_tells_its_thread_network_once_it_serves(border_router, router):
    url, state = border_router
    state["dataset"] = dataset("0123456789abcdef")
    service = serving(router(), url)
    await asyncio.wait_for(service.start(), 3)
    assert learned(service) == [(service, {"thread_dataset": dataset("0123456789abcdef")})]
