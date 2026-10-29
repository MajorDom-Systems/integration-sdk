"""The peripherals the runtime runs in its own process, on demand, each provided as a shared endpoint carrying itself:
the Bluetooth router (``ble/router``, its sources: the ``ble`` extra), DNS-SD (``mdns/zeroconf``, the ``discovery``
extra) and SSDP (``upnp/ssdp``); and `BLEProxy`, a Bluetooth source an integration lends the router."""
