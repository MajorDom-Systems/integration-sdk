"""Hardware soldered on the board, as peripherals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from majordom_integration_sdk.peripherals import Peripheral, UsbDriver
from majordom_integration_sdk.runtime.peripherals.hardware.drivers import DEFAULT_USB_DRIVERS
from majordom_integration_sdk.runtime.peripherals.hardware.usb import Radio


@dataclass(frozen=True)
class FixedRadio:
    """A radio wired to the board: used without asking, by the driver named ``driver``. With no ``firmware`` that
    driver identifies it; ``params`` override the firmware's serial settings where the board wires them otherwise."""

    name: str
    path: str
    driver: str
    firmware: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


class FixedPeripherals(Peripheral):
    """What is soldered on the board, as its owner describes it (the Hub reads its board profile): each one whose path
    exists is brought along, a radio as a `Radio`.

        FixedPeripherals(FixedRadio("mg26", "/dev/ttyAMA3", "silabs", "multipan-rcp", {"flow_control": False}))
    """

    name = "board"

    def __init__(self, *radios: FixedRadio, drivers: Sequence[UsbDriver] = DEFAULT_USB_DRIVERS):
        self.radios, self.drivers = radios, list(drivers)
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
            await (radio.check_bluetooth() if known else radio.identify())
            present.append(radio)
        self._present = present

    async def stop(self):
        self._present = []
