import base64
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from majordom_integration_sdk.peripherals import Endpoint
from majordom_integration_sdk.runtime import OS_BOARD_PROFILE, Catalog, FixedRadio, Silabs, ZWave, board_profile
from majordom_integration_sdk.runtime.peripherals.hardware import catalog as module
from majordom_integration_sdk.runtime.peripherals.hardware.catalog import CatalogError

SKYCONNECT = "/dev/serial/by-id/usb-Nabu_Casa_SkyConnect_v1.0_0-if00-port0"
ZBT1 = "/dev/serial/by-id/usb-Nabu_Casa_Home_Assistant_Connect_ZBT-1_9e2adbd75b8beb119fe564a0f320645d-if00-port0"
DONGLE_P = "/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_0001-if00-port0"
NABUCASA = "https://github.com/NabuCasa/silabs-firmware-builder/releases/download/v2026.02.23"

#: What the SDK hardcoded before the catalog: the USB ids it opened unasked.
OLD_RADIO_USB_IDS = {
    ("10c4", "ea60"),
    ("10c4", "8a2a"),
    ("1a86", "55d4"),
    ("0658", "0200"),
    ("1cf1", "0030"),
    ("0403", "6015"),
    ("0451", "16a8"),
}

PROTO = {
    "host": "cm4",
    "match": {"board_json": "proto-hub1-cm4-devkits"},
    "name": "proto",
    "slots": {
        "mg26": {
            "label": "MGM260P (EFR32MG26)",
            "pins": {"uart": "/dev/ttyAMA3"},
            "reset": {"gpio": "gpiochip0:23"},
            "firmware": ["mg26-multipan-pa04-pa05"],
            "default": "mg26-multipan-pa04-pa05",
        },
        "zwave": {"label": "ZGM230S", "pins": {"uart": "/dev/ttyAMA4"}, "stock": {"driver": "zwave", "baud": 115200}},
    },
}
MULTIPAN = {
    "package": "mg26-multipan-pa04-pa05",
    "version": "1.0.0",
    "protocol": "silabs-cpc",
    "serial": {"baud": 115200, "flow": "none"},
    "files": {"gbl": {"url": "https://example/mp.gbl", "sha256": "a" * 64, "size": 1}},
}


# Snapshot and parity with the hardcoded data it replaced


def test_the_snapshot_knows_every_radio_usb_id_the_sdk_did():
    assert Catalog.default().usb_ids() == OLD_RADIO_USB_IDS


def test_a_skyconnect_is_offered_the_mirrored_nabucasa_builds_on_the_dev_channel_only():
    silabs = Silabs()
    assert silabs.flashable(SKYCONNECT, "stable") == []
    assert silabs.flashable(SKYCONNECT, "dev") == ["zigbee-ncp", "thread-rcp"]
    assert silabs.flashable(ZBT1, "dev") == ["zigbee-ncp", "thread-rcp"]
    builds = silabs.catalog.builds("nabucasa/skyconnect", "dev")
    zigbee, thread = builds["skyconnect-zigbee"], builds["skyconnect-thread"]
    assert (zigbee.version, zigbee.protocol, thread.version, thread.protocol) == ("7.5.1", "ezsp", "2.7.2", "spinel")
    assert zigbee.url == f"{NABUCASA}/skyconnect_zigbee_ncp_7.5.1.0.gbl"
    assert zigbee.sha256 == "5b9221fc1decb687cfe822de119443db06728a53a7ce465c7a14ef73c8e146ad"
    assert thread.sha256 == "15c70a4a235ccb8a086a29cb9f073d1f9346bf2ea0bca34fb6239ab9c9fe4ca2"


def test_drivers_take_the_builds_of_the_protocols_they_handle():
    assert Silabs().protocols == {"ezsp": "zigbee-ncp", "spinel": "thread-rcp", "silabs-cpc": "multipan-rcp"}
    assert ZWave().protocols == {"zwave-serial-api": "zwave-serial-api"}


def test_an_identify_only_or_unknown_stick_is_offered_nothing():
    assert Silabs().catalog.usb_board(DONGLE_P) == "sonoff/zbdongle-p"
    assert Silabs().flashable(DONGLE_P, "dev") == []
    assert Silabs().flashable("/dev/serial/by-id/usb-1a86_ZBDongle-E-if00", "dev") == []


def test_a_firmwares_serial_settings_are_its_boards_build_ones_or_the_drivers():
    silabs = Silabs()
    assert silabs.serves("zigbee-ncp", SKYCONNECT) == [
        Endpoint("zigbee", "ezsp", SKYCONNECT, params={"baud": 115200, "flow_control": True})
    ]
    assert silabs.serves("thread-rcp", SKYCONNECT)[0].params == {"baud": 460800, "flow_control": True}
    assert silabs.serves("multipan-rcp", "/dev/ttyAMA3", {"baud": 115200, "flow_control": False})[0].params == {
        "baud": 115200,
        "flow_control": False,
    }


async def test_flashing_downloads_the_catalogs_build_and_checks_it(tmp_path, monkeypatch):
    from majordom_integration_sdk.runtime.peripherals.hardware import drivers

    data, ran = b"gbl", []
    build = {"url": "https://example/sky.gbl", "sha256": hashlib.sha256(data).hexdigest(), "size": 3}
    boards = json.loads(json.dumps(Catalog.default().boards))
    boards["nabucasa/skyconnect"]["slots"]["radio"]["firmware"].append(MULTIPAN["package"])
    doc = _channel("dev", [{**MULTIPAN, "files": {"gbl": build}}])
    catalog = Catalog(boards, {"stable": _channel("stable", []), "dev": doc})

    class Response:
        def read(self) -> bytes:
            return data

    async def flasher(path: str, *args: str) -> str:
        ran.append(args)
        return ""

    monkeypatch.setattr(drivers.urllib.request, "urlopen", lambda url, timeout: Response())
    monkeypatch.setattr(drivers, "_flasher", flasher)
    await Silabs(catalog).flash(SKYCONNECT, "multipan-rcp", "dev", tmp_path)
    assert ran == [("flash", "--firmware", str(tmp_path / "sky.gbl"))]
    with pytest.raises(ValueError, match="No multipan-rcp"):
        await Silabs(catalog).flash(SKYCONNECT, "multipan-rcp", "stable", tmp_path)


# Overrides


def test_the_env_points_at_a_local_catalog(tmp_path, monkeypatch):
    shutil.copytree(module.SNAPSHOT, tmp_path, dirs_exist_ok=True)
    boards = json.loads((tmp_path / "boards.json").read_text())
    boards["boards"]["majordom/proto-hub1-cm4-devkits/r1"] = PROTO
    (tmp_path / "boards.json").write_text(json.dumps(boards))
    monkeypatch.setenv(module.CATALOG_ENV, str(tmp_path))
    assert "majordom/proto-hub1-cm4-devkits/r1" in Catalog.default().boards
    monkeypatch.delenv(module.CATALOG_ENV)
    assert "majordom/proto-hub1-cm4-devkits/r1" not in Catalog.default().boards


# Refresh from the signed channel


@pytest.fixture
def channel(monkeypatch) -> dict:
    """The channel served: its files and the public key signing them."""
    key, key_id = Ed25519PrivateKey.generate(), os.urandom(8)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    boards = json.dumps({"schema": 1, "boards": {**Catalog.default().boards, "x/new": PROTO}}).encode()
    files: dict = {"boards.json": boards}
    for name in ("stable", "dev"):
        doc = json.dumps(_channel(name, [], hashlib.sha256(boards).hexdigest())).encode()
        files |= {f"{name}.json": doc, f"{name}.json.minisig": _sign(key, key_id, doc).encode()}
    served = {
        "files": files,
        "key": f"untrusted comment: test\n{_b64(b'Ed' + key_id + public)}\n",
        "sign": lambda data: _sign(key, key_id, data).encode(),
    }
    monkeypatch.setattr(module, "_fetch", lambda url: files[url.rsplit("/", 1)[1]])
    return served


async def test_a_signed_channel_replaces_the_catalog(channel):
    catalog = Catalog.default()
    assert await catalog.refresh("https://example/v1", channel["key"])
    assert "x/new" in catalog.boards


@pytest.mark.parametrize("tamper", ["signature", "key", "expired", "boards", "offline"])
async def test_a_channel_that_doesnt_verify_leaves_the_catalog_as_it_was(channel, monkeypatch, tamper):
    files = channel["files"]
    match tamper:
        case "signature":
            files["dev.json"] = files["dev.json"].replace(b'"dev"', b'"Dev"')
        case "key":
            channel["key"] = f"untrusted comment: other\n{_b64(b'Ed' + os.urandom(40))}\n"
        case "expired":
            boards_sha256 = hashlib.sha256(files["boards.json"]).hexdigest()
            doc = json.dumps(_channel("stable", [], boards_sha256, datetime.now(UTC) - timedelta(days=1))).encode()
            files |= {"stable.json": doc, "stable.json.minisig": channel["sign"](doc)}
        case "boards":
            files["boards.json"] += b" "
        case "offline":
            monkeypatch.setattr(module, "_fetch", lambda url: (_ for _ in ()).throw(OSError("down")))
    catalog = Catalog.default()
    assert not await catalog.refresh("https://example/v1", channel["key"])
    assert catalog.boards == Catalog.default().boards


async def test_the_bundled_key_is_still_a_placeholder_so_the_snapshot_stays(channel):
    assert not await Catalog.default().refresh("https://example/v1")


def test_the_minisign_verifier_rejects_a_bad_global_signature():
    key, key_id = Ed25519PrivateKey.generate(), b"12345678"
    public = f"c\n{_b64(b'Ed' + key_id + key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}"
    signature = _sign(key, key_id, b"data")
    module._verify(public, b"data", signature)
    with pytest.raises(CatalogError):
        module._verify(public, b"data", signature.replace("trusted comment: t", "trusted comment: u"))


# Board profiles


def test_no_profile_by_default_diy_and_library_mode():
    assert board_profile(None) == []
    assert Path("/etc/majordom/board.json") == OS_BOARD_PROFILE


def test_the_os_images_profile_in_the_firmware_repos_format(tmp_path):
    profile = tmp_path / "board.json"
    profile.write_text(
        json.dumps(
            {
                "schema": 1,
                "board": "proto-hub1-cm4-devkits",
                "revision": 1,
                "slots": [
                    {
                        "name": "mg26",
                        "path": "/dev/ttyAMA3",
                        "driver": "silabs",
                        "firmware": "multipan-rcp",
                        "params": {"baud": 115200, "flow_control": False, "reset_gpio": "gpiochip0:23"},
                    },
                    {"name": "zwave", "path": "/dev/ttyAMA4", "driver": "zwave", "params": {"baud": 115200}},
                ],
            }
        )
    )
    assert board_profile(profile) == PROTO_RADIOS


def test_the_hubs_older_bare_list_profile(tmp_path):
    profile = tmp_path / "board.json"
    profile.write_text(json.dumps([{"id": "zwave", "path": "/dev/ttyAMA4", "driver": "zwave"}]))
    assert board_profile(profile) == [FixedRadio("zwave", "/dev/ttyAMA4", "zwave")]


def test_a_catalog_board_id_builds_its_profile():
    boards = {**Catalog.default().boards, "majordom/proto-hub1-cm4-devkits/r1": PROTO}
    catalog = Catalog(boards, {"stable": _channel("stable", []), "dev": _channel("dev", [MULTIPAN])})
    assert board_profile("proto-hub1-cm4-devkits", catalog) == PROTO_RADIOS
    profile = catalog.profile("proto-hub1-cm4-devkits/r1", [Silabs(catalog)])
    assert (profile["revision"], [slot["name"] for slot in profile["slots"]]) == (1, ["mg26", "zwave"])
    with pytest.raises(CatalogError, match="No host-wired board"):
        board_profile("skyconnect", catalog)


PROTO_RADIOS = [
    FixedRadio(
        "mg26",
        "/dev/ttyAMA3",
        "silabs",
        "multipan-rcp",
        {"baud": 115200, "flow_control": False, "reset_gpio": "gpiochip0:23"},
    ),
    FixedRadio("zwave", "/dev/ttyAMA4", "zwave", None, {"baud": 115200}),
]


def _channel(name: str, items: list[dict], boards_sha256: str = "", expires: datetime | None = None) -> dict:
    """A manifest as the firmware repo's manifest_gen writes it."""
    expires = expires or datetime.now(UTC) + timedelta(days=90)
    firmware: dict = {}
    for item in items:
        firmware.setdefault(item["package"], []).append(item)
    return {
        "schema": 1,
        "channel": name,
        "issued_at": "2026-11-20T00:00:00Z",
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "boards_sha256": boards_sha256,
        "policy": {"downgrade": name == "dev"},
        "firmware": firmware,
    }


def _sign(key: Ed25519PrivateKey, key_id: bytes, data: bytes, comment: str = "t") -> str:
    """A minisign signature (prehashed, ``ED``)."""
    digest = hashes.Hash(hashes.BLAKE2b(64))
    digest.update(data)
    signature = key.sign(digest.finalize())
    trusted = key.sign(signature + comment.encode())
    return f"untrusted comment: test\n{_b64(b'ED' + key_id + signature)}\ntrusted comment: {comment}\n{_b64(trusted)}\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_identify_only_sticks_of_shared_usb_ids_are_told_apart_by_product():
    catalog = Catalog.default()
    prefix = "/dev/serial/by-id/usb-"
    assert catalog.usb_board(f"{prefix}ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_1-if00") == "sonoff/zbdongle-e"
    assert catalog.usb_board(DONGLE_P) == "sonoff/zbdongle-p"
    assert catalog.usb_board(f"{prefix}dresden_elektronik_ConBee_III_DE1-if00-port0") == "dresden/conbee-iii"
    assert catalog.usb_board(f"{prefix}Silicon_Labs_HubZ_Smart_Home_Controller_C0F0-if01-port0") == "nortek/husbzb-1"
    assert catalog.usb_board(f"{prefix}Silicon_Labs_Zooz_ZST10\u00a0700_Z-Wave_Stick_0001-if00-port0") == "zooz/zst10"
