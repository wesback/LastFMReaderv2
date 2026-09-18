# Last.fm Export

Last.fm Export is a Python command-line application that retrieves a user's
Last.fm scrobble history and exports it to local or cloud-backed destinations
for downstream processing. It supports incremental and full-resync workflows,
checkpointing, and configurable output formats.

## Setup

The project requires Python 3.11 or newer. From the repository root, create a
virtual environment, activate it, and install the package in editable mode:

```console
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

For S3 or Azure destinations, install the corresponding optional dependency
extra:

```console
python3 -m pip install -e '.[aws]'
python3 -m pip install -e '.[azure]'
```

Copy `docs/config.example.toml` to a deployment-owned location and configure
the exporter before running it:

```console
lastfm-export --config /path/to/config.toml
```

See [the installation guide](docs/installation.md) for credential handling
and unattended operation details.

## Running tests

Run the test suite from the repository root with:

```console
make test
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local test requirements, branch and
pull request expectations, and the contributor workflow.
