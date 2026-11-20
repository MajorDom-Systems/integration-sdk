"""The firmware catalog: which radio boards exist and which firmware builds each can run, as the firmware repo publishes
them on its signed channel (``boards.json``, ``stable.json``, ``dev.json``). A snapshot of the channel ships with the
SDK (``firmware/``) and works offline; `Catalog.refresh` replaces it with the channel's when that verifies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Regenerate: `python -m majordom_integration_sdk.runtime.peripherals.hardware.catalog`
SNAPSHOT = Path(__file__).with_name("firmware")
CHANNEL_URL = "https://firmware.majordom.io/v1/"
CATALOG_ENV = "MAJORDOM_FIRMWARE_CATALOG"  # a folder with the channel's files, in place of the snapshot
CHANNELS = ("stable", "dev")

#: What the host runs against a package of each kind. TODO(firmware): read the packages' ``[runtime]`` once the channel
#: carries it; its entries don't today.
KINDS = {"zigbee": ("silabs", "zigbee-ncp"), "thread": ("silabs", "thread-rcp"), "multipan": ("silabs", "multipan-rcp")}

type Json = Mapping[str, Any]


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Build:
    package: str
    version: str
    url: str
    sha256: str
    params: Mapping[str, Any]  # the firmware's serial settings: ``baud``, ``flow_control``


class Catalog:
    """One shared by the drivers and the scanner sees what a refresh brings."""

    def __init__(self, boards: Json, channels: Mapping[str, Json]):
        self.boards, self.channels = boards, channels

    @classmethod
    def read(cls, folder: Path) -> Catalog:
        boards, *channels = (json.loads((folder / f"{name}.json").read_bytes()) for name in ("boards", *CHANNELS))
        return cls(boards["boards"], dict(zip(CHANNELS, channels, strict=True)))

    @classmethod
    def default(cls) -> Catalog:
        """The one ``MAJORDOM_FIRMWARE_CATALOG`` points at, else the snapshot."""
        return cls.read(Path(os.environ.get(CATALOG_ENV) or SNAPSHOT))

    async def refresh(self, url: str = CHANNEL_URL, public_key: str | None = None) -> bool:
        """Take the channel's catalog at ``url`` if its manifests are signed by ``public_key`` (minisign, the bundled
        one by default) and not expired; otherwise keep this one."""
        try:
            key = public_key or (SNAPSHOT / "minisign.pub").read_text()
            files = await asyncio.to_thread(_download, url)
            for channel in CHANNELS:
                _verify(key, files[f"{channel}.json"], files[f"{channel}.json.minisig"].decode())
            fresh = {channel: json.loads(files[f"{channel}.json"]) for channel in CHANNELS}
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            boards_sha256 = hashlib.sha256(files["boards.json"]).hexdigest()
            for channel, doc in fresh.items():
                if doc["expires_at"] < now or doc["boards_sha256"] != boards_sha256:
                    raise CatalogError(f"The {channel} manifest is expired or names other boards")
        except Exception as error:
            log.info(f"Keeping the firmware catalog at hand, the channel's isn't usable: {error!r}")
            return False
        self.boards, self.channels = json.loads(files["boards.json"])["boards"], fresh
        return True

    def usb_ids(self) -> set[tuple[str, str]]:
        return {(rule["vid"], rule["pid"]) for board in self.boards.values() for rule in board["match"].get("usb", [])}

    def usb_board(self, path: str) -> str | None:
        """The board of the stick at ``path`` (``/dev/serial/by-id/usb-<vendor>_<product>_<serial>-if00``), by its
        product name."""
        name = Path(path).name
        return next(
            (
                key
                for key, board in self.boards.items()
                for rule in board["match"].get("usb", [])
                if any(product.replace(" ", "_") in name for product in rule.get("product", []))
            ),
            None,
        )

    def builds(self, board: str, channel: str, slot: str | None = None) -> dict[str, Build]:
        """The newest build of each firmware for ``board`` on ``channel`` (dev lists the stable builds too)."""
        found = {}
        for kinds in self.channels[channel]["firmware"].values():
            for kind, items in kinds.items():
                newest = next((i for i in items if i["board"] == board and slot in (None, i["slot"])), None)
                if newest and kind in KINDS:
                    serial, gbl = newest["serial"], newest["files"]["gbl"]
                    params = {"baud": serial["baud"], "flow_control": serial["flow"] != "none"}
                    found[KINDS[kind][1]] = Build(
                        newest["package"], newest["version"], gbl["url"], gbl["sha256"], params
                    )
        return {firmware: found[firmware] for _, firmware in KINDS.values() if firmware in found}

    def profile(self, board_id: str) -> Json:
        """The ``board.json`` of the host-wired board ``board_id`` (``hub1-cm4``, or a revision: ``hub1-cm4/r1``), as
        the firmware repo generates it: each radio wired to the host with the firmware of its newest build."""
        keys = sorted(k for k in self.boards if k.split("/", 1)[1] == board_id or k.split("/")[1] == board_id)
        if not keys or not any("pins" in slot for slot in self.boards[keys[-1]]["slots"].values()):
            raise CatalogError(f"No host-wired board {board_id} in the firmware catalog")
        key, radios = keys[-1], []
        for name, slot in self.boards[keys[-1]]["slots"].items():
            if "pins" not in slot:
                continue
            radio: dict[str, Any] = {"name": name, "path": slot["pins"]["uart"]}
            builds = self.builds(key, "stable", name) or self.builds(key, "dev", name)
            if firmware := next(iter(builds), None):
                radio |= {"driver": "silabs", "firmware": firmware}
                params: dict[str, Any] = dict(builds[firmware].params)
            elif stock := slot.get("stock"):
                radio["driver"], params = stock["driver"], {"baud": stock["baud"]}
            else:
                raise CatalogError(f"{key}/{name}: no build and no stock firmware")
            if reset := slot.get("reset"):
                params["reset_gpio"] = reset["gpio"]
            radios.append(radio | {"params": params})
        revision = key.split("/")[2].removeprefix("r") if key.count("/") == 2 else None
        return {"schema": 1, "board": key.split("/")[1], "revision": revision and int(revision), "radios": radios}


def _download(url: str) -> dict[str, bytes]:
    names = ["boards.json", *(f"{c}.json{s}" for c in CHANNELS for s in ("", ".minisig"))]
    return {name: _fetch(f"{url.rstrip('/')}/{name}") for name in names}


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def _verify(public_key: str, data: bytes, signature: str):
    """Check a minisign signature: Ed25519 over the BLAKE2b-512 of ``data`` (``ED``) or over ``data`` itself (legacy
    ``Ed``), and the global signature over that signature and its trusted comment."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = base64.b64decode(public_key.strip().splitlines()[-1])
    _, sig_line, comment_line, global_line = signature.strip().splitlines()
    sig = base64.b64decode(sig_line)
    if len(key) != 42 or key[:2] != b"Ed" or len(sig) != 74 or sig[2:10] != key[2:10]:
        raise CatalogError("The signature isn't by the catalog's key")
    if sig[:2] == b"ED":
        digest = hashes.Hash(hashes.BLAKE2b(64))
        digest.update(data)
        data = digest.finalize()
    elif sig[:2] != b"Ed":
        raise CatalogError("Unknown minisign signature algorithm")
    comment = comment_line.removeprefix("trusted comment: ").encode()
    verifier = Ed25519PublicKey.from_public_bytes(key[10:])
    try:
        verifier.verify(sig[10:], data)
        verifier.verify(base64.b64decode(global_line), sig[10:] + comment)
    except InvalidSignature as error:
        raise CatalogError("Bad signature") from error


if __name__ == "__main__":  # refresh the snapshot from the live channel, verified
    catalog = Catalog.read(SNAPSHOT)
    if not asyncio.run(catalog.refresh()):
        raise SystemExit("The channel's catalog didn't verify: the snapshot is unchanged")
    (SNAPSHOT / "boards.json").write_text(
        json.dumps({"schema": 1, "boards": catalog.boards}, indent=2, sort_keys=True) + "\n"
    )
    for name, doc in catalog.channels.items():
        (SNAPSHOT / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n")
