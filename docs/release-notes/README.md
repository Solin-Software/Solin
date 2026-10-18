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

## Editorial format

Use this format for manually written notes. The change categories follow
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version numbers still
follow Solin's [CalVer contract](../releases.md#calver-contract).

An optional opening paragraph is reserved for stable releases with several
notable changes. Summarize the main benefits or a useful new workflow in two or
three sentences. Omit it when the entries already explain a small release.

Use level-three headings in the following order, without trailing colons or
emojis. Omit every section that has no relevant entries.

| English heading | Portuguese heading | Content |
| --- | --- | --- |
| `Upgrade notes` | `Notas de atualização` | Required migration steps, compatibility changes, or precautions before updating. Include the affected users and the action they must take. |
| `Added` | `Adicionado` | New capabilities available to users. |
| `Changed` | `Alterado` | Improvements or changes to existing behavior. |
| `Fixed` | `Corrigido` | Corrections to problems users could encounter. |
| `Removed` | `Removido` | Removed capabilities and any replacement or migration path. |
| `Security` | `Segurança` | Security fixes or protections, with advisory links when available. |

- Write concise bullets beginning with a bold product-area label, such as
  `- **Meetings:** ...`. Keep a single concise change on that line. When an area
  has several distinct changes within the same category, use its label as a
  parent bullet and give each change its own sub-bullet, as shown below. Use only
  one nesting level; do not split one change merely because its description is
  long. Use the application's terminology consistently and describe each change
  once, under its most relevant category.
- Describe the visible outcome and any useful context. Include technical details
  only when they help users understand compatibility or decide what to do.
  Omit implementation logs and routine dependency, CI, or test changes with no
  user impact.
- Stable notes compare the final behavior with the previous stable release,
  consolidating all betas. For the first GitHub-hosted stable release, use the
  verified historical stable reference documented in the
  [contribution range rules](../releases.md#contributor-credits). Fold refinements
  of new features into their descriptions; omit fixes for regressions introduced
  and resolved only during that beta cycle. Written beta notes describe changes
  since the preceding published version; they need no opening summary.
- Keep translations equivalent in meaning, section order, and upgrade guidance.
  Translate headings and product labels using the application's vocabulary.
- Start source files with the summary or first applicable section. Publication
  supplies the version heading and the generated contribution appendix. Keep PR
  inventories and contributor lists in that appendix rather than duplicating
  them in the manual notes.

Example within `Added`:

```markdown
- **Meetings:**
  - Save a meeting as a template to reuse its structure when preparing future meetings.
  - Preview a meeting's media in sequence before presenting it to the audience.
- **Playlists:** Export the playlist as a PDF checklist for use during the meeting.
```

## Preparation

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

## Generated contributions

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
