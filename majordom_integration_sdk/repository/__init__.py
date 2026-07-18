"""Device persistence for integrations — one protocol, two standalone implementations."""

from .memory import DeviceRepositoryMemory
from .protocol import DeviceRepositoryProtocol
from .sqlite import SqliteDeviceRepository

__all__ = ["DeviceRepositoryProtocol", "DeviceRepositoryMemory", "SqliteDeviceRepository"]
