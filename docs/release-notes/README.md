# Release notes

Each distributed version owns a directory named exactly like its GitHub tag.
An `en.md` file is mandatory and other translations use existing Solin locale codes:

```text
docs/release-notes/26.32.0/en.md
docs/release-notes/26.32.0/pt_BR.md
docs/release-notes/26.33.0-beta.1/en.md
```

Write user-visible behavior rather than commit messages. The GitHub Release,
release manifest, and in-app update dialog all consume these files. Missing
translations fall back to English.

`prepare beta` creates an empty `en.md`. Leave it empty to use the automatically
grouped PRs and related issues as the English notes in the published manifest
and in-app updater. This needs no manual writing or review marker removal.
A missing `en.md` is still an error. Optional translations with no content fall
back to English; filled notes remain authoritative for their language.
When every contribution is excluded, the generated notes link to the verified
commit comparison. The source files remain empty after generation.

Stable releases require nonempty reviewed English notes. `prepare promote`
includes only written beta notes in its review draft; complete it with the
user-visible changes from the whole release line before tagging.

Create the next directory through `python scripts/release.py prepare <intent>`.
Do not create a tag while the generated English file still contains its review
marker.

The English **Related pull requests and issues** block is generated separately
in the GitHub Release body during publication. It includes every merged PR in
the verified release interval, including documentation, build, and release
preparation, grouped by current PR labels without a Conventional Commit title
requirement. Each entry appears once, and unclassified PRs and direct commits use
**Other changes**.
The `skip-changelog` label excludes a PR and its related issue links from this
block regardless of its category. It is assigned automatically to `chore(release):`
PRs and can also be applied manually; manual additions and removals override
automation for that PR. It does not affect reviewed
notes, the commit interval, builds, or publication. Removing it restores the
entry on the next generation.
Empty categories are omitted; the label catalog and priority are documented in
[contributor credits](../releases.md#contributor-credits).
Prefixes are removed only for presentation. PR entries credit the PR author and
Git coauthors, excluding bots; direct commits use linked titles and their authors
and coauthors as a fallback. Related issues come from the included PR's closing
links and explicit references in its title or description, regardless of issue
state or closure. They provide context without asserting a fix or crediting issue
authors. References already in the PR title are not appended again.

Keep generated entries out of the source localized note files. The manifest and
in-app notes use them only as the empty-beta fallback described above. See
[contributor credits](../releases.md#contributor-credits) for
the frozen range rules, permissions, and the body-only manual workflow for
existing releases.
