"""The contracts of the peripherals the runtime provides in-process, for integrations to use through their needs:
``ble/router`` (`ble`, secured by `ble_security`), ``mdns/dns-sd`` (`dns_sd`) and ``upnp/ssdp`` (`ssdp`). Interfaces
only: the implementations live in `majordom_integration_sdk.runtime`."""

from .ble import (
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
from .dns_sd import DNSSD, ZeroconfDiscoveryInfo, ZeroconfDiscoveryListener
from .ssdp import SSDP, SSDPDiscoveryInfo, SSDPDiscoveryListener

__all__ = [
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
    "DNSSD",
    "ZeroconfDiscoveryInfo",
    "ZeroconfDiscoveryListener",
    "SSDP",
    "SSDPDiscoveryInfo",
    "SSDPDiscoveryListener",
]
