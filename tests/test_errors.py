"""MissingCredentialsError: what the Hub lacks to pair a device, readable back from a last_error."""

import pytest

from majordom_integration_sdk import MissingCredentialsError


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


@pytest.mark.parametrize("type_", ["wifi", "thread", "zigbee_network_key"])
def test_the_type_is_read_back_from_a_last_error(type_: str):
    parsed = MissingCredentialsError.parse(str(MissingCredentialsError(type_)))
    assert parsed is not None
    assert parsed.type == type_


@pytest.mark.parametrize("last_error", [None, "", "Device is no longer connected", "Missing credentials: Wi-Fi"])
def test_other_errors_are_not_read_as_missing_credentials(last_error: str | None):
    assert MissingCredentialsError.parse(last_error) is None
