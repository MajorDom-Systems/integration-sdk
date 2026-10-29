"""The contracts of the peripherals the runtime provides in-process, for integrations to use through their needs:
``ble/router`` (`ble`, secured by `ble_security`), ``mdns/zeroconf`` (`zeroconf`) and ``upnp/ssdp`` (`ssdp`). Interfaces
only: the implementations live in `majordom_integration_sdk.runtime`."""

from .ble import (
    BLE_ROUTER,
    BLEBusyError,
    BLEConnection,
    BLEDiscoveryInfo,
    BLEDiscoveryListener,
    BLELease,
    BLEMatcher,
    BLEPairingError,
    BLERouter,
    BLESource,
    GattCharacteristic,
    GattService,
    OnAdvertisement,
    PairableBLEConnection,
    PausableBLESource,
    ScanModeBLESource,
)
from .ble_security import LESecureConnections, SecureChannel, Unsecured
from .ssdp import SSDP, UPNP_SSDP, SSDPDiscoveryInfo, SSDPDiscoveryListener
from .zeroconf import ZEROCONF, Zeroconf, ZeroconfDiscoveryInfo, ZeroconfDiscoveryListener

__all__ = [
    "BLE_ROUTER",
    "ZEROCONF",
    "UPNP_SSDP",
    "BLEBusyError",
    "BLEConnection",
    "BLEDiscoveryInfo",
    "BLEDiscoveryListener",
    "BLELease",
    "BLEMatcher",
    "BLEPairingError",
    "BLERouter",
    "BLESource",
    "GattCharacteristic",
    "GattService",
    "OnAdvertisement",
    "PairableBLEConnection",
    "PausableBLESource",
    "ScanModeBLESource",
    "LESecureConnections",
    "SecureChannel",
    "Unsecured",
    "Zeroconf",
    "ZeroconfDiscoveryInfo",
    "ZeroconfDiscoveryListener",
    "SSDP",
    "SSDPDiscoveryInfo",
    "SSDPDiscoveryListener",
]
