"""The SDK's USB drivers, for radios: recognise which firmware a stick runs, say what each firmware serves, flash
between them."""

from __future__ import annotations

import asyncio
import hashlib
import re
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from majordom_integration_sdk.peripherals import Endpoint, UsbDriver
from majordom_integration_sdk.runtime.peripherals.hardware.catalog import Build, Catalog


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

    # NabuCasa's multi-PAN RCP is 802.15.4 only: none of these builds serves Bluetooth (``bluetooth`` stays empty)
    _apps = {"EZSP": "zigbee-ncp", "SPINEL": "thread-rcp", "CPC": "multipan-rcp"}

    async def identify(self, path: str) -> str | None:
        found = re.search(r"Detected ApplicationType\.(\w+)", await _flasher(path, "probe"))
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
        board = self.catalog.usb_board(path)
        return self.catalog.builds(board, channel) if board else {}


class ZWave(UsbDriver):
    """Z-Wave controllers speaking the Serial API (any vendor)."""

    name = "zwave"
    firmwares = {
        "serial-api": [Endpoint("zwave", "serial-api", "{device}", params={"baud": 115200, "flow_control": False})]
    }

    async def identify(self, path: str) -> str | None:
        return "serial-api" if await asyncio.to_thread(_zwave_answers, path) else None


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


async def _flasher(path: str, *args: str) -> str:
    """Run universal-silabs-flasher (an external tool, kept out of this package's dependencies)."""
    process = await asyncio.create_subprocess_exec(
        "universal-silabs-flasher", "--device", path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    output = (await process.communicate())[0].decode(errors="replace")
    if process.returncode:
        raise RuntimeError(f"universal-silabs-flasher {args[0]} failed on {path}:\n{output[-2000:]}")
    return output


#: USB ids of radios opened unasked to identify them, besides the catalog's boards (`radio_usb_ids`): sticks the
#: firmware repo doesn't describe yet. Generic bridges common on other boards stay out (CH340 1a86:7523 on Arduinos,
#: Espressif 303a on ESP32s): opening their port resets the board. A device that isn't listed is identified only when
#: asked (`Radio.probe`).
THIRD_PARTY_RADIO_USB_IDS = {
    ("10c4", "ea60"),  # CP210x: Sonoff Z-Wave PZG23, Zooz ZST10 (SkyConnect / ZBT-1 and ZBDongle-P are catalog boards)
    ("10c4", "8a2a"),  # Nortek HUSBZB-1
    ("1a86", "55d4"),  # CH9102: Sonoff ZBDongle-E, SMLIGHT SLZB-07
    ("0658", "0200"),  # Sigma Designs: Aeotec Z-Stick, HomeSeer SmartStick
    ("1cf1", "0030"),  # dresden elektronik ConBee II
    ("0403", "6015"),  # FTDI X: ConBee III, ZiGate+
    ("0451", "16a8"),  # TI CC2531
}


def radio_usb_ids(catalog: Catalog) -> set[tuple[str, str]]:
    return catalog.usb_ids() | THIRD_PARTY_RADIO_USB_IDS


def default_usb_drivers(catalog: Catalog | None = None) -> list[UsbDriver]:
    """Every driver of the SDK, tried in this order on every unidentified stick: the cheap single-frame Z-Wave check
    first."""
    return [ZWave(), Silabs(catalog)]
