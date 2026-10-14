# majordom-transports

Radios and the services between them and MajorDom integrations.

Integrations only say what they connect to (`Need("zigbee")`, `Need("matter", ["ws"])`). This package
finds the USB sticks and soldered radios, recognises their firmware, flashes them between modes,
binds every need to whatever radio, service or host capability can serve it, and runs the services
that end up bound through Docker Compose.

```
USB stick (multi-PAN RCP) ── cpc/uart ──▶ multiprotocol ── zigbee/ezsp ─────────▶ Zigbee
                                                     └──── thread/otbr-rest ──▶ matter-server ── matter/ws ──▶ Matter
USB stick (Thread RCP)    ── thread/spinel ──▶ otbr ── thread/otbr-rest ──▶ (same)
TI / ConBee / XBee stick  ── recognised by the Zigbee integration's probe ──▶ Zigbee
Z-Wave stick              ── zwave/serial ──▶ zwave-js (from the Z-Wave integration) ──▶ Z-Wave
```

## Use

```python
from majordom_transports import Runtime

runtime = Runtime(data_dir, {"zigbee": ZigBeeController, "matter": MatterController})
await runtime.start()  # finds sticks, binds, runs services, watches for changes
runtime.links("zigbee")  # {"zigbee": Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")}
await runtime.set_firmware(radio_id, "multipan")
await runtime.update_settings("zigbee", {"tx_power": -20})
```

Any layer can be yours instead:

| You want | Pass |
|---|---|
| everything automatic | `Runtime(data, consumers)` |
| your own border router | `pinned={"thread": Endpoint("thread", "otbr-rest", "http://my-otbr:8081", shared=True)}` |
| a specific device, no scanning | `scan=False, devices={"stick": "/dev/ttyUSB0"}` |
| to run containers yourself | `docker=False` — the would-be `compose.json` is still written |
| nothing of this | skip the package; hand the controller `Dependencies(links={...})` |

### Servers you already run

A matter-server or zwave-js-server of your own is a service you define by hand: an `Endpoint` with a fixed URL, pinned by family. It is offered as written, ahead of anything the runtime finds, and no container starts for a need it serves:

```python
from majordom_integration_sdk import Endpoint
from majordom_transports import Runtime

runtime = Runtime(
    data_dir,
    {"matter": MatterController, "zwave": ZwaveController},
    pinned={
        "matter": Endpoint("matter", "ws", "ws://my-matter:5580/ws"),  # your matter-server
        "zwave": Endpoint("zwave", "ws", "ws://my-zwave:3000"),  # your zwave-js-server
    },
    scan=False,  # the sticks live behind those servers
    docker=False,  # nothing left to run
)
await runtime.start()
runtime.links("matter")  # {"matter": Endpoint("matter", "ws", "ws://my-matter:5580/ws")}
```

Pin only `thread` (`Endpoint("thread", "otbr-rest", "http://my-otbr:8081", shared=True)`) to keep matter-server running here, but behind your border router.

Service templates can use `{device}`, `{data}`, `{links.<family>.url}`, `{links.<family>.params[baud]}`,
`{settings.<field>}`, `{context.region}`, `{host.interface}`, `{host.uid}`, `{host.gid}` and `{ports.<name>}`.
A service names its host ports with the one it prefers (`ports={"api": 5580}`); the runtime picks a free one,
keeps it in `state.json`, and fills `{ports.<name>}` in the compose and the `provides` URLs.

Soldered radios come from a board profile: `board=[{"id": "mg26", "path": "/dev/ttyAMA3", "driver": "silabs", "firmware": "multipan"}]`.
Add `"params": {"baud": 115200, "flow_control": false}` where the board wires the UART unlike the firmware's default.

A stick another program has open (zigbee2mqtt, Home Assistant) is not opened or bound; `radios()` shows it
`held_by` that program. Probing it, or pinning a family to it, claims it anyway.

## Firmware

The hub setting `firmware` (`"stable"`, the default, or `"dev"`) picks what radios can be flashed to:
verified builds only, or every known build. Flashing uses the
`universal-silabs-flasher` command, which must be on `PATH`.

## Develop

```
poetry install && poetry run pytest
```
