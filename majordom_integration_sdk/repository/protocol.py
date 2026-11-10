"""The device-repository interface a controller talks to.

A controller never touches a database directly — it goes through this protocol, obtained
from its platform's ``device_repository(self)`` (an async context manager scoping one unit of
work). The
Hub backs it with its shared SQLAlchemy database; the SDK ships two first-class
implementations for standalone/dev use — :class:`DeviceRepositoryMemory` and
:class:`SqliteDeviceRepository`.

**Integration scope.** The repository is bound to one integration at construction, so there
is no ``integration`` argument on the reads: an integration only ever sees its own devices.
Out-of-scope reads come back ``None``/empty and out-of-scope writes raise — a guard against
carelessness, not a security boundary. Constructing with ``integration=None`` gives full
access (the Hub's own higher-level use).

**Keyed by ``physical_id``.** An integration looks its devices up by the ``physical_id`` it gave them
(``device_uuid``), never by the Hub-owned ``id``: ``get``, ``state``, ``get_parameter_state`` and
``save_parameter_state`` take it, ``save`` writes the record of ``device.id`` (read-only, set by the Hub).

**Typed reads.** ``as_`` deserializes into your own ``Device[MyData]`` /
``DeviceState[MyData, MyParameterData]`` (or a subclass), so ``integration_data`` comes back
typed instead of a bare dict::

    async with self.platform.device_repository(self) as repo:
        device = await repo.get(physical_id, as_=MyDevice)  # class MyDevice(Device[MyData])

**Not here on purpose:** ``delete`` and ``update_parameter_visibility`` are Hub-internal
lifecycle operations. They exist on the concrete implementations, but are kept off this
protocol so an integration can't reach past its scope.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from majordom_integration_sdk.schemas.device import Device, DeviceState
from majordom_integration_sdk.schemas.parameter import ParameterState


@runtime_checkable
class DeviceRepositoryProtocol(Protocol):
    """Persistence for one integration's paired devices and their parameter states."""

    async def get_all[T: Device](self, as_: type[T] = Device) -> list[T]:
        """Every paired device in scope, deserialized as ``as_``."""
        ...

    async def get[T: Device](self, physical_id: UUID, as_: type[T] = Device) -> T | None:
        """The device (info + ``integration_data``) as ``as_``, or ``None`` if unknown/out of scope."""
        ...

    async def state[T: DeviceState](self, physical_id: UUID, as_: type[T] = DeviceState) -> T | None:
        """The device with its parameter states as ``as_``, or ``None`` if unknown/out of scope."""
        ...

    async def get_parameter_state(self, physical_id: UUID, parameter_id: UUID) -> ParameterState | None:
        """One parameter's current state, or ``None`` if the device/parameter is unknown/out of scope."""
        ...

    async def save(self, device: Device | DeviceState):
        """Insert or update a device, merging into whatever is already stored.

        Saving a ``Device`` updates its info/``integration_data``; saving a ``DeviceState``
        updates its info/``parameters``, and its ``integration_data`` only when it was given
        one — neither clobbers the other's fields. The record is the one of ``device.id``; its
        ``physical_id`` must not be another record's. Rejects a device outside the bound
        integration's scope.
        """
        ...

    async def save_parameter_state(self, physical_id: UUID, parameter_state: ParameterState):
        """Update a single parameter's value/state on an existing in-scope device."""
        ...


__all__ = ["DeviceRepositoryProtocol"]
