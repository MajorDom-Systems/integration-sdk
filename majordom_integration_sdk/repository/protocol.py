"""The device-repository interface a controller talks to.

A controller never touches a database directly — it goes through this protocol, obtained
from ``dependencies.make_device_repository()`` (an async context manager scoping one unit
of work). The Hub backs it with its shared SQLAlchemy database; the SDK ships two
first-class implementations for standalone/dev use — :class:`DeviceRepositoryMemory` and
:class:`SqliteDeviceRepository`.

**Integration scope.** The repository is bound to one integration at construction, so there
is no ``integration`` argument on the reads: an integration only ever sees its own devices.
Out-of-scope reads come back ``None``/empty and out-of-scope writes raise — a guard against
carelessness, not a security boundary. Constructing with ``integration=None`` gives full
access (the Hub's own higher-level use).

**Typed reads.** ``as_`` deserializes into your own ``Device``/``DeviceState`` subclass, so
``integration_data`` comes back typed instead of a bare dict::

    async with self.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MyDevice)  # device.integration_data: MyData

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

    async def get[T: Device](self, device_id: UUID, as_: type[T] = Device) -> T | None:
        """The device (info + ``integration_data``) as ``as_``, or ``None`` if unknown/out of scope."""
        ...

    async def state[T: DeviceState](self, device_id: UUID, as_: type[T] = DeviceState) -> T | None:
        """The device with its parameter states as ``as_``, or ``None`` if unknown/out of scope."""
        ...

    async def get_parameter_state(self, device_id: UUID, parameter_id: UUID) -> ParameterState | None:
        """One parameter's current state, or ``None`` if the device/parameter is unknown/out of scope."""
        ...

    async def save(self, device: Device | DeviceState, previous_id: UUID | None = None) -> None:
        """Insert or update a device, merging into whatever is already stored.

        Saving a ``Device`` updates its info/``integration_data``; saving a ``DeviceState``
        updates its info/``parameters`` — neither clobbers the other's fields. ``previous_id``
        renames an existing record before the write (pairing turning a provisional discovery
        id into the device's final id), carrying its stored data over. Rejects a device
        outside the bound integration's scope.
        """
        ...

    async def save_parameter_state(self, device_id: UUID, parameter_state: ParameterState) -> None:
        """Update a single parameter's value/state on an existing in-scope device."""
        ...


__all__ = ["DeviceRepositoryProtocol"]
