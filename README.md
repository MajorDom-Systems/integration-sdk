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

Soldered radios come from a board profile: `board=[{"id": "mg26", "path": "/dev/ttyAMA3", "driver": "silabs", "firmware": "multipan"}]`.

## Firmware

`channel="stable"` offers verified builds only; `"dev"` adds every known build. Flashing uses the
`universal-silabs-flasher` command, which must be on `PATH`.

## Develop

```
poetry install && poetry run pytest
```
