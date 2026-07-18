from typing import Any
from uuid import UUID

from .base import Base


class Event(Base):
    """Base class for device-domain events a controller reports to the Hub.

    Delivered through :meth:`ControllerOutput.controller_did_receive_events`. A parameter
    change is the first concrete kind; more (button presses, device online/offline, …) can
    extend this without changing the callback. Kept device-domain and distinct from the
    Hub's automation event bus, which wraps these on its side.
    """


class DeviceParameterChange(Event):
    """A device reported that one of its parameters changed to a new value.

    Mirrors :class:`DeviceCommand`'s shape (``device_id``, ``parameter_id``, ``value``) but
    flows the other direction — device → Hub.
    """

    device_id: UUID
    parameter_id: UUID
    value: Any
