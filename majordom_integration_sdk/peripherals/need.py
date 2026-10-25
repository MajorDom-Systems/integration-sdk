"""What a peripheral requires from its environment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from majordom_integration_sdk.peripherals.endpoint import Endpoint


@dataclass(frozen=True)
class Need:
    """A dependency something else must provide, as ``family`` and the ``dialects`` it speaks.

    The runtime binds it to an `Endpoint` and hands that over in the peripheral's `Peripherals`, to a controller as
    ``self.peripherals.endpoint(<family>)`` and to a `Service` as ``{peripherals.<family>.url}``. An ``is_many`` need
    binds every matching endpoint that is free, as a list: a Bluetooth router hears through every radio it is given.
    """

    family: str
    dialects: Sequence[str] = ()  # empty: any dialect of the family
    is_required: bool = True
    is_many: bool = False

    def accepts(self, endpoint: Endpoint) -> bool:
        return endpoint.family == self.family and (not self.dialects or endpoint.dialect in self.dialects)
