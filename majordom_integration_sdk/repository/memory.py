"""In-memory :class:`DeviceRepositoryProtocol` implementation.

A legitimate, lightweight persistence choice for standalone/dev runs and tests — not a
mock. State lives in a dict for the process lifetime; use :class:`SqliteDeviceRepository`
when you need it to survive a restart.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from majordom_integration_sdk.repository._record import Record, dump, merge
from majordom_integration_sdk.schemas.device import Device, DeviceState
from majordom_integration_sdk.schemas.parameter import ParameterState, ParameterVisibility


class DeviceRepositoryMemory:
    """Stores device records in a dict shared across sessions.

    Pass ``integration`` to scope every operation to that integration's devices (the guard
    the Hub sets when handing a repository to a controller). ``None`` gives full access.
    """

    def __init__(self, integration: str | None = None, store: dict[UUID, Record] | None = None):
        self.integration = integration
        self._store: dict[UUID, Record] = store if store is not None else {}

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[DeviceRepositoryMemory]:
        """One unit of work: what ``Platform.make_device_repository`` returns for its integration."""
        yield self

    def _in_scope(self, record: Record) -> bool:
        return self.integration is None or record.get("integration") == self.integration

    def _owned(self, physical_id: UUID) -> Record | None:
        return next(
            (r for r in self._store.values() if r.get("physical_id") == str(physical_id) and self._in_scope(r)), None
        )

    async def get_all[T: Device](self, as_: type[T] = Device) -> list[T]:
        return [as_.model_validate(r) for r in self._store.values() if self._in_scope(r)]

    async def get[T: Device](self, physical_id: UUID, as_: type[T] = Device) -> T | None:
        record = self._owned(physical_id)
        return as_.model_validate(record) if record else None

    async def state[T: DeviceState](self, physical_id: UUID, as_: type[T] = DeviceState) -> T | None:
        record = self._owned(physical_id)
        return as_.model_validate(record) if record else None

    async def get_parameter_state(self, physical_id: UUID, parameter_id: UUID) -> ParameterState | None:
        record = self._owned(physical_id)
        if not record:
            return None
        return next(
            (ParameterState.model_validate(p) for p in record.get("parameters", []) if p["id"] == str(parameter_id)),
            None,
        )

    async def save(self, device: Device | DeviceState):
        if self.integration is not None and device.integration != self.integration:
            raise PermissionError(f"Device {device.id} is outside integration scope {self.integration!r}")
        holder = next(
            (i for i, r in self._store.items() if r.get("physical_id") == str(device.physical_id) and i != device.id),
            None,
        )
        if holder is not None:
            raise ValueError(f"Physical id {device.physical_id} is held by device {holder}")
        self._store[device.id] = merge(self._store.get(device.id), device)

    async def save_parameter_state(self, physical_id: UUID, parameter_state: ParameterState):
        record = self._owned(physical_id)
        if not record:
            raise KeyError(f"Unknown or out-of-scope device {physical_id}")
        updated = dump(parameter_state)
        record["parameters"] = [
            updated if p["id"] == str(parameter_state.id) else p for p in record.get("parameters", [])
        ]

    # Hub-internal (not on DeviceRepositoryProtocol), by the Hub's ids; still scope-checked.

    async def update_parameter_visibility(self, parameter_id: UUID, visibility: ParameterVisibility):
        for record in self._store.values():
            if not self._in_scope(record):
                continue
            for parameter in record.get("parameters", []):
                if parameter["id"] == str(parameter_id):
                    parameter["visibility"] = visibility.value
                    return

    async def delete(self, device_id: UUID):
        record = self._store.get(device_id)
        if record is not None and self._in_scope(record):
            del self._store[device_id]


__all__ = ["DeviceRepositoryMemory"]
