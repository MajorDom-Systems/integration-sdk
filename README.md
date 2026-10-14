<!-- MajorDom Project Banner -->
<a href="https://majordom.io" target="_blank">
  <picture>
    <source
      media="(prefers-color-scheme: dark)"
      srcset="https://markparker.me/banners/majordom-dark.webp"
    />
    <img
      alt="Part of MajorDom — the next-gen smart home"
      src="https://markparker.me/banners/majordom-light.webp"
    />
  </picture>
</a>

# majordom-integration-sdk

Models, protocols, and tooling for building [MajorDom](https://majordom.io) integrations.

An integration bridges an external protocol or platform (Matter, Zigbee, HomeKit, a vendor
cloud, …) into the MajorDom language. This SDK is the contract: the schemas, the
`AbstractController` framework, the device-repository protocol, discovery services, and a
standalone dev runner so you can build and test an integration **without a running Hub**.
The Hub itself depends on this same package — first-party integrations use no private
shortcut.

## Install

```sh
pip install majordom-integration-sdk
```

Requires Python 3.12+.

## Also a standalone IoT toolkit

You don't need MajorDom to get value from this package. `majordom_integration_sdk.discovery`
ships three fully working, standardized device-discovery services you can drop into **any**
async Python project:

- **Zeroconf / mDNS** (`ZeroconfDiscoveryService`) — browse Bonjour/mDNS services with a
  clean listener protocol, and — unusually — **reliable disappearance tracking**: it augments
  raw zeroconf with an active cache-poll + confirm-evict loop so you get accurate "device
  went away" events, not just arrivals.
- **SSDP / UPnP** (`SSDPDiscoveryService`) — flexible, per-listener **configurable** search
  targets and refresh behavior over raw sockets, without dragging in a full UPnP stack.
- **BLE** (`BLEDiscoveryService`) — a uniform listener interface over Bleak for advertisement
  scanning.

All three share one `register(listener, …) → cancel` shape, run standalone (each module has a
runnable `__main__` demo), and are a solid reference for implementing **standardized
interfaces over messy IoT protocols** even if you never touch MajorDom. The
`AbstractController` framework and the `DeviceRepositoryProtocol` + in-memory/SQLite
implementations are equally reusable as a small, typed device-modelling toolkit.

## Documentation

Full integration-author docs — models, the controller lifecycle, storing data, discovery,
and a worked example — live at **[docs.majordom.io](https://docs.majordom.io/device-integration)**.

The fastest way to start a new integration is the
[integration-template](https://github.com/MajorDom-Systems/integration-template) repository
(**Use this template → Create a new repository**).

### Pairing credentials

A `Discovery` lists every way the device can be paired as `CredentialsOption`s; the Hub asks the
user for one and passes it back to `pair_device` as `ProvidedCredentials`.

```python
Discovery(
    ...,
    expected_credentials_options=[
        CredentialsType.code.with_mask("DDDD-DDD-DDDD").with_hint("On the label"),  # D = digit
        CredentialsType.button.with_hint("Press the button on the device"),
        CredentialsType.qr,  # a bare type is an option without mask/hint
    ],
)
# serialized: [{"type": "code", "mask": "DDDD-DDD-DDDD", "hint": "On the label"}, ...]
```

`with_mask()` and `with_hint()` are the short form of `CredentialsOption(type=..., mask=..., hint=...)`:
they chain in any order and return a new option, leaving the type and earlier options untouched.

| Type     | The user...                                           | `ProvidedCredentials.value` |
|----------|-------------------------------------------------------|-----------------------------|
| `code`   | types a setup code, formatted by `mask`               | the code                    |
| `qr`     | scans a setup QR code                                 | the raw QR payload          |
| `secret` | enters a key (e.g. an ESPHome API encryption key)     | the key                     |
| `button` | physically confirms on the device (Improv, a setup button) | none: a confirmation step |
| `none`   | does nothing; pairing needs no input                  | none                        |

In `pair_device`, check the supplied type with `discovery.accepts(credentials)` (no credentials
count as `none`).

### Missing Hub credentials

When the Hub itself lacks something a pairing needs (a Hub on Ethernet has no Wi-Fi to give a
node set up over BLE), raise `MissingCredentialsError(type)` from `pair_device` before touching the
device, and set the discovery's `last_error` to `str(error)`. The app asks the user for them, the Hub
saves them, and the user pairs again.

```python
error = MissingCredentialsError(MissingCredentialsError.WIFI)  # "wifi" is the default
str(error)                                                     # "Missing credentials: wifi"
MissingCredentialsError.parse(device.last_error).type          # "wifi"; parse() is None for other errors
```

`type` is a lower_snake_case string, so an integration can add its own; the app maps it to an enum
to pick the screen to show.

| `type`   | The Hub lacks...                                   |
|----------|----------------------------------------------------|
| `wifi`   | a Wi-Fi network (SSID and password) to give a device |
| `thread` | a Thread network (dataset) to give a device        |

An integration that raises any other type documents it in its own README.

## License

See [LICENSE](LICENSE). For commercial licensing or partnership inquiries, contact us via
[parker-industries.org/partnership](https://parker-industries.org/partnership).

---

## Development

Setup:

```sh
poetry install && poetry run poe install
```

This installs dependencies and the pre-commit hook that runs `poe check`.

| Task | Description |
|------|-------------|
| `poe install` | Install deps and register the pre-commit hook |
| `poe check` | Full quality pipeline (ruff, ty, pytest, poetry build/check) |
| `poe check --ci` | Same, plus `git diff --exit-code` |

Two long-lived branches: `develop` (integration branch, all work lands here) and `master`
(protected, released). Releases are cut from `develop` via **Actions → Release**; see
[ParkerIndustries/workflows](https://github.com/ParkerIndustries/workflows) for the CI/CD.
