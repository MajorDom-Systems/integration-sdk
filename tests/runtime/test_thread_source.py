import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from majordom_integration_sdk.runtime.peripherals.services import builtin as services

SCRIPT = services._IMAGES / "thread-source" / "run"

# A stand-in for iproute2: answers the queries the script makes from a JSON state and applies its route changes.
FAKE_IP = f"""#!{sys.executable}
import ipaddress, json, sys
from pathlib import Path

state_file = Path(__file__).with_name("state.json")
state = json.loads(state_file.read_text())
args = sys.argv[1:]
if args[:5] == ["-o", "-6", "addr", "show", "dev"]:
    if args[5] not in state["addr"]:
        sys.exit(f'Device "{{args[5]}}" does not exist.')
    print("\\n".join(state["addr"][args[5]]))
elif args[:4] == ["-6", "route", "show", "match"]:
    address = ipaddress.ip_address(args[4])
    for dst, line in state["routes"].get(args[6], {{}}).items():
        if dst != "default" and address in ipaddress.ip_network(dst):
            print(f"{{dst}} {{line}}")
elif args[:3] == ["-6", "route", "replace"]:
    state["replaced"].append(args[3:])
    state["routes"][args[5]][args[3]] = " ".join(args[6:])
    state_file.write_text(json.dumps(state))
else:
    sys.exit(f"unexpected: ip {{' '.join(args)}}")
"""

ETH0 = [
    "2: eth0    inet6 fdc0:ca84:d7e3:0:ce47:229:37f5:dc98/64 scope global temporary dynamic",
    "2: eth0    inet6 2a0a:a548:cced:0:8aa2:9eff:fea7:d66d/64 scope global dynamic mngtmpaddr proto kernel_ra",
    "2: eth0    inet6 fdc0:ca84:d7e3:0:8aa2:9eff:fea7:d66d/64 scope global dynamic mngtmpaddr proto kernel_ra",
]
WPAN0 = [
    "7: wpan0    inet6 fd4c:60e5:af3f:4111:0:ff:fe00:fc11/64 scope global nodad deprecated",
    "7: wpan0    inet6 fd11:ec6a:d233:1:5465:34b9:c5b4:2b7c/64 scope global nodad",
    "7: wpan0    inet6 fd4c:60e5:af3f:4111:f746:74ed:1eb7:e84/64 scope global nodad deprecated",
]
OMR, MESH_LOCAL = "fd11:ec6a:d233:1::/64", "fd4c:60e5:af3f:4111::/64"
STABLE_ULA = "fdc0:ca84:d7e3:0:8aa2:9eff:fea7:d66d"
KERNEL_ROUTE = "proto kernel metric 256 pref medium"


@pytest.fixture
def host(tmp_path: Path):
    """The border router's host as `ip` shows it: Thread routes as the kernel adds them for wpan0's addresses."""
    (tmp_path / "ip").write_text(FAKE_IP)
    (tmp_path / "ip").chmod(0o755)
    state = {
        "addr": {"eth0": ETH0, "wpan0": WPAN0},
        "routes": {"wpan0": {OMR: KERNEL_ROUTE, MESH_LOCAL: KERNEL_ROUTE}},
        "replaced": [],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    return tmp_path


def sync(host: Path) -> dict:
    env = {"PATH": f"{host}{os.pathsep}{os.environ['PATH']}", "OT_THREAD_IF": "wpan0", "BACKBONE_IF": "eth0"}
    subprocess.run(["bash", "-c", f'source "{SCRIPT}" && sync_sources'], env=env, check=True, timeout=10)
    return json.loads((host / "state.json").read_text())


def edit(host: Path, change):
    state = json.loads((host / "state.json").read_text())
    change(state)
    (host / "state.json").write_text(json.dumps(state))


def test_routes_to_the_border_routers_own_thread_addresses_take_a_stable_backbone_ula(host):
    state = sync(host)
    assert state["replaced"] == [[OMR, "dev", "wpan0", *KERNEL_ROUTE.split(), "src", STABLE_ULA]]
    assert MESH_LOCAL not in {r[0] for r in state["replaced"]}  # only deprecated addresses of ours there


def test_a_route_with_the_source_already_is_left_alone(host):
    sync(host)
    assert len(sync(host)["replaced"]) == 1


def test_follows_the_border_router_re_adding_its_route(host):
    sync(host)
    edit(host, lambda s: s["routes"]["wpan0"].update({OMR: KERNEL_ROUTE}))
    assert len(sync(host)["replaced"]) == 2


def test_follows_the_backbone_renumbering(host):
    sync(host)
    renumbered = "fdc0:ca84:d7e3:0:1111:2222:3333:4444"
    edit(host, lambda s: s["addr"].update(eth0=[ETH0[2].replace(STABLE_ULA, renumbered)]))
    assert sync(host)["replaced"][-1][-2:] == ["src", renumbered]


def test_a_temporary_ula_when_there_is_no_stable_one(host):
    edit(host, lambda s: s["addr"].update(eth0=ETH0[:2]))
    assert sync(host)["replaced"][0][-1] == "fdc0:ca84:d7e3:0:ce47:229:37f5:dc98"


def test_without_a_backbone_ula_nothing_changes(host):
    edit(host, lambda s: s["addr"].update(eth0=[ETH0[1]]))  # a GUA: nodes may have no route back to it
    assert sync(host)["replaced"] == []


def test_without_a_thread_interface_nothing_changes(host):
    edit(host, lambda s: s["addr"].pop("wpan0"))
    assert sync(host)["replaced"] == []


def test_both_border_routers_run_it():
    assert f"{SCRIPT.parent}:/etc/services.d/thread-source:ro" in services.otbr().compose["volumes"]  # s6 starts it
    assert f"{SCRIPT}:/thread-source:ro" in services.silabs_multiprotocol().compose["volumes"]
    assert "bash /thread-source &" in (services._IMAGES / "multiprotocol" / "entrypoint.sh").read_text()
