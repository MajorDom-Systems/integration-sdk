# Multi-PAN host stack, SiSDK 2025.6.3

One Silicon Labs multi-PAN RCP serves Zigbee and Thread at the same time:

```
dbus, mdnsd -> cpcd -> zigbeed (CPC iid 1) -> EZSP pty -> bellows form -> EZSP over TCP :9999
                    -> otbr-agent (CPC iid 2) -> wpan0, REST :8081
```

It replaces `../multiprotocol/` (gsdk-4.4.6, CPC protocol v5) for radios whose RCP is built on SiSDK 2025.6.3 (CPC protocol v6), such as the MG26 on the CM4 board. Host programs and RCP firmware must come from the same SDK release.

## Contents

| Component | Version | Source | License |
|---|---|---|---|
| cpcd, libcpc | 4.7.1.0 (the deb is labelled 4.6.0) | SiSDK v2025.6.3 `debian-bookworm.zip` | Silicon Labs MSLA |
| zigbeed | 8.2.2 (EmberZNet 8.2.2.0, EZSP v18) | same | MSLA |
| otbr-agent, ot-ctl | ot-br-posix 2.7.3.0 | same | MSLA + BSD-3-Clause (OpenThread) |
| mdnsd, libdns_sd | built from the source in the ot-br-posix deb | same | Apache-2.0 / BSD-3-Clause |
| bellows, zigpy, aiosqlite | 0.49.2, 2.0.1, 0.21.0 (the Hub's lock) | PyPI | GPL-3.0 (bellows, zigpy), MIT |
| Base | `debian:bookworm-slim`, python 3.11 | Debian | various |

The release zip is pinned by sha256 (`SISDK_DEBS_SHA256` in the Dockerfile). Only the target architecture's debs are unpacked. Their maintainer scripts are never run: they would pull in bind9, nodejs, radvd and rsyslog. otbr-web is not included.

| Image | Size | Compressed | Local image ID |
|---|---|---|---|
| linux/arm64 | 210 MB | 62 MB | `sha256:880788dd961bfdf0ca6560f3e5216ff4e20f884157c0a024ebec6de9621f06ca` |
| linux/amd64 | 176 MB | 62 MB | `sha256:b6d3fc7e7e8b4cc1b6a166ea94db315ec623cb0aedb1c424fa4187a3576bedb6` |

The gsdk-4.4.6 image is 1.98 GB. The registry digest only exists after a push: pin the multi-arch index digest, not these IDs.

## Build

```sh
docker buildx build --platform linux/arm64 -t majordom/multiprotocol:sisdk-2025.6.3-arm64 --load .
docker buildx build --platform linux/amd64 -t majordom/multiprotocol:sisdk-2025.6.3-amd64 --load .
```

On a Pi 5, the arm64 build takes about 1.5 min and the amd64 build about 4–8 min under QEMU. One multi-arch index needs a `docker-container` buildx builder and `--push`. The build fails if any copied binary is missing a shared library.

## Run

```sh
docker run -d --name multiprotocol --privileged --network host \
  -v /dev/serial:/dev/serial:ro -v otbr-data:/var/lib/thread -v zigbee-data:/var/lib/zigbee \
  -e RADIO_DEVICE=/dev/serial/by-id/<stick> majordom/multiprotocol:sisdk-2025.6.3-arm64
```

`entrypoint.sh` and `cpcd.conf.tmpl` are part of the image, so nothing needs to be mounted. The entrypoint renders `/etc/cpcd.conf` from the environment; a mounted `/etc/cpcd.conf` is used as is. The other environment variables are listed at the top of `entrypoint.sh`. For the MG26 on the CM4's bare UART:

```
RADIO_DEVICE=/dev/ttyAMA3 RADIO_BAUD=115200 RADIO_HARDFLOW=false RADIO_RESET_GPIO=gpiochip0:23
```

`RADIO_RESET_GPIO` pulses RESETn about 1.5 s after cpcd starts, as `majordom-cpcd.sh` does on the host, and holds the line high while the container runs. Once it exits, the line is released. The CM4's GPIO23 has its pull-down enabled, so give RESETn a pull-up on the PCB.

Changes from the gsdk-4.4.6 entrypoint:
- cpcd's readiness line is now "Connected to Secondary".
- bellows 0.46 does not know EZSP v18. Its form crashed, but the old success check still matched the aiosqlite `.join` traceback and wrote the "formed" marker. A form now counts only when it exits 0.

## Validated

The arm64 image was validated on 2026-10-14 on the CM4 against the MG26, which runs the SiSDK 2025.6.3 multi-PAN RCP. The CM4 has no Docker, so the exported image rootfs ran in private mount, net and pid namespaces with a dummy `eth0`:
- cpcd reported `Secondary Protocol v6` and `Secondary CPC v4.7.1`.
- `ot-ctl state` returned `leader`, and REST `/node/state` returned `"leader"`.
- `bellows info` over `socket://127.0.0.1:9999` returned `JOINED_NETWORK`, `COORDINATOR` and `EmberZNet version: 8.2.2.0 build 532`.
- After a restart, the stack restored both networks (same Zigbee PAN) instead of forming new ones.

## Licensing

- **Silicon Labs binaries.** The image carries cpcd, zigbeed and otbr-agent unmodified, as object code under the MSLA (`/usr/share/doc/silabs/MSLA`). The MSLA permits distribution to end users only for use with Silicon Labs devices. Ask a Silicon Labs FAE before publishing the image publicly (MSLA §4.2.5).
- **GPL components.** bellows and zigpy are GPL-3.0. They run as separate programs, which is plain aggregation. Their licence texts are in their `dist-info` directories, and the source is unmodified PyPI releases.
- **Notices.** The OpenThread BSD-3 text and the component list are in `/usr/share/doc/majordom-multiprotocol/`.
