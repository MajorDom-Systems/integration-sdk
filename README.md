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

# majordom-transports

Radios and the services between them and MajorDom integrations. Documentation: [Radios and Transports](https://docs.majordom.io/hub/radios/).

## Install

```sh
pip install majordom-transports
```

Requires Python 3.12+. The services run in Docker, so the host needs Docker and access to the radios' serial ports.

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
