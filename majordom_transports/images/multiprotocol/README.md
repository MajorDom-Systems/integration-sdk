# Multiprotocol radio harness (concurrent Thread + Zigbee on one stick)

Runs **cpcd + zigbeed + OTBR + matter-server** so a single Silicon Labs **multi-PAN RCP** serves
Zigbee and Thread (Matter) at the same time — no reflash to switch protocols. Proven end-to-end on
lab-pi5: the Matter cage test commissions and controls an IKEA bulb over Thread while a Zigbee
coordinator runs concurrently on the same radio.

```
 EFR32 multi-PAN RCP (USB)                     Zigbee integration ── socket://127.0.0.1:9999
        │ UART @460800                              (bellows, MAJORDOM_ZIGBEE_RADIO=ezsp)
   ┌────┴─────┐  CPC multiplexing
   │  cpcd    │  iid=1 ─ zigbeed ─ EZSP pty ─ socat ─ tcp:9999
   │ (cpcd_0) │  iid=2 ─ otbr-agent ─ Thread ─ REST :8081 ──── matter-server (ws :5580)
   └──────────┘                                              (BLE commissioning via host BlueZ)
```

## Layout
| File | Purpose |
|---|---|
| `docker-compose.yml` | the `silabs-multiprotocol` + `matter-server` services |
| `Dockerfile` | thin derived image over the SiLabs base — bakes in the Zigbee forming tool (bellows) |
| `entrypoint.sh` | brings the daemons up in order (dbus → cpcd → zigbeed → **form Zigbee on the pty** → otbr), idempotent + supervised |
| `cpcd.conf` | cpcd config (device, 460800 baud) |
| `flash-rcp.sh` | idempotently flashes the stick to the multi-PAN RCP firmware |
| `../majordom-multiprotocol.service` | systemd unit: flash-then-`compose up`, ordered `Before=majordom-hub` |

`system/setup.sh` installs Docker, enables the service, and appends the endpoints to `.env`.

## Multiprotocol options (evaluated) — and what this uses
1. **RCP host-stack multiprotocol** — dumb multi-PAN RCP on the stick; cpcd + zigbeed + otbr run on
   the host. **← what this harness uses.** Works on the existing EFR32MG21 SkyConnect.
2. **HA "Silicon Labs Multiprotocol" add-on** — the same stack, packaged for the HA Supervisor.
   Battle-tested but supervisor-coupled and **deprecated by Nabu Casa (2025)**; image is gated.
3. **DMP (Dynamic Multiprotocol)** — on-SoC time-slicing; used for BLE + 802.15.4, not for
   Zigbee+Thread-on-a-gateway.
4. **Two radios** — one Zigbee coordinator + one Thread RCP. Rock-solid, what Nabu Casa now
   recommends, but two sticks.
5. **Newer silicon** — EFR32MG24/MGM240 (more RAM, better CMP) or Series-3 SiMG301 (native CMP).
   The right hardware move if multiprotocol becomes load-bearing.

This harness = option 1, using a thin derived image (`Dockerfile`) over the version-matched
`siliconlabsinc/multiprotocol:gsdk-4.4.6-arm64` base — the only addition is the baked-in Zigbee
forming tool — against a `gsdk 4.4.4` multi-PAN RCP.

### Zigbee coordinator forming (why it happens in the container)
EZSP `form` is a latency-sensitive radio-level sequence and **cannot traverse the EZSP→TCP bridge**
(zigbeed→pty→socat→TCP loses ASH frames → `networkInit` timeout / `ERR_FLASH_VERIFY_FAILED`), even
though version negotiation and steady-state load/commands work fine over it. So `entrypoint.sh`
forms the coordinator **in-container, on the pty, as the single reader, before exposing TCP** (step
3a), when the NCP reports `NOT_JOINED`. It's idempotent — the coordinator persists in NCP flash, so
restarts skip it — and the Hub then just *loads* it over `socket://…:9999`. `bellows` (0.46.2 +
zigpy 0.84.0 + aiosqlite 0.22.1, the rig-proven combo) is baked into the image so first boot forms
offline with no runtime `pip`.

## Review of the current bare-metal setup, and improvements
`setup.sh` today installs the Hub bare-metal (apt + poetry + networking + a systemd unit) and
assumes a single directly-attached coordinator. Observations / improvements:
- **Radio is now containerized.** Do **not** run any bare-metal zigbee/thread stack against the
  stick as well — cpcd owns it. The Hub talks TCP/WS/REST only.
- **`>> /boot/config.txt` is not idempotent** (re-running setup appends `dtparam=spi=on` again).
  Guard every append with `grep -q … || …` (done for `.env` here; apply the same to config.txt).
- **`apt install … dhcpd` / `rustc`**: `dhcpd` isn't a Debian package name (likely `isc-dhcp-server` /
  `kea`), and building `rustc` from apt is heavy — pin what's actually needed.
- **Address the stick by its `by-id` path** everywhere (ttyUSBn reshuffles across reboots — it did
  during this work). `docker-compose.yml`/`flash-rcp.sh` already do.
- **Kernel:** the arm64 daemon image runs on the stock 16 KB-page Pi 5 kernel — keep it. Only the
  legacy 32-bit image needs the 4 KB `kernel8.img`; don't ship that.
- **Version match is load-bearing:** cpcd/zigbeed GSDK generation must match the RCP firmware, or
  network ops hang while `info` still works. Pin the image tag and the GBL together.

## Sourcing policy — first-party Silicon Labs, minimal NabuCasa
Keep NabuCasa out of the shipped/core path; use it only as a lab reference.
- **Daemons:** `siliconlabsinc/multiprotocol` is Silicon Labs' *own* image (cpcd + zigbeed + ot-cli)
  — first-party, fine to ship. The fully-direct alternative is to build the three daemons yourself:
  `github.com/SiliconLabs/cpc-daemon` (cpcd — builds native arm64), `zigbeed` from the Silicon Labs
  GSDK, and `openthread/ot-br-posix` (otbr). No NabuCasa in any of these.
- **RCP firmware:** build the `rcp-uart-802154` sample from the **Silicon Labs GSDK** (slc-cli +
  GCC-ARM) in CI and publish the `.gbl` as a build artifact — the first-party source of truth.
  `flash-rcp.sh` expects that at `RCP_GBL`. The archived NabuCasa GBL is fetched **only** with
  `ALLOW_NABUCASA=1` (lab convenience, not shipped).
- **Flasher:** prefer **Simplicity Commander** (`commander`). `universal-silabs-flasher` (NabuCasa)
  is a fallback gated behind `ALLOW_NABUCASA=1`.

## Baking it into the OS image (build/) — complete image, minimal first boot
The image is built with **CustoPiZer** (`build/build.sh` → `build/scripts/run_setup.sh`, which runs
`system/setup.sh` then `build/scripts/preload-multiprotocol.sh`). Principle: **bake everything at
build time; first boot only starts things.** Faster manufacturing, more test coverage, predictable,
short first-boot.

1. **Build arm64.** `build/download_image.sh` defaults to `raspios_lite_arm64` (required for the
   arm64 daemon container + Pi 5); aligned with `config_64.local` (`EDITBASE_ARCH=arm64`).
2. **Docker is installed at build time** (`setup.sh`, chroot-tolerant).
3. **Container images are baked in at build time.** Stage them on the build host, then
   `preload-multiprotocol.sh` `docker load`s them into the image's `/var/lib/docker` (via a
   throwaway dockerd in the privileged build container):
   ```sh
   # build host, before CustoPiZer:
   mkdir -p build/files/images build/files/firmware
   # Thin derived image = siliconlabsinc base + baked-in Zigbee forming tool (see Dockerfile). Build
   # it for arm64 so first boot forms the Zigbee coordinator offline, no runtime pip.
   docker buildx build --platform linux/arm64 --load \
     -t majordom/silabs-multiprotocol:gsdk-4.4.6-arm64 system/multiprotocol
   docker pull --platform linux/arm64 ghcr.io/matter-js/matterjs-server:dev
   docker save majordom/silabs-multiprotocol:gsdk-4.4.6-arm64 -o build/files/images/silabs-mp.tar
   docker save ghcr.io/matter-js/matterjs-server:dev          -o build/files/images/matterjs.tar
   cp <gsdk-built>/rcp-uart-802154.gbl build/files/firmware/     # first-party firmware
   ```
   First boot then runs `docker compose up --pull never` against **already-present** images — no
   pull, no network needed.
4. **Firmware:** the `.gbl` is staged at `/opt/majordom/firmware` at build time; **pre-flash the
   sticks at a manufacturing station**, so first boot's `flash-rcp.sh` is just a VERIFY (idempotent
   no-op on a correct stick). The stick's firmware can't live in the SD image — it's separate silicon.
5. **Pin by digest** in `docker-compose.yml` (`image: …@sha256:…`) for reproducible field images.
6. **Kernel:** RaspiOS arm64 for the Pi 5 ships the stock 16 KB-page kernel and the arm64 container
   runs on it — do **not** put `kernel=kernel8.img` in the image.
7. **Root size:** `config_64.local` enlarges root; leave ~2 GB headroom for the baked images.

What legitimately can't be baked: the per-device stick firmware (separate hardware → manufacturing
station) and per-device secrets/keys. Everything else ships in the image.

## Continuous integration
The lab's `iot` runner is a **Nomad-scheduled, dockerless, non-root, ephemeral** GitHub-App runner
(home-lab `apps/github-runner`) with USB/serial passthrough only — it does **not** run Docker and is
already well de-rooted (no `--privileged`, no root, no docker socket). So the harness is **not** run by
the workflow; it runs as a **node service on the CI node** (this compose via systemd, or a Nomad job
alongside the runner). `.github/workflows/test_hardware.yaml` only **verifies the endpoints are up**
(`HARNESS_HOST`, default 127.0.0.1) and runs both hardware suites against them — Zigbee via
`socket://…:9999`, Matter via `matter-server` (:5580) + OTBR REST (:8081). To deploy on the CI node:
enable `majordom-multiprotocol.service`, or add a Nomad job (docker driver, `privileged`,
`network_mode=host`, device passthrough). The runner must be able to reach the harness ports (share the
host net namespace, or target the node IP via `HARNESS_HOST`). BlueZ cache flush before a Matter run is
a node/matter-server responsibility (the dockerless runner can't restart host bluetoothd).

## Production-proof recommendation (hardware/runtime)
For a shipped product, prefer **robustness over cleverness**:
1. **Two dedicated radios** (option 4) if BOM allows, or **move to MG24/MGM240** (option 5). The
   MG21 is below Silicon Labs' recommended RAM for multi-PAN and the whole host stack is upstream-
   deprecated — fine for R&D, risky as the only path in a product.
2. If shipping multi-PAN on one stick: **pin everything and make it reproducible** — pin the image
   digest and the RCP GBL, run `flash-rcp.sh` as a first-boot oneshot, health-check the container
   (compose already does), and let `systemd`/compose `restart: unless-stopped` recover crashes.
3. **Enable CPC security** (build/flash an RCP with security on, provision a binding key) instead of
   `disable_encryption: true`.
4. **Bake it into the image build** (`build/`) rather than first-boot `docker pull` — pre-pull the
   pinned images and stage the firmware at `/opt/majordom/firmware` so a field unit needs no network
   to come up.
5. **Observability:** ship cpcd/zigbeed/otbr logs to the journal and alert on the healthcheck; the
   #1 field failure mode is a wedged RCP (`cp210x … -110`) — automate the USB bus-reset recovery.
