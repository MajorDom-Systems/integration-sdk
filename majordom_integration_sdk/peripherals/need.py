"""What a controller, a service or a runtime provider requires from its environment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from majordom_integration_sdk.peripherals.endpoint import Endpoint


@dataclass(frozen=True)
class Need:
    """A dependency something else must provide, as ``family`` and the ``dialects`` it speaks.

    The runtime binds it to an `Endpoint` and hands that over, to a controller in ``self.peripherals`` and to a
    `Service` as ``{peripherals.<family>.url}``. A ``many`` need binds every matching endpoint that is free, as a
    list: a Bluetooth router hears through every radio it is given.
    """

    family: str
    dialects: Sequence[str] = ()  # empty: any dialect of the family
    required: bool = True
    many: bool = False

    def accepts(self, endpoint: Endpoint) -> bool:
        return endpoint.family == self.family and (not self.dialects or endpoint.dialect in self.dialects)
