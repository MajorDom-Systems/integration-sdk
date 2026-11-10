"""MissingCredentialsError: what the Hub lacks to pair a device."""

from uuid import UUID

import pytest

from majordom_integration_sdk import DeviceIdTakenError, MissingCredentialsError


def test_the_type_defaults_to_wifi():
    assert MissingCredentialsError().type == MissingCredentialsError.WIFI == "wifi"


def test_the_known_types():
    assert MissingCredentialsError.THREAD == "thread"


def test_the_message_carries_the_type():
    assert str(MissingCredentialsError("thread")) == "Missing credentials: thread"


def test_a_custom_type_is_accepted():
    assert MissingCredentialsError("zigbee_network_key").type == "zigbee_network_key"


@pytest.mark.parametrize("type_", ["", "WiFi", "wi-fi", "thread network", "_wifi", "wifi_", "1wifi"])
def test_the_type_is_lower_snake_case(type_: str):
    with pytest.raises(ValueError):
        MissingCredentialsError(type_)


def test_the_hub_already_handles_it_as_a_lookup_error():
    assert issubclass(MissingCredentialsError, LookupError)


def test_a_taken_device_id_carries_the_record_holding_it():
    device_id = UUID(int=1)
    error = DeviceIdTakenError(device_id, "Lamp", is_paired=True)
    assert (error.device_id, error.name, error.is_paired) == (device_id, "Lamp", True)
    assert str(error) == f"Device id {device_id} is taken by 'Lamp' (paired)"
