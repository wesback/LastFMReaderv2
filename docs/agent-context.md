# Repository-specific agent context

Use this file for concise, durable facts that help future contributors and
coding agents work in this repository. Keep facts specific, verifiable from
the code, tests, or an explicit maintainer decision. Do not copy run logs,
task summaries, speculative explanations, or credential values here.

## Domain invariants

- Configuration rejects duplicate tracked-user names and unsupported formats or destination schemes in `lastfm_export/config.py`.
- Each tracked user resolves an IANA time zone and may override the shared destination in `lastfm_export/config.py`.
- Retrieval keeps dated scrobbles inside the requested half-open timestamp window and skips undated now-playing rows in `lastfm_export/retrieval.py`.
- Per-user checkpoints, full-resync progress, and execution leases are persisted by `lastfm_export/state.py`.

## Architecture and integration points

- The installed lastfm-export command maps to the lastfm_export.cli:main entry point declared in `pyproject.toml`.
- The page paginator can deliver each fetched page through a callback instead of retaining all rows in `lastfm_export/retrieval.py`.
- Normalized landing rows include UTC and user-local timestamps plus a stable event identifier in `lastfm_export/landing.py`.
- The writer serializes Parquet, CSV, or JSONL to local or fsspec-backed locations in `lastfm_export/output.py`.
- AWS and Azure filesystem extras are optional dependencies declared in `pyproject.toml`.

## Verification and tooling

- The exact repository test command is python -m tests.runner, as configured in `.pipeline-test-command`.
- `tests/runner.py` discovers unittest tests under tests/ and exits with a failing status when the suite fails.
- The project declares Python 3.11 or newer in `pyproject.toml`.
- Documentation behavior is covered in `tests/test_operations_docs.py`.

## Operational workflows

- The installation guide documents core and optional editable package setup in `docs/installation.md`.
- Supply the Last.fm credential through the environment variable named by configuration and let cloud adapters use native credential chains per `docs/installation.md`.
- Cron and Kubernetes CronJobs use a non-zero exporter exit as the scheduler failure signal in `docs/operations.md`.
- The status command reports per-user checkpoint watermarks and staleness in `docs/operations.md`.
- Retain checkpoint state separately when deleting landing files already merged downstream in `docs/operations.md`.
