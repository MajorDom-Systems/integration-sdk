"""Bluetooth of a Silicon Labs multiprotocol radio, as the ``ble/silabs-cpc-hci`` source of the router.

A multi-PAN RCP whose firmware carries the Bluetooth controller serves HCI over CPC next to 802.15.4: cpcd's
``cpc-hci-bridge`` exposes it as a pty, and ``btattach`` makes that a BlueZ adapter (``hciN``), heard through bleak like
any other. The radio provides the endpoint only when its firmware has Bluetooth: from the driver's firmware
metadata (`UsbDriver.bluetooth`) where it knows, else from `has_bluetooth`, asked once per radio.

To verify on hardware (nothing here is yet): which CPC endpoint answers for Bluetooth on a given firmware (and how to
ask cpcd without taking the port from the multiprotocol container), and the ``hciN`` the bridge comes up as — it
arrives in the endpoint's ``params["hci"]`` once the multiprotocol service attaches it.
"""

from __future__ import annotations

from majordom_integration_sdk.capabilities.ble import BLESource
from majordom_integration_sdk.peripherals import Endpoint


async def has_bluetooth(path: str) -> bool:
    """Whether the multi-PAN radio at ``path`` serves Bluetooth over CPC. Unverified on hardware: says no until it
    asks cpcd (see the module docstring)."""
    return False


def source_of(endpoint: Endpoint) -> BLESource:
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter  # the ble extra

    if not (hci := endpoint.params.get("hci")):
        raise ConnectionError(f"{endpoint.url} has no Bluetooth adapter attached yet")
    return LocalAdapter(hci)
