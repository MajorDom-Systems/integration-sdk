"""What runs by default, and that it is there without the extras."""

import subprocess
import sys

from majordom_integration_sdk.runtime import UsbScanner, default_peripherals, default_usb_drivers
from majordom_integration_sdk.runtime.peripherals.services.builtin import otbr, silabs_multiprotocol

WITHOUT_EXTRAS = """
import importlib.abc, sys

class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"serial", "bleak", "dbus_fast", "zeroconf", "typer_shell", "rich"}:
            raise ImportError(f"No module named {name!r}")

sys.meta_path.insert(0, Missing())
import majordom_integration_sdk, majordom_integration_sdk.dev, majordom_integration_sdk.testing
from majordom_integration_sdk.runtime import default_peripherals
print(" ".join(p.name for p in default_peripherals()))
"""


def test_the_defaults_are_what_runs_around_integrations_in_the_order_they_bind():
    assert [p.name for p in default_peripherals()] == [
        "usb",
        "bluetooth-adapters",
        "host",
        "silabs-multiprotocol",
        "otbr",
        "ble-router",
        "zeroconf",
        "ssdp",
    ]
    on_demand = [p.name for p in default_peripherals() if p.is_on_demand]
    assert on_demand == ["silabs-multiprotocol", "otbr", "ble-router", "zeroconf", "ssdp"]


def test_the_usb_scanner_knows_every_sdk_driver_by_default():
    assert [driver.name for driver in default_usb_drivers()] == ["zwave", "silabs"]  # the cheap Z-Wave check first
    assert [driver.name for driver in UsbScanner().drivers] == ["zwave", "silabs"]
    scanner = next(p for p in default_peripherals() if isinstance(p, UsbScanner))
    assert [driver.name for driver in scanner.drivers] == ["zwave", "silabs"]


def test_every_runtime_gets_defaults_of_its_own():
    first, second = default_peripherals(), default_peripherals()
    assert not {id(p) for p in first} & {id(p) for p in second}
    assert otbr() is not otbr() and silabs_multiprotocol() is not silabs_multiprotocol()
    assert not {id(d) for d in default_usb_drivers()} & {id(d) for d in default_usb_drivers()}
    assert not {id(d) for d in UsbScanner().drivers} & {id(d) for d in UsbScanner().drivers}


def test_the_package_and_its_defaults_import_without_the_extras():
    result = subprocess.run([sys.executable, "-c", WITHOUT_EXTRAS], capture_output=True, text=True, check=True)
    assert result.stdout.split() == [
        "usb",
        "bluetooth-adapters",
        "host",
        "silabs-multiprotocol",
        "otbr",
        "ble-router",
        "ssdp",
    ]
    assert 'pip install "majordom-integration-sdk[discovery]"' in result.stderr  # no DNS-SD, said once
