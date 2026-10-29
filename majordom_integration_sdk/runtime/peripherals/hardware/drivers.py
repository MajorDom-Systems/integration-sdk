"""The SDK's USB drivers, for radios: recognise which firmware a stick runs, say what each firmware serves, flash
between them."""

from __future__ import annotations

import asyncio
import hashlib
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from majordom_integration_sdk.peripherals import Endpoint, UsbDriver


@dataclass(frozen=True)
class Firmware:
    url: str
    sha256: str
    is_stable: bool = False  # verified for shipping; the rest is offered on the dev channel only


_NABUCASA = "https://github.com/NabuCasa/silabs-firmware-builder/releases/download"


class Silabs(UsbDriver):
    """Silicon Labs EFR32 sticks: Zigbee NCP (EZSP), Thread RCP (Spinel) or multi-PAN RCP (CPC)."""

    name = "silabs"
    firmwares = {
        "zigbee-ncp": [Endpoint("zigbee", "ezsp", "{device}", params={"baud": 115200, "flow_control": True})],
        "thread-rcp": [Endpoint("thread", "spinel", "{device}", params={"baud": 460800, "flow_control": True})],
        "multipan-rcp": [Endpoint("multipan", "silabs-cpc", "{device}", params={"baud": 460800, "flow_control": True})],
    }

    #: Builds per board, keyed by the product name in the stick's /dev/serial/by-id path.
    boards: ClassVar[dict[str, dict[str, Firmware]]] = {
        "SkyConnect": {
            "zigbee-ncp": Firmware(
                f"{_NABUCASA}/v2025.11.24/skyconnect_zigbee_ncp_7.4.4.5.gbl",
                "9a189c5797ee4ac975c0a5ebdd96b126332ac348cab27d065bd58e993e7bb7b0",
            ),
            "thread-rcp": Firmware(
                f"{_NABUCASA}/v2025.11.24/skyconnect_openthread_rcp_2.4.4.0_GitHub-7074a43e4_gsdk_4.4.4.gbl",
                "4cb5d04f4fab827745665104110fe1d272e4eb9bc38d1f496abb0311ef76e062",
            ),
            "multipan-rcp": Firmware(
                f"{_NABUCASA}/v2024.8.20/skyconnect_rcp-uart-802154_gsdk_4.4.4.gbl",
                "2140cfb06d2957e610057888a80fa068cf1bdacdbadb51ba430e2c8e1907a8bb",
            ),
        },
    }

    # NabuCasa's multi-PAN RCP is 802.15.4 only: none of these builds serves Bluetooth (``bluetooth`` stays empty)
    _apps = {"EZSP": "zigbee-ncp", "SPINEL": "thread-rcp", "CPC": "multipan-rcp"}

    async def identify(self, path: str) -> str | None:
        found = re.search(r"Detected ApplicationType\.(\w+)", await _flasher(path, "probe"))
        return self._apps.get(found[1]) if found else None

    def flashable(self, path: str, channel: str) -> list[str]:
        builds = next((b for name, b in self.boards.items() if name in path), {})
        return [fw for fw, build in builds.items() if build.is_stable or channel == "dev"]

    async def flash(self, path: str, firmware: str, channel: str, cache: Path):
        if firmware not in self.flashable(path, channel):
            raise ValueError(f"No {firmware} firmware for {path} on the {channel} channel")
        build = next(b[firmware] for name, b in self.boards.items() if name in path)
        file = cache / build.url.rsplit("/", 1)[1]
        if not file.exists():
            cache.mkdir(parents=True, exist_ok=True)
            data = await asyncio.to_thread(lambda: urllib.request.urlopen(build.url, timeout=60).read())
            if hashlib.sha256(data).hexdigest() != build.sha256:
                raise ValueError(f"Checksum mismatch for {build.url}")
            file.write_bytes(data)
        await _flasher(path, "flash", "--firmware", str(file))


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


#: USB ids of radios opened unasked to identify them. Generic bridges common on other boards stay out
#: (CH340 1a86:7523 on Arduinos, Espressif 303a on ESP32s): opening their port resets the board.
#: A device that isn't listed is identified only when asked (`Radio.probe`).
RADIO_USB_IDS = {
    ("10c4", "ea60"),  # CP210x: Home Assistant SkyConnect / ZBT-1, Sonoff ZBDongle-P and Z-Wave PZG23, Zooz ZST10
    ("10c4", "8a2a"),  # Nortek HUSBZB-1
    ("1a86", "55d4"),  # CH9102: Sonoff ZBDongle-E, SMLIGHT SLZB-07
    ("0658", "0200"),  # Sigma Designs: Aeotec Z-Stick, HomeSeer SmartStick
    ("1cf1", "0030"),  # dresden elektronik ConBee II
    ("0403", "6015"),  # FTDI X: ConBee III, ZiGate+
    ("0451", "16a8"),  # TI CC2531
}


def default_usb_drivers() -> list[UsbDriver]:
    """Every driver of the SDK, tried in this order on every unidentified stick: the cheap single-frame Z-Wave check
    first."""
    return [ZWave(), Silabs()]
