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
