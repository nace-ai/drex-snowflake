# Contributing

Use [GitHub Issues](https://github.com/nace-ai/drex-snowflake/issues) for bugs and proposals. Include the integration version,
runtime version, expected behavior and a small reproduction with synthetic inputs. Never include credentials,
signed image URLs or customer data.

## Local checks

Clone the repository and follow [Development](README.md#development) from the repository root.
The tests use a local mock API and need no Snowflake account; a passing mock test is not a live-platform check. See [.github/workflows](.github/workflows) for the CI commands.

Live tests require separate credentials and may incur API and cloud charges. Do not run them as part of a routine
documentation change. Describe any skipped checks in the pull request.

## Pull requests

Keep the change focused, explain the behavior it changes and list the checks you actually ran. Update the
[API reference](docs/API.md) and examples if an interface changes. Preserve NULL/error semantics, credential
handling and request-cost warnings. Use synthetic fixtures and avoid committing generated bundles or local secrets.

## Releases

A release bumps the version in `pyproject.toml` and `__version__` in `src/drex_snowflake.py` together
(`tests/test_setup_sql.py` checks they agree; the version is sent as `X-Nace-Client: drex-snowflake/<version>`), adds
its entry to [CHANGELOG.md](CHANGELOG.md), updates the release named in the README and in
[docs/INSTALL.md](docs/INSTALL.md#upgrading), and is tagged `vMAJOR.MINOR.PATCH` on `main` with a GitHub release.

## Branding

Follow the [branding guidance](docs/assets/README.md) before changing names or adding artwork.
An official download is not, by itself, permission to use a provider logo. Record sources and applicable usage terms.
