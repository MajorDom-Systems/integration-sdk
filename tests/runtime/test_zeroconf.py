"""The runtime's DNS-SD browser, over fakes of zeroconf's: no multicast socket is opened."""

from typing import cast

import pytest

from majordom_integration_sdk.runtime.peripherals.providers import zeroconf as module
from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService


class Browser:
    def __init__(self, zeroconf: object, services: list[str], listener: object):
        self.cancels = 0

    async def async_cancel(self):
        self.cancels += 1


class AsyncZeroconf:
    zeroconf = None

    async def async_close(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(module, "AsyncZeroconf", AsyncZeroconf)
    monkeypatch.setattr(module, "AsyncServiceBrowser", Browser)


async def test_a_registration_cancelled_after_it_stopped_is_gone_already():
    zeroconf = ZeroconfDiscoveryService()
    await zeroconf.start()
    cancel = zeroconf.register(object(), {"_esphomelib._tcp.local."})
    [browser] = cast(set[Browser], zeroconf._browsers)
    await zeroconf.stop()  # before a user that cancels only then

    cancel()
    assert browser.cancels == 1 and zeroconf._adapters == []
