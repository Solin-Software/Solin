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
updates the canonical version, and creates the English release-note file. A beta
starts with an empty `en.md`, selecting automatic notes without an editorial step.
Stable preparations start with a draft that requires review. The command
does not create a tag or publish anything. The generated notes and version
change are reviewed together in a focused pull request, which also receives the
normal quality checks.

Release notes live under `docs/release-notes/<tag>/<locale>.md`. `en.md` is
required before tagging. An empty file is allowed only for a beta; a missing
file or a pending review marker still fails validation. During publication, a beta
with empty English notes uses the same grouped PR and issue entries as the GitHub
appendix for the manifest and in-app update dialog. Filled notes take precedence;
empty optional translations fall back to English. If no entries remain after
exclusions, the fallback is a link to the verified commit comparison.
Other files use an existing application locale such as
`pt_BR.md`; the updater falls back to English when a translation is absent.
Stable notes consolidate the user-visible changes that were exercised in beta.
Promotion carries forward only written beta notes into the stable review draft;
automatic beta notes are not copied. The stable draft must still be completed
and reviewed, including changes from betas without written notes.

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

### Contributor credits

Publication appends a managed English **Related pull requests and issues** block
to the GitHub Release body. Written English and translated changelogs stay
unchanged. When a beta has empty English notes, the manifest and in-app notes
use the visible content from that same contribution snapshot, without duplicating
the block in the GitHub body. Generation never writes back to source note files.
GitHub account mentions on contribution entries provide the release's contributor
avatars.

Entries come from the exact commits after the previous published version up to
the new tag. A beta uses the highest earlier published version; a stable release
uses the highest earlier published stable version, including contributions from
its betas. Drafts and tags from failed publication attempts are not predecessors.
Version order, not publication date, determines the predecessor.

Until an appropriate predecessor exists, the historical boundary is commit
`2209281ebe91c9f8872132d4f3aafdf1ba0c012b`, the externally distributed
`26.31.1.0` bump. That commit is excluded. Both boundaries must belong to the
same verified history; missing tags or divergent histories fail rather than
silently crediting all repository history.

The managed block records both commit boundaries. Subsequent updates preserve
that recorded interval even if an older release is published later.

Every merged pull request whose merge commit belongs to the interval is included
unless it carries the `skip-changelog` label. Documentation,
build, and release-preparation PRs are otherwise included. Entries use linked
PR titles grouped by their current labels, without filtering by Conventional
Commit type or title format. A Conventional Commit prefix, when present, is
removed only for presentation; the original title remains unchanged on GitHub.
Titles without that format are also included.

Each PR appears in exactly one category. The ordered label catalog in
[`.github/labels.json`](../.github/labels.json) defines category names and priority:
**Bug fixes**, **Features and improvements**, **CI/CD**, **Dependencies**,
**Translations**, then **Documentation**. When several labels match, the first
category wins; a bug fix that also changes Markdown remains under **Bug fixes**.
PRs with no recognized label and direct commits appear under **Other changes**.
Empty categories are omitted. Labels on related issues do not classify their PR.
`skip-changelog` overrides every category and hides only that PR's entry and its
related issue links. The labeling workflow assigns it to `chore(release):` PRs
automatically and removes its own assignment if the title no longer matches.
Manual additions and removals override automation for that PR.
Removing the label restores the entry on the next generation. Exclusions do not
change the verified commit interval, produce fallback commit entries, edit reviewed
notes, or skip any release build, validation, or publication step. If all entries
are excluded, only the managed interval markers remain, without empty headings.
Manual PR label changes are reflected the next time the appendix is generated;
already published releases change only through the explicit contributor update.
That update changes only the GitHub body; automatic notes already embedded in
a published manifest remain the snapshot taken during publication.

PR entries credit the PR author, authors of included associated Git commits, and
Git coauthors recognized through `Co-authored-by` identities. Bots are excluded
and accounts are deduplicated on
each entry. GitHub resolves accounts; the publisher never guesses logins from
names or email addresses. Commits without an included merged PR fall back to a
linked commit title with its human author and Git coauthors, using the same
prefix removal for presentation. Unlinked commit authors produce a warning so
their GitHub email association can be corrected.

Related issues come from an included merged PR's closing links and explicit
references in its title or description: `#123`, `owner/repository#123`, or a
GitHub issue URL. References are verified as issues; pull request numbers and
missing issues are skipped. Comments are not scanned. Issue state, closure
reason, and which PR closed it do not affect inclusion. These links provide
context, without asserting that the release fixes every related issue or
crediting issue authors. An issue already referenced in the PR title is not
appended again to that entry.

For an already published release, run **Release contributors** from Actions.
Supply the existing tag and select `apply` to update it; the default is a preview.
The CLI equivalent is:

```text
git fetch origin main --tags
python scripts/release.py contributors --tag 26.32.0-beta.4
python scripts/release.py contributors --tag 26.32.0-beta.4 --apply
```

Or dispatch the manual workflow after it is merged into `main`:

```text
gh workflow run release-contributors.yml --ref main -f tag=26.32.0-beta.4 -f apply=false
gh workflow run release-contributors.yml --ref main -f tag=26.32.0-beta.4 -f apply=true
```

The workflow reports the exact interval and generated entries in its run summary
and shares the publication concurrency lock. It verifies the existing source marker
against the actual tag, preserves all text outside its managed block, detects
intervening edits, and updates only the release body. Repeating an unchanged
update is a no-op. It does not rebuild packages, upload assets, move tags, change
the release channel, or change `latest`.

Publication and the manual workflow require **Pull requests: read** and
**Issues: read** to resolve merged PRs, their authors, and related issues,
alongside **Contents: write** to update the release body.

The manual workflow normally uses `GITHUB_TOKEN`. GitHub can require additional
workflow authorization when an older release's target differs from `main` in
`.github/workflows/`, even for a body-only update. For that case, configure the
optional repository secret `RELEASE_NOTES_TOKEN`: a fine-grained token restricted
to this repository with **Contents: write**, **Pull requests: read**,
**Issues: read**, and **Workflows: write**. Alternatively, run the CLI using an
appropriately authorized `gh` login. Do not change the release target to bypass
this check. See GitHub's
[update-release permissions](https://docs.github.com/en/rest/releases/releases#update-a-release).

### Published package integrity

Published assets are never replaced. A failed run may safely complete its owned
draft; an application correction to a published release requires a new version.
Related pull requests, issues, and contributor attribution can be maintained
separately as described above. Repository release immutability is an external
prerequisite for production publication;
it locks each published tag and its assets after the workflow publishes the
draft.

The platform workflows remain manually runnable for diagnostics. Manual output
is short lived and is not distribution.

## Shared compilation cache

**Main build cache** runs the same platform builders after pushes to `main`,
including both macOS architectures. It uses the Quality change classifier to
skip documentation-only changes and version increases accompanied only by
Markdown notes. A manual run on `main` always rebuilds, including when a cache
has expired. Runs on other branches are skipped.

These builds populate the default branch's pip and Nuitka caches, which GitHub
allows subsequent branch and tag builds to restore. They do not publish a
release or run the release-only installation and upgrade tests. Their diagnostic
packages are retained for one day; tag publication still runs every required
release check. Each platform lets its running build finish before another starts,
so frequent pushes do not repeatedly cancel compilation before the cache can be
saved. Classification happens before joining these queues, so documentation-only
pushes do not replace a queued compilation.

All builders store Nuitka's cache, including its `ccache` or `clcache` output,
under `NUITKA_CACHE_DIR`. Compilation snapshots are separated by operating
system, architecture, Python version, and dependency/build-tool requirements.
The commit identifies each immutable snapshot; a restore prefix can reuse the
latest compatible snapshot when source code or the release version changes.
The compiler cache still validates individual compilation inputs and compiler
identity before reusing an object file.

The first successful main build populates the cache. Later builds still perform
Nuitka analysis, linking, packaging, and their configured checks; restoring a
snapshot does not mean every compiled file will be a cache hit. Missing or
evicted caches cause a normal full compilation. Wait for **Main build cache**
to finish before tagging when immediate reuse matters. See GitHub's
[cache access restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).

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
