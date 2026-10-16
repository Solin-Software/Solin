# Contributing to Solin

## Before starting

Use Python 3.13 or newer. On macOS, first follow the
[macOS binding bootstrap](docs/building.md#macos) before installing dependencies.
On Linux, first install the
[native dependencies](docs/building.md#linux-development). Then activate an
isolated environment and install the locked development dependencies:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

The repository does not depend on a specific environment manager or Python
launcher.

## Run from source

Start the application with the installed `solin` command or the local launcher:

```text
python main.py
```

Build the metadata and thumbnail helper after installing Python and native
dependencies, using the same interpreter as the application:

```text
python scripts/build_media_extract.py
```

The script compiles and qualifies `solin-media-extract`, then installs only that
executable beside the installed `pylibobs` runtime. Windows and macOS use
checksum-pinned OBS FFmpeg SDK headers; Linux uses matching Ubuntu development
packages and stages missing native dependencies without replacing wheel libraries.
Rebuild after changing the helper or upgrading native dependencies. Media
extraction requires no `ffmpeg` or `ffprobe` executable. Playback and scene
composition use the supervised libobs process. See
[Building and platform requirements](docs/building.md) for toolchains and packaging.

## Change workflow

1. Create a focused branch from `main`.
2. Keep each change scoped to one concern.
3. Add or update tests for behavior and repository contracts.
4. Run the relevant local quality checks.
5. Open a pull request and describe the behavior, risks, and validation.

Use English [Conventional Commit](https://www.conventionalcommits.org/)
messages, for example `fix: preserve playlist order during import`. Pull
requests are normally squash-merged into one coherent commit on `main`.

Issue forms apply `bug` or `enhancement` according to the selected form.
Questions and general conversations belong in
[Discussions](https://github.com/Solin-Software/Solin/discussions).

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

Media extraction integration fixtures are included in the repository; running
those tests and qualifying the helper requires neither FFmpeg command-line tools
nor a graphics session. CI builds the helper before the Python suites.

Register cleanup when a fixture acquires a resource, before assertions can fail.
Stop and join owned workers before destroying their Qt consumers or restoring
test doubles. Independent finalizers must still run if another cleanup fails.
Destroy GUI objects on the Qt thread and cancel owned timers and callbacks;
flushing deferred deletion does not cancel unrelated queued work.

Wait for observable readiness when preparing native input or asynchronous
layouts. Preserve timing assertions that are part of the behavior contract.
Use pytest's per-invocation temporary directories; concurrent invocations must
not share a fixed `--basetemp`. Passing a rerun does not establish a correction.

Application changes run tests on Windows, Linux, Intel macOS, and Apple Silicon.
Documentation-only changes and verified version-only increases use static checks;
version increases also validate release metadata and the matching release notes.
Release tags always run the complete platform suite.
Portable virtual-camera transport tests run on the Linux reference runner;
Windows builds qualify both DirectShow architectures and gate the Release
frame-adapter benchmark. Lint, locale validation,
release-note validation, and workflow validation run once. Branch protection should require the aggregate
`Quality Gate` result.

Release preparation is a separate pull request. Use an explicit
`python scripts/release.py prepare <intent>` command and review its version and
English notes; do not create or move a release tag from an unreviewed commit.
For betas, leave the generated `en.md` empty to use automatic PR and issue notes
in GitHub and the application. Stable releases still require written, reviewed
notes. See [release preparation](docs/releases.md#preparing-a-release-pull-request).

## Repository hygiene

Do not commit:

- passwords, tokens, private keys, certificates, or local environment files;
- real publication archives or user data;
- generated `build/`, `dist/`, cache, or virtual-environment content;
- locally compiled copies of dependencies already distributed as packages.

Tests that need an archive or user state must construct deterministic synthetic
fixtures containing only the minimum data required by the scenario.

## Licensing

Contributions to Solin are provided under [GPL-3.0-or-later](LICENSE).
Preserve third-party notices and check license compatibility before adding
dependencies or copying code. See the [licensing guide](docs/licensing.md) for
distribution requirements.

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

### Automatic labels

PR titles may use a Conventional Commit prefix to suggest a label. Titles
without a recognized prefix are also accepted. File rules supplement title
rules, so a PR can receive multiple labels:

| Label | Title prefixes | Changed files |
| --- | --- | --- |
| `bug` | `fix:`, `bug:` | None |
| `enhancement` | `feat:`, `enhancement:`, `refactor:`, `perf:` | None |
| `documentation` | `docs:`, `documentation:` | Markdown files |
| `dependencies` | `deps:`, or a `deps` / `deps-dev` scope | Root `requirements*.txt` and `pyproject.toml` manifests |
| `github_actions` | `ci:` | `.github/workflows/` YAML and `.github/actions/` files |
| `skip-changelog` | `chore(release):` | None |
| `translations` | `i18n:`, `l10n:` | Qt `.ts` / `.qm` catalogs and locale JSON under `src/solin/resources/translations/` |

Scopes and breaking-change markers are supported, for example
`fix(player)!: preserve playback`. Matching rules and release category priority
live in [`.github/labels.json`](.github/labels.json). File labels indicate an
affected area: touching `pyproject.toml` suggests `dependencies` even when the
edit concerns other project metadata. The `enhancement` label covers new features,
architectural improvements, refactoring, and performance improvements.

Labels are recalculated when a PR opens, reopens, changes its title or description,
or receives commits. Only obsolete labels previously applied by this automation
are removed. Maintainers can add or remove any label in GitHub. Once a label is
explicitly changed by a person or an integration using another identity, this automation leaves
that label under manual control for the rest of that PR, including future title
and file changes. Other labels are unaffected. Missing catalog labels are
created when first needed; existing label colors and descriptions are preserved.

The labeling workflow reads titles, file paths, and label history through the
GitHub API and runs only the trusted base revision, including for PRs from forks.

The generated release appendix groups PRs by their current labels. Each PR is
listed once; see [contributor credits](docs/releases.md#contributor-credits) for
category priority and the fallback for unclassified changes.

The workflow assigns `skip-changelog` to `chore(release):` PRs automatically.
Other `chore:` titles and other types with a `release` scope do not trigger it.
You can also apply it manually to administrative PRs that would add noise to
the changelog. Manual additions and removals take precedence, as with other labels.
The label omits a PR and its related issue links from this appendix, regardless
of its other labels. Removing it restores the entry on the next generation.
It does not skip builds, validation, version preparation, or publication,
and does not edit the reviewed release notes.
