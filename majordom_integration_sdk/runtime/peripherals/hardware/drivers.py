"""The SDK's USB drivers, for radios: recognise which firmware a stick runs, say what each firmware serves, flash
between them."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from majordom_integration_sdk.peripherals import Endpoint, UsbDriver
from majordom_integration_sdk.runtime.peripherals.hardware.catalog import Build, Catalog

log = logging.getLogger(__name__)


class Silabs(UsbDriver):
    """Silicon Labs EFR32 sticks: Zigbee NCP (EZSP), Thread RCP (Spinel) or multi-PAN RCP (CPC). The builds a stick
    can be flashed with, and their serial settings, are its board's in the firmware ``catalog``."""

    name = "silabs"
    #: Serial settings of a stick the catalog has no build for (a ZBDongle-E on EZSP); its board's builds override them.
    firmwares = {
        "zigbee-ncp": [Endpoint("zigbee", "ezsp", "{device}", params={"baud": 115200, "flow_control": True})],
        "thread-rcp": [Endpoint("thread", "spinel", "{device}", params={"baud": 460800, "flow_control": True})],
        "multipan-rcp": [Endpoint("multipan", "silabs-cpc", "{device}", params={"baud": 460800, "flow_control": True})],
    }

    def __init__(self, catalog: Catalog | None = None):
        self.catalog = catalog or Catalog.default()
        self._is_flasher_missing_reported = False

    # NabuCasa's multi-PAN RCP is 802.15.4 only: none of these builds serves Bluetooth (``bluetooth`` stays empty)
    _apps = {"EZSP": "zigbee-ncp", "SPINEL": "thread-rcp", "CPC": "multipan-rcp"}
    #: What it recognises, our multi-PAN build first: the flasher's own order tries EZSP first, ~25 s on a CPC stick,
    #: and bellows can crash there on a stick a container only just let go of.
    _probes = "cpc:460800,cpc:115200,cpc:230400,ezsp:115200,ezsp:460800,spinel:460800"

    async def identify(self, path: str) -> str | None:
        if not shutil.which(FLASHER):
            if not self._is_flasher_missing_reported:
                log.error(f"{FLASHER} isn't installed: Silicon Labs sticks stay unrecognised; install the silabs extra")
                self._is_flasher_missing_reported = True
            return None
        try:
            output = await _flasher(path, "--probe-methods", self._probes, "probe")
        except RuntimeError:  # once more: a stick just let go of may still be resetting
            await asyncio.sleep(PROBE_RETRY_DELAY)
            output = await _flasher(path, "--probe-methods", self._probes, "probe")
        found = re.search(r"Detected ApplicationType\.(\w+)", output)
        return self._apps.get(found[1]) if found else None

    def serves(self, firmware: str | None, path: str, params: Mapping[str, Any] | None = None) -> list[Endpoint]:
        build = self._builds(path, "dev").get(firmware or "")
        return super().serves(firmware, path, {**(build.params if build else {}), **(params or {})})

    def flashable(self, path: str, channel: str) -> list[str]:
        return list(self._builds(path, channel))

    async def flash(self, path: str, firmware: str, channel: str, cache: Path):
        if not (build := self._builds(path, channel).get(firmware)):
            raise ValueError(f"No {firmware} firmware for {path} on the {channel} channel")
        file = cache / build.url.rsplit("/", 1)[1]
        if not file.exists():
            cache.mkdir(parents=True, exist_ok=True)
            data = await asyncio.to_thread(lambda: urllib.request.urlopen(build.url, timeout=60).read())
            if hashlib.sha256(data).hexdigest() != build.sha256:
                raise ValueError(f"Checksum mismatch for {build.url}")
            file.write_bytes(data)
        await _flasher(path, "flash", "--firmware", str(file))

    def _builds(self, path: str, channel: str) -> dict[str, Build]:
        """The builds for the stick at ``path`` this driver speaks the protocol of, by firmware."""
        board, protocols = self.catalog.usb_board(path), self.protocols
        builds = self.catalog.builds(board, channel).values() if board else []
        return {protocols[b.protocol]: b for b in builds if b.protocol in protocols}


class ZWave(UsbDriver):
    """Z-Wave controllers speaking the Serial API (any vendor)."""

    name = "zwave"
    firmwares = {
        "zwave-serial-api": [
            Endpoint("zwave", "zwave-serial-api", "{device}", params={"baud": 115200, "flow_control": False})
        ]
    }

    async def identify(self, path: str) -> str | None:
        return "zwave-serial-api" if await asyncio.to_thread(_zwave_answers, path) else None


def _zwave_answers(path: str) -> bool:
    import serial  # the runtime extra

    # Serial API request frame for FUNC_ID_ZW_GET_VERSION: SOF, length, REQ, 0x15, checksum.
    # A controller replies with its library version string, "Z-Wave x.y", and repeats any reply
    # the host didn't ACK (answering CAN to new requests meanwhile): NAK first, ACK the answer.
    with serial.Serial(path, 115200, timeout=1) as port:
        port.write(b"\x15")
        for _ in range(3):
            port.reset_input_buffer()
            port.write(bytes([0x01, 0x03, 0x00, 0x15, 0xE9]))
            if b"Z-Wave" in port.read(64):
                port.write(b"\x06")
                return True
    return False


FLASHER = "universal-silabs-flasher"
PROBE_RETRY_DELAY = 3


async def _flasher(path: str, *args: str) -> str:
    """Run universal-silabs-flasher (GPL-3.0, so a separate process; the `silabs` extra installs it)."""
    process = await asyncio.create_subprocess_exec(
        FLASHER, "--device", path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    output = (await process.communicate())[0].decode(errors="replace")
    if process.returncode:
        raise RuntimeError(f"{FLASHER} {" ".join(args)} failed on {path}:\n{output[-2000:]}")
    return output


def default_usb_drivers(catalog: Catalog | None = None) -> list[UsbDriver]:
    """Every driver of the SDK, tried in this order on every unidentified stick: the cheap single-frame Z-Wave check
    first."""
    return [ZWave(), Silabs(catalog)]
