# Versioning and releases

## CalVer contract

The public version is `YY.RELEASE.PATCH`, optionally followed by the Python
pre-release suffix `bN`. Examples are `26.32.0`, `26.32.1`, and `26.33.0b2`.
Tags and GitHub Releases use the corresponding display form without a `v`
prefix: `26.32.0` and `26.33.0-beta.2`.

- `YY` is the two-digit release year.
- `RELEASE` is the sequential release line in that year. It starts at one in a
  new year.
- `PATCH` starts at zero and increases for corrective releases on that line.
- `bN` identifies a beta. A stable release has no suffix.

`src/solin/version.py` is the single source of truth. Native package metadata
is derived from it because Windows and macOS impose different numeric formats:

- Windows uses `YY.RELEASE.PATCH.BUILD`, where beta `N` uses `BUILD=N` and the
  stable build uses `65535`.
- macOS uses `YY.RELEASE.PATCH` as its short version and a numeric build of
  `YY.RELEASE.(PATCH*65536+BUILD)`.

These projections keep successive betas ordered before their stable release
without exposing a fourth public component. The GitHub updater accepts only
this canonical three-component public version and its optional beta suffix.

## Preparing a release pull request

Release preparation runs from a clean checkout of `main` with one explicit
intent:

```text
python scripts/release.py prepare release
python scripts/release.py prepare patch
python scripts/release.py prepare beta
python scripts/release.py prepare promote
```

The command calculates the next version, rejects existing local or remote tags,
updates the canonical version, and creates the English release-note draft. It
does not create a tag or publish anything. The generated notes and version
change are reviewed together in a focused pull request, which also receives the
normal quality checks.

Release notes live under `docs/release-notes/<tag>/<locale>.md`. `en.md` is
required before tagging. Other files use an existing application locale such as
`pt_BR.md`; the updater falls back to English when a translation is absent.
Stable notes consolidate the user-visible changes that were exercised in beta.

Conventional Commits and pull-request labels can organize the draft, but they do
not choose a CalVer increment. The release intent remains an explicit reviewed
decision.

## Publishing

Publication starts when the exact reviewed tag is pushed on the merged release
commit. Tags matching the release contract start `.github/workflows/release.yml`,
and the workflow accepts only tag commits contained in `main`.

The release workflow validates the identity and notes, runs the cross-platform
quality gate, then builds and tests all required packages:

- Windows x86-64 setup, which also installs and registers the virtual camera;
- ad-hoc signed macOS 13+ DMGs for Intel and Apple Silicon;
- Linux x86-64 AppImage requiring glibc 2.38 or newer.

The publisher runs only after every required job succeeds. It verifies the exact
asset inventory, sizes, SHA-256 hashes, source commit, architecture, and release
channel. Files are first uploaded to a draft and the draft is published only
after GitHub reports every expected asset as complete with the expected digest.
A beta is marked as a prerelease and never becomes `latest`.

Published assets are never replaced. A failed run may safely complete its owned
draft; a correction to a published release requires a new version. Repository
release immutability is an external prerequisite for production publication;
it locks each published tag and its assets after the workflow publishes the
draft.

The platform workflows remain manually runnable for diagnostics. Manual output
is short lived and is not distribution.

## Qualifying a branch before tagging

Run the same platform build workflows on a pushed working branch before preparing
another beta. These runs build the full packages and exercise the packaged runtime
and verified-predecessor upgrade tests, but do not invoke the release publisher:

```text
gh workflow run quality.yml --ref <branch>
gh workflow run build-solin-windows.yml --ref <branch> -f run_release_smoke_tests=true
gh workflow run build-solin-linux.yml --ref <branch> -f run_packaged_smoke_test=true -f run_release_smoke_tests=true
gh workflow run build-solin-macos.yml --ref <branch> -f run_release_smoke_tests=true
gh run list --branch <branch>
```

Inspect failed job logs, fix the cause on that branch, push, and dispatch the
affected workflow again. Qualify all platforms and Quality on the final commit.
Download successful packages from the run's artifacts for additional testing.
Only after qualification should the normal review, release preparation, and tag
publication flow proceed. Diagnostic artifacts are not published releases and
do not require a version bump or tag.

## External repository prerequisites

Production publication depends on repository controls that cannot be declared
by a workflow file. The expected state is:

- release immutability enabled;
- `main` protected by the required `Quality Gate` check;
- release tags protected from deletion and force updates.

The first GitHub-hosted release also depends on repository variables containing
the externally distributed predecessor package and its verified digest:

- `SOLIN_TRANSITION_WINDOWS_URL` and `SOLIN_TRANSITION_WINDOWS_SHA256`
- `SOLIN_TRANSITION_MACOS_URL` and `SOLIN_TRANSITION_MACOS_SHA256`
- `SOLIN_TRANSITION_LINUX_URL` and `SOLIN_TRANSITION_LINUX_SHA256`

These variables are build-time E2E fixtures only. They are never read by the
application and do not preserve the previous update protocol. Apple Silicon has
no native transition predecessor and is validated as a clean install on its
first release. Later workflows resolve predecessors and hashes from prior
GitHub release manifests automatically, so the transition variables are no
longer used.

The first version containing the GitHub updater must also be announced through
the old SolinAV version endpoint so existing installations can migrate. New
versions use GitHub only for update discovery; the SolinAV notification service
remains independent.

macOS packages currently use free ad-hoc signing. This seals the bundle but does
not establish a Developer ID identity or provide notarization, so Gatekeeper may
require explicit user approval. Developer ID signing, hardened runtime, and
notarization remain future distribution work.
