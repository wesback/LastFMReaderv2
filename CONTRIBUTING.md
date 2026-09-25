# Contributing

Thank you for helping improve Last.fm Export. Keep changes focused and
document behavior that affects users or operators.

## Before opening a pull request

Run the complete test suite from the repository root:

```console
make test
```

The deterministic story pipeline runs the same suite directly as
`python -m tests.runner` through `.pipeline-test-command`.

Do not open a pull request until the command passes. Include relevant test
coverage for behavioral changes and describe the user-visible effect in the
pull request.

## Branches and pull requests

- Use a short, descriptive branch name for each change.
- Keep one logical change per pull request.
- Write descriptive commit messages that explain the change.
- Keep pull requests focused and explain the motivation, implementation, and
  validation performed.
- Update documentation when setup, operation, or user-facing behavior changes.

## Repository workflow

Before changing repository automation or working with the label-driven
pipeline, read [AGENTS.md](AGENTS.md). It describes the repository's
automation process and contribution constraints.
