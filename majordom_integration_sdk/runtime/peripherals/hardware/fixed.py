"""Hardware soldered on the board, as peripherals."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from majordom_integration_sdk.peripherals import Peripheral, UsbDriver
from majordom_integration_sdk.runtime.peripherals.hardware.catalog import Catalog
from majordom_integration_sdk.runtime.peripherals.hardware.drivers import default_usb_drivers
from majordom_integration_sdk.runtime.peripherals.hardware.usb import Radio


@dataclass(frozen=True)
class FixedRadio:
    """A radio wired to the board: used without asking, by the driver named ``driver``. With no ``firmware`` that
    driver identifies it; ``params`` override the firmware's serial settings where the board wires them otherwise, and
    may name the line wired to its reset (``reset_gpio``, e.g. ``gpiochip0:23``) for a multi-PAN RCP on a bare UART."""

    name: str
    path: str
    driver: str
    firmware: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


OS_BOARD_PROFILE = Path("/etc/majordom/board.json")  # where the MajorDom OS image installs its board's profile


def parse_board_profile(profile: Any) -> list[FixedRadio]:
    """The radios of a ``board.json``: the firmware repo's ``{"schema": 1, "board", "revision", "slots": [{"name",
    "path", "driver", "firmware"?, "params"}]}``, or the Hub's older bare list of ``{"id", ...}``."""
    radios = profile if isinstance(profile, list) else profile["slots"]
    return [
        FixedRadio(r.get("name") or r["id"], r["path"], r["driver"], r.get("firmware"), r.get("params", {}))
        for r in radios
    ]


def board_profile(board: str | Path | None, catalog: Catalog | None = None) -> list[FixedRadio]:
    """The radios soldered on ``board``: a ``board.json`` file (`OS_BOARD_PROFILE` on the MajorDom OS image), a board id
    of the firmware ``catalog`` (``hub1-cm4``) for a DIY build of one, or none (USB sticks only)."""
    if board is None:
        return []
    if isinstance(board, Path):
        return parse_board_profile(json.loads(board.read_bytes()))
    catalog = catalog or Catalog.default()
    return parse_board_profile(catalog.profile(board, default_usb_drivers(catalog)))


class FixedPeripherals(Peripheral):
    """What is soldered on the board, as its owner describes it (the Hub reads its board profile): each one whose path
    exists is brought along, a radio as a `Radio`.

        FixedPeripherals(
            FixedRadio("mg26", "/dev/ttyAMA3", "silabs", "multipan-rcp", {"baud": 115200, "flow_control": False})
        )
        FixedPeripherals(*board_profile(OS_BOARD_PROFILE))  # the MajorDom OS image
        FixedPeripherals(*board_profile("hub1-cm4", catalog), catalog=catalog)  # a DIY build of a catalog board
    """

    name = "board"

    def __init__(self, *radios: FixedRadio, drivers: Sequence[UsbDriver] | None = None, catalog: Catalog | None = None):
        self.radios, self.drivers = radios, default_usb_drivers(catalog) if drivers is None else list(drivers)
        self._present: list[Radio] = []

    @property
    def provides(self) -> list[Radio]:
        return self._present

    async def start(self):
        drivers = {driver.name: driver for driver in self.drivers}
        present = []
        for fixed in self.radios:
            if not Path(fixed.path).exists():
                continue
            driver = drivers[fixed.driver]
            known = driver if fixed.firmware else None
            radio = Radio(fixed.name, fixed.path, lambda driver=driver: [driver], known, fixed.firmware, fixed.params)
            await (radio.check_bluetooth() if known else radio.recognise())
            present.append(radio)
        self._present = present

    async def stop(self):
        self._present = []
