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
