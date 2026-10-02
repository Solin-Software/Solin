# Contributing to Solin

## Before starting

Use Python 3.13 or newer. On Intel macOS, first follow the
[macOS binding bootstrap](docs/building.md#macos) before installing dependencies.
On other platforms, install the locked development dependencies:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

An isolated environment is recommended, but the repository does not depend on a
specific environment manager or Python launcher.

## Change workflow

1. Create a focused branch from `main`.
2. Keep each change scoped to one concern.
3. Add or update tests for behavior and repository contracts.
4. Run the relevant local quality checks.
5. Open a pull request and describe the behavior, risks, and validation.

Use English [Conventional Commit](https://www.conventionalcommits.org/)
messages, for example `fix: preserve playlist order during import`. Pull
requests are normally squash-merged into one coherent commit on `main`.

## Validation

The complete local validation is:

```text
python -m ruff check .
python scripts/typecheck.py
python -m pytest
python scripts/check_locales.py
```

Run focused tests while developing, then run the complete applicable suite
before requesting review. Packaged-app and installer tests are run when their
platform or delivery contract changes.

## Repository hygiene

Do not commit:

- passwords, tokens, private keys, certificates, or local environment files;
- real publication archives or user data;
- generated `build/`, `dist/`, cache, or virtual-environment content;
- locally compiled copies of dependencies already distributed as packages.

Tests that need an archive or user state must construct deterministic synthetic
fixtures containing only the minimum data required by the scenario.

## Dependencies

`pyproject.toml` is the source for direct runtime and optional dependencies.
`requirements.txt` locks the complete delivery environment, while
`requirements-dev.txt` extends it with repository tooling.

When changing a direct dependency, update the lock in the same pull request.
The dependency contract tests verify that every direct runtime requirement is
present with the same version and platform marker. Validate packaging-sensitive
updates on every affected operating system.

## Pull requests

Explain:

- what changed and why;
- any user-visible or persisted-data effect;
- platforms and integrations affected;
- tests and manual checks performed;
- follow-up work intentionally left out of scope.

Keep unrelated cleanup in separate pull requests. Refactors should preserve
behavior unless the pull request explicitly documents a contract change.
