"""Both first-class repositories must satisfy the same protocol behaviour."""

from uuid import uuid4

import pytest
from conftest import make_device_state

from majordom_integration_sdk.repository import (
    DeviceRepositoryMemory,
    DeviceRepositoryProtocol,
    SqliteDeviceRepository,
)
from majordom_integration_sdk.schemas.base import Base
from majordom_integration_sdk.schemas.device import Device
from majordom_integration_sdk.schemas.parameter import ParameterVisibility


class _PairingData(Base):
    """An integration's own typed `integration_data` (e.g. HomeKit pairing data)."""

    token: str


class _MyDevice(Device):
    integration_data: _PairingData


def make_my_device(device_id=None, token: str = "secret", integration: str = "example") -> _MyDevice:
    return _MyDevice(
        id=device_id or uuid4(),
        name="Lamp",
        room_id=uuid4(),
        transport="wifi",
        integration=integration,
        manufacturer="ACME",
        integration_data=_PairingData(token=token),
    )


@pytest.fixture(params=["memory", "sqlite"])
def repository(request, tmp_path) -> DeviceRepositoryProtocol:
    if request.param == "memory":
        return DeviceRepositoryMemory()
    return SqliteDeviceRepository(tmp_path / "devices.db")


async def test_save_and_read_back(repository, device_state):
    async with repository.session() as repo:
        await repo.save(device_state)

        state = await repo.state(device_state.id)
        got = await repo.get(device_state.id)
        assert state is not None and state.id == device_state.id
        assert got is not None and got.id == device_state.id
        assert [d.id for d in await repo.get_all()] == [device_state.id]


async def test_get_unknown_is_none(repository):
    async with repository.session() as repo:
        assert await repo.state(uuid4()) is None
        assert await repo.get(uuid4()) is None


async def test_parameter_state_round_trip(repository, device_state):
    parameter = device_state.parameters[0]
    async with repository.session() as repo:
        await repo.save(device_state)

        fetched = await repo.get_parameter_state(device_state.id, parameter.id)
        assert fetched is not None and fetched.id == parameter.id

        parameter.value = 7
        await repo.save_parameter_state(device_state.id, parameter)
        updated = await repo.get_parameter_state(device_state.id, parameter.id)
        assert updated is not None and updated.value == 7


async def test_update_visibility(repository, device_state):
    parameter = device_state.parameters[0]
    async with repository.session() as repo:
        await repo.save(device_state)
        await repo.update_parameter_visibility(parameter.id, ParameterVisibility.system)
        fetched = await repo.get_parameter_state(device_state.id, parameter.id)
        assert fetched is not None and fetched.visibility is ParameterVisibility.system


async def test_save_with_previous_id_renames_row(repository):
    provisional = make_device_state(id=uuid4())
    final_id = uuid4()
    async with repository.session() as repo:
        await repo.save(provisional)
        renamed = provisional.model_copy(update={"id": final_id})
        await repo.save(renamed, previous_id=provisional.id)

        assert await repo.state(provisional.id) is None
        renamed_state = await repo.state(final_id)
        assert renamed_state is not None and renamed_state.id == final_id


async def test_delete(repository, device_state):
    async with repository.session() as repo:
        await repo.save(device_state)
        await repo.delete(device_state.id)
        assert await repo.state(device_state.id) is None


async def test_typed_read_returns_typed_integration_data(repository):
    device = make_my_device(token="secret")
    async with repository.session() as repo:
        await repo.save(device)

        got = await repo.get(device.id, as_=_MyDevice)
        assert got is not None
        assert isinstance(got.integration_data, _PairingData)
        assert got.integration_data.token == "secret"

        listed = await repo.get_all(as_=_MyDevice)
        assert [d.integration_data.token for d in listed] == ["secret"]


async def test_device_and_state_views_merge(repository, device_state):
    """Device carries integration_data, DeviceState carries parameters — they're siblings, so
    saving one must not clobber the other's half of the record."""
    device = make_my_device(device_id=device_state.id, token="t", integration=device_state.integration)
    async with repository.session() as repo:
        await repo.save(device_state)  # info + parameters
        await repo.save(device)  # info + integration_data

        state = await repo.state(device_state.id)
        typed = await repo.get(device_state.id, as_=_MyDevice)
        assert state is not None and len(state.parameters) == 1, "parameters must survive a Device save"
        assert typed is not None and typed.integration_data.token == "t"

        # ...and the reverse order.
        await repo.save(device_state)
        typed_again = await repo.get(device_state.id, as_=_MyDevice)
        assert typed_again is not None, "integration_data must survive a DeviceState save"
        assert typed_again.integration_data.token == "t"


@pytest.fixture(params=["memory", "sqlite"])
def make_scoped(request, tmp_path):
    """Factory building repositories that share one backing store, scoped per call."""
    store: dict = {}
    path = tmp_path / "scoped.db"

    def make(integration):
        if request.param == "memory":
            return DeviceRepositoryMemory(integration=integration, store=store)
        return SqliteDeviceRepository(path, integration=integration)

    return make


async def test_scope_limits_get_all(make_scoped):
    admin = make_scoped(None)  # full access
    async with admin.session() as repo:
        await repo.save(make_device_state(integration="mine"))
        await repo.save(make_device_state(integration="other"))

    async with make_scoped("mine").session() as repo:
        assert {d.integration for d in await repo.get_all()} == {"mine"}
    async with make_scoped(None).session() as repo:
        assert {d.integration for d in await repo.get_all()} == {"mine", "other"}


async def test_scope_rejects_foreign_save(make_scoped):
    async with make_scoped("mine").session() as repo:
        with pytest.raises(PermissionError):
            await repo.save(make_device_state(integration="other"))


async def test_scope_hides_foreign_reads(make_scoped):
    foreign = make_device_state(integration="other")
    async with make_scoped(None).session() as repo:
        await repo.save(foreign)
    async with make_scoped("mine").session() as repo:
        assert await repo.state(foreign.id) is None
        assert await repo.get(foreign.id) is None
        assert await repo.get_parameter_state(foreign.id, foreign.parameters[0].id) is None


async def test_sqlite_persists_across_sessions(tmp_path, device_state):
    repo = SqliteDeviceRepository(tmp_path / "devices.db")
    async with repo.session() as session:
        await session.save(device_state)
    # A brand-new repository object over the same file still sees the device.
    reopened = SqliteDeviceRepository(tmp_path / "devices.db")
    async with reopened.session() as session:
        persisted = await session.state(device_state.id)
        assert persisted is not None and persisted.id == device_state.id
