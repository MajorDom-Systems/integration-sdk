"""File-backed :class:`DeviceRepositoryProtocol` on the standard-library ``sqlite3``.

No third-party dependency: each device is stored as one row of JSON (the record already
stores pythonic parameter values as JSON via Pydantic). Suitable
for a standalone integration that needs its paired devices to survive a restart; the Hub
uses its own SQLAlchemy-backed repository instead.

``sqlite3`` is synchronous; calls are cheap and local, so the async methods invoke it
directly. Obtain a session with :meth:`session` (opens a connection, commits on clean exit).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from majordom_integration_sdk.repository._record import Record, dump, merge
from majordom_integration_sdk.schemas.device import Device, DeviceState
from majordom_integration_sdk.schemas.parameter import ParameterState, ParameterVisibility


class SqliteDeviceRepository:
    """Persists device records as JSON rows in a SQLite file.

    Pass ``integration`` to scope every operation to that integration's devices (the guard
    the Hub sets when handing a repository to a controller). ``None`` gives full access.
    """

    def __init__(self, path: str | Path, integration: str | None = None):
        self.path = str(path)
        self.integration = integration
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS devices ("
                "  id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL,"
                "  integration TEXT NOT NULL, data TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @asynccontextmanager
    async def session(self) -> AsyncIterator[SqliteDeviceRepository]:
        """Async-context-manager factory to pass as ``make_device_repository``."""
        # A fresh connection per unit of work; sharing one across concurrent tasks is unsafe.
        self._conn = self._connect()
        try:
            yield self
            self._conn.commit()
        finally:
            self._conn.close()
            del self._conn

    def _cursor(self) -> sqlite3.Connection:
        conn = getattr(self, "_conn", None)
        if conn is None:
            raise RuntimeError("Use `async with repo.session():` before calling repository methods")
        return conn

    def _record(self, device_id: UUID) -> Record | None:
        """The stored record for an in-scope device, or None."""
        if self.integration is None:
            row = self._cursor().execute("SELECT data FROM devices WHERE uuid = ?", (str(device_id),)).fetchone()
        else:
            row = (
                self._cursor()
                .execute(
                    "SELECT data FROM devices WHERE uuid = ? AND integration = ?",
                    (str(device_id), self.integration),
                )
                .fetchone()
            )
        return json.loads(row["data"]) if row else None

    def _write(self, record: Record) -> None:
        self._cursor().execute(
            "INSERT INTO devices (uuid, integration, data) VALUES (?, ?, ?) "
            "ON CONFLICT(uuid) DO UPDATE SET integration = excluded.integration, data = excluded.data",
            (record["id"], record["integration"], json.dumps(record)),
        )

    async def get_all[T: Device](self, as_: type[T] = Device) -> list[T]:
        if self.integration is None:
            rows = self._cursor().execute("SELECT data FROM devices").fetchall()
        else:
            rows = (
                self._cursor().execute("SELECT data FROM devices WHERE integration = ?", (self.integration,)).fetchall()
            )
        return [as_.model_validate(json.loads(row["data"])) for row in rows]

    async def get[T: Device](self, device_id: UUID, as_: type[T] = Device) -> T | None:
        record = self._record(device_id)
        return as_.model_validate(record) if record else None

    async def state[T: DeviceState](self, device_id: UUID, as_: type[T] = DeviceState) -> T | None:
        record = self._record(device_id)
        return as_.model_validate(record) if record else None

    async def get_parameter_state(self, device_id: UUID, parameter_id: UUID) -> ParameterState | None:
        record = self._record(device_id)
        if not record:
            return None
        return next(
            (ParameterState.model_validate(p) for p in record.get("parameters", []) if p["id"] == str(parameter_id)),
            None,
        )

    async def save(self, device: Device | DeviceState, previous_id: UUID | None = None) -> None:
        if self.integration is not None and device.integration != self.integration:
            raise PermissionError(f"Device {device.id} is outside integration scope {self.integration!r}")
        base: Record | None = None
        if previous_id is not None and previous_id != device.id:
            base = self._record(previous_id)
            self._cursor().execute("DELETE FROM devices WHERE uuid = ?", (str(previous_id),))
        self._write(merge(base if base is not None else self._record(device.id), device))

    async def save_parameter_state(self, device_id: UUID, parameter_state: ParameterState) -> None:
        record = self._record(device_id)
        if not record:
            raise KeyError(f"Unknown or out-of-scope device {device_id}")
        updated = dump(parameter_state)
        record["parameters"] = [
            updated if p["id"] == str(parameter_state.id) else p for p in record.get("parameters", [])
        ]
        self._write(record)

    # Hub-internal (not on DeviceRepositoryProtocol); still scope-checked.

    async def update_parameter_visibility(self, parameter_id: UUID, visibility: ParameterVisibility) -> None:
        for device in await self.get_all():
            record = self._record(device.id)
            if not record:
                continue
            if any(p["id"] == str(parameter_id) for p in record.get("parameters", [])):
                for parameter in record["parameters"]:
                    if parameter["id"] == str(parameter_id):
                        parameter["visibility"] = visibility.value
                self._write(record)
                return

    async def delete(self, device_id: UUID) -> None:
        if self._record(device_id) is None:
            return
        self._cursor().execute("DELETE FROM devices WHERE uuid = ?", (str(device_id),))


__all__ = ["SqliteDeviceRepository"]
