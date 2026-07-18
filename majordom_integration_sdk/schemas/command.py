from typing import Any
from uuid import UUID

from .base import Base


class DeviceCommand(Base):
    device_id: UUID
    parameter_id: UUID
    value: Any
