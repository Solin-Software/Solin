# Release notes

Each distributed version owns a directory named exactly like its GitHub tag.
English is mandatory and other translations use existing Solin locale codes:

```text
docs/release-notes/26.32.0/en.md
docs/release-notes/26.32.0/pt_BR.md
docs/release-notes/26.33.0-beta.1/en.md
```

Write user-visible behavior rather than commit messages. The GitHub Release,
release manifest, and in-app update dialog all consume these files. Missing
translations fall back to English.

Create the next directory through `python scripts/release.py prepare <intent>`.
Do not create a tag while the generated English file still contains its review
marker.

The English **Related pull requests and issues** block is generated separately
in the GitHub Release body during publication. It includes every merged PR in
the verified release interval, including documentation, build, and release
preparation, without categories or a Conventional Commit title requirement.
Prefixes are removed only for presentation. PR entries credit the PR author and
Git coauthors, excluding bots; direct commits use linked titles and their authors
and coauthors as a fallback. Issues require an actual closure by an included
merged PR and a `CLOSED` state with reason `COMPLETED`, with no issue-author
attribution.

Keep these generated entries out of the reviewed localized notes, manifest, and
in-app notes. See [contributor credits](../releases.md#contributor-credits) for
the frozen range rules, permissions, and the body-only manual workflow for
existing releases.
