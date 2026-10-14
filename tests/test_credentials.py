"""Pairing credentials: what a discovery expects (CredentialsOption) vs. what a pairing supplies."""

import warnings
from uuid import uuid4

import pytest

from majordom_integration_sdk.schemas.device import (
    CredentialsOption,
    CredentialsType,
    Discovery,
    ProvidedCredentials,
)


def _discovery(options: list) -> Discovery:
    return Discovery(
        id=uuid4(),
        integration="test",
        expected_credentials_options=options,
        transport="test",
        device_manufacturer=None,
        device_name="Bulb",
        device_category=None,
        device_icon=None,
    )


def test_the_mask_is_serialized():
    discovery = _discovery([CredentialsOption(type=CredentialsType.code, mask="DDDD-DDD-DDDD")])
    dumped = discovery.model_dump(mode="json")["expected_credentials_options"]
    assert dumped == [{"type": "code", "mask": "DDDD-DDD-DDDD", "hint": None}]
    assert Discovery.model_validate_json(discovery.model_dump_json()) == discovery


@pytest.mark.parametrize("legacy", [[CredentialsType.code, CredentialsType.qr], ["code", "qr"]])
def test_bare_types_are_accepted_for_stored_discoveries_and_older_integrations(legacy: list):
    discovery = _discovery(legacy)
    assert discovery.expected_credentials_options == [
        CredentialsOption(type=CredentialsType.code),
        CredentialsOption(type=CredentialsType.qr),
    ]


def test_a_stored_legacy_discovery_loads_from_json():
    stored = _discovery([CredentialsOption(type=CredentialsType.secret)]).model_dump(mode="json")
    stored["expected_credentials_options"] = ["secret"]
    loaded = Discovery.model_validate(stored)
    assert loaded.expected_credentials_options == [CredentialsOption(type=CredentialsType.secret)]


def test_with_mask_no_longer_mutates_the_shared_enum_member():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        option = CredentialsType.code.with_mask("DDD-DD-DDD")
    assert option == CredentialsOption(type=CredentialsType.code, mask="DDD-DD-DDD")
    assert not hasattr(CredentialsType.code, "code_mask")
    # the mask now survives the trip to the Hub
    assert _discovery([option]).model_dump(mode="json")["expected_credentials_options"][0]["mask"] == "DDD-DD-DDD"


def test_with_hint_returns_an_option_with_the_hint():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        option = CredentialsType.button.with_hint("Press the button")
    assert option == CredentialsOption(type=CredentialsType.button, hint="Press the button")
    assert not hasattr(CredentialsType.button, "hint")


def test_with_mask_can_carry_a_hint():
    option = CredentialsType.code.with_mask("DDD-DD-DDD", hint="On the box")
    assert option == CredentialsOption(type=CredentialsType.code, mask="DDD-DD-DDD", hint="On the box")
    dumped = _discovery([option]).model_dump(mode="json")["expected_credentials_options"]
    assert dumped == [{"type": "code", "mask": "DDD-DD-DDD", "hint": "On the box"}]


def test_each_call_returns_a_new_option_and_leaves_the_type_untouched():
    assert CredentialsType.code.with_hint("x") is not CredentialsType.code.with_hint("x")
    assert CredentialsType.code.with_mask("DDDD") is not CredentialsType.code.with_mask("DDDD")
    assert CredentialsType.code == "code"


def test_option_is_a_plain_data_model_without_builders():
    option = CredentialsOption(type=CredentialsType.code)
    assert not hasattr(option, "with_mask")
    assert not hasattr(option, "with_hint")


def test_existing_membership_checks_against_bare_types_keep_working():
    # integrations do `credentials.type not in discovery.expected_credentials_options`
    discovery = _discovery([CredentialsOption(type=CredentialsType.code, mask="DDD-DD-DDD")])
    assert CredentialsType.code in discovery.expected_credentials_options
    assert CredentialsType.qr not in discovery.expected_credentials_options


@pytest.mark.parametrize(
    ("credentials", "accepted"),
    [
        (ProvidedCredentials(type=CredentialsType.code, value="123-45-678"), True),
        (ProvidedCredentials(type=CredentialsType.button), True),
        (ProvidedCredentials(type=CredentialsType.secret, value="key"), False),
        (None, False),
    ],
)
def test_accepts_matches_the_provided_type_against_the_options(credentials: ProvidedCredentials | None, accepted: bool):
    discovery = _discovery([CredentialsType.button, CredentialsOption(type=CredentialsType.code, mask="DDD-DD-DDD")])
    assert discovery.accepts(credentials) is accepted


def test_no_credentials_are_accepted_only_where_none_is_an_option():
    assert _discovery([CredentialsType.none]).accepts(None)
    assert _discovery([CredentialsType.none]).accepts(ProvidedCredentials(type=CredentialsType.none))


def test_button_is_a_confirmation_without_a_value():
    credentials = ProvidedCredentials(type=CredentialsType.button)
    assert credentials.value is None
    assert ProvidedCredentials.model_validate({"type": "button"}) == credentials
    assert CredentialsType.button != CredentialsType.none


def test_a_discovery_can_offer_button_or_qr():
    discovery = _discovery([CredentialsType.button, CredentialsType.qr])
    assert [o.type for o in discovery.expected_credentials_options] == [CredentialsType.button, CredentialsType.qr]
