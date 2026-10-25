"""USB devices as peripherals: the scanner brings one `Radio` along per stick plugged in."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import tempfile
from collections.abc import Callable, Coroutine, Mapping, Sequence
from glob import glob
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from majordom_integration_sdk.peripherals import (
    USB_DRIVER,
    Endpoint,
    Need,
    Peripheral,
    PeripheralChange,
    UsbDriver,
)
from majordom_integration_sdk.runtime.peripherals.hardware.drivers import RADIO_USB_IDS, default_usb_drivers
from majordom_integration_sdk.runtime.peripherals.providers.ble import cpc_hci

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 60  # per driver: one that hangs (bellows resetting a busy port) is given up
POLL = 2  # seconds between looks at the sticks plugged in
FIRMWARE_CACHE = Path(tempfile.gettempdir()) / "majordom-firmware"  # downloads are checked against their checksum
UNSEEN = "another program"  # one that locked a port: whether it let go shows only by opening it


class Radio(Peripheral):
    """A radio, a USB stick or soldered on the board: it serves what its firmware does once a driver identified it, and
    nothing while another program holds it or it is being flashed. Flashing and probing are the user's to ask for."""

    class Settings(BaseModel):
        firmware_channel: Literal["stable", "dev"] = Field(
            "stable", title="Radio firmware", description="Stable offers verified builds only, dev every known build."
        )

    settings: Settings

    def __init__(
        self,
        name: str,
        path: str,
        drivers: Callable[[], Sequence[UsbDriver]],
        driver: UsbDriver | None = None,
        firmware: str | None = None,
        params: Mapping[str, Any] = {},  # the firmware's serial settings, where soldered hardware wires them otherwise
    ):
        self.name, self.path, self.drivers = name, path, drivers
        self.driver, self.firmware, self.params = driver, firmware, params
        self.node = os.path.realpath(path)  # a container keeps the node it was created with: follow a re-enumeration
        self.held_by: str | None = None  # another program that has it open: left alone until probed
        self.is_claimed = False  # the user probed it: opened whoever holds it or whatever its USB id
        self.is_busy = False  # being flashed or probed
        self.has_bluetooth = False  # its multiprotocol firmware serves Bluetooth too
        self.settings = self.Settings()
        self._asked: list[UsbDriver] = []  # the drivers that didn't recognise it

    @property
    def provides(self) -> list[Endpoint]:
        if self.is_busy or self.held_by or self.driver is None:
            return []
        endpoints = self.driver.endpoints(self.firmware, self.node, self.params)
        if self.has_bluetooth:  # the multiprotocol container attaches it and tells its hciN
            endpoints.append(Endpoint("ble", "silabs-cpc-hci", self.node, params={"hci": None}))
        return endpoints

    @property
    def options(self) -> list[str]:
        """Firmwares it can be switched to on its channel."""
        return self.driver.flashable(self.path, self.settings.firmware_channel) if self.driver else []

    async def identify(self, is_asking_again: bool = False) -> bool:
        """Ask the drivers which firmware it runs, each once unless ``is_asking_again``; True once one recognised it."""
        for driver in [d for d in self.drivers() if is_asking_again or all(d is not a for a in self._asked)]:
            if firmware := await _firmware(driver, self.path):
                self.driver, self.firmware = driver, firmware
                await self.check_bluetooth()
                return True
            self._asked.append(driver)
        log.info(f"No driver recognised {self.path}")
        return False

    async def probe(self):
        """Identify it now, also a stick no driver knows the USB id of, or one another program holds: its port is
        opened, which may reset what's behind it. Whatever uses it lets go of it first."""
        async with self._busy():
            self.held_by, self.is_claimed = None, True
            await self.identify(is_asking_again=True)

    async def flash(self, firmware: str):
        """Switch it to ``firmware`` on its channel; whatever uses it lets go of it first and gets it back after."""
        if (driver := self.driver) is None:
            raise LookupError(f"No driver knows {self.name}: probe it first")
        async with self._busy():
            try:
                await driver.flash(self.path, firmware, self.settings.firmware_channel, FIRMWARE_CACHE)
            finally:  # whether it took or not, what ran before may be gone
                self.firmware, self.has_bluetooth = await _firmware(driver, self.path), False
                await self.check_bluetooth()

    @contextlib.asynccontextmanager
    async def _busy(self):
        self.is_busy = True
        await self.runtime.peripheral_did_change_provided(self)  # returns once what used it let go of it
        try:
            yield
        finally:
            self.is_busy = False
            await self.runtime.peripheral_did_change_provided(self)

    async def check_bluetooth(self):
        """Whether a multi-PAN firmware serves Bluetooth too: the driver says where it knows, else it is asked."""
        endpoints = self.driver.endpoints(self.firmware, self.node) if self.driver else []
        if any(e.family == "multipan" for e in endpoints) and self.driver:
            self.has_bluetooth = self.firmware in self.driver.bluetooth or await cpc_hci.has_bluetooth(self.path)

    def __repr__(self) -> str:
        return f"Radio({self.name!r}, {self.firmware!r})"


class UsbScanner(Peripheral):
    """USB devices plugged in (``/dev/serial/by-id``), each brought along as a `Radio` and identified by its drivers,
    then by the ones integrations provide (``usb-driver``): radios today. Only a device whose USB id is a known
    radio's is opened unasked; another is identified when the user asks (`Radio.probe`). One another program holds is
    left alone until then.

        UsbScanner()  # every SDK driver: default_usb_drivers()
        UsbScanner(drivers=[Silabs()])
    """

    name = "usb"
    needs = [Need(USB_DRIVER, is_many=True, is_required=False)]

    def __init__(self, drivers: Sequence[UsbDriver] | None = None):
        self.drivers = default_usb_drivers() if drivers is None else list(drivers)
        self._radios: list[Radio] = []
        self._tasks: set[asyncio.Task] = set()  # watching the sticks, and asking new drivers about them
        self._scanning = asyncio.Lock()  # one scan at a time: two would open a stick at once

    @property
    def provides(self) -> list[Radio]:
        return self._radios

    @property
    def radios(self) -> list[Radio]:
        return list(self._radios)

    async def start(self):
        await self._scan()
        self._spawn(self._watch())

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        self._radios = []

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """An integration brought a driver along: it may know the sticks no other driver did. Asked in the background,
        as a probe may take long."""
        self._spawn(self._rescan())
        return True

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _drivers(self) -> list[UsbDriver]:
        return [*self.drivers, *(endpoint.instance for endpoint in self.peripherals.endpoints(USB_DRIVER))]

    async def _scan(self):
        """Follow the sticks plugged in, and identify the new ones it may open."""
        async with self._scanning:
            radios = {radio.name: radio for radio in self.radios}
            found = _sticks()
            for name, path in found.items():
                radio = radios.get(name) or Radio(name, path, self._drivers)
                radio.node = os.path.realpath(path)
                is_trusted = radio.is_claimed or _usb_id(path) in RADIO_USB_IDS
                if not radio.is_claimed and not radio.is_busy:
                    radio.held_by = _holder(path, is_opening_allowed=is_trusted and radio.driver is None)
                if is_trusted and radio.driver is None and not radio.held_by and not radio.is_busy:
                    await radio.identify()
                radios[name] = radio
            self._radios = [radios[name] for name in sorted(found)]

    async def _watch(self):
        seen = _nodes(_sticks())
        while True:
            await asyncio.sleep(POLL)
            found = _nodes(_sticks())
            if found != seen:  # act once the change held for one more poll (a stick re-enumerating)
                seen = found
                continue
            is_moved = found != {radio.name: radio.node for radio in self.radios}
            is_let_go = any(r.held_by not in (None, UNSEEN) and not _holder(r.path) for r in self.radios)
            if is_moved or is_let_go:
                await self._rescan()

    async def _rescan(self):
        try:
            await self._scan()
            await self.runtime.peripheral_did_change_provided(self)
        except Exception:
            log.exception("Following the USB sticks failed")


async def _firmware(driver: UsbDriver, path: str) -> str | None:
    """What ``driver`` recognises the device at ``path`` running; None when it doesn't, can't tell, or hangs."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            return await driver.identify(path)
    except Exception as error:
        log.warning(f"{driver.name} failed to identify {path}: {_failure(error)}")
        return None


def _failure(error: Exception) -> str:
    return str(error) or type(error).__name__


def _sticks() -> dict[str, str]:
    return {Path(path).name: path for path in glob("/dev/serial/by-id/*")}


def _nodes(paths: Mapping[str, str]) -> dict[str, str]:
    """The device node behind each path. After a USB reset a stick keeps its by-id name but may come back
    on another tty; a container keeps the node it was created with, which may then be another device."""
    return {name: os.path.realpath(path) for name, path in paths.items()}


def _usb_id(path: str) -> tuple[str, str] | None:
    """USB vendor and product id of the device behind a serial port; None for a UART."""
    device = Path("/sys/class/tty", Path(os.path.realpath(path)).name, "device").resolve()
    for parent in (device, *device.parents):
        with contextlib.suppress(OSError):
            return (parent / "idVendor").read_text().strip(), (parent / "idProduct").read_text().strip()
    return None


def _holder(path: str, is_opening_allowed: bool = False) -> str | None:
    """The other program that has a serial port open: one whose open files we may read, or with
    ``is_opening_allowed`` (it opens the port) one that locked it, whoever runs it (`UNSEEN`). Our own containers see
    the port under another name (/dev/ttyRCP), so they don't count."""
    names = {path, os.path.realpath(path)}
    for fd in glob("/proc/[0-9]*/fd/*"):
        with contextlib.suppress(OSError):
            if (pid := fd.split("/")[2]) != str(os.getpid()) and os.readlink(fd) in names:
                return Path(f"/proc/{pid}/comm").read_text().strip()
    if not is_opening_allowed:
        return None
    try:
        port = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        return UNSEEN if error.errno == errno.EBUSY else None  # EBUSY: opened exclusively (TIOCEXCL)
    try:
        fcntl.flock(port, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyserial's and node-serialport's exclusive lock
    except OSError:
        return UNSEEN
    finally:
        os.close(port)
    return None
