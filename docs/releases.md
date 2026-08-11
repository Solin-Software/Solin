# Versioning and releases

## Version contract

Solin versions contain four numeric components:

```text
YY.RELEASE.PATCH.REVISION
```

- `YY` is the two-digit release year.
- `RELEASE` is the sequential release line within that year.
- `PATCH` starts at zero and increments for a corrective release on the same
  release line.
- `REVISION` is reserved for packaging compatibility and remains zero for
  normal cross-platform releases.

For example, `26.26.1.0` is the first corrective release for release line 26 in
2026. The canonical version is defined once in `src/solin/version.py`.

macOS application metadata uses the first three components. Do not increment
`REVISION` without reviewing the update API and every platform workflow.

## Release policy

Merging to `main` does not distribute a new version. `main` should remain in a
state that can be built, while distribution is a separate, explicit step.

Prepare a distributed version in a focused pull request:

1. choose the next version according to the contract above;
2. update `src/solin/version.py`;
3. prepare the release notes in the existing external changelog flow;
4. run lint, type checking, tests, and locale validation;
5. verify the version in application, installer, and update metadata;
6. merge the reviewed release preparation;
7. run the required platform build workflows manually, keeping the default
   Windows signing gate enabled for every distributed Windows build;
8. run packaged startup and previous-version upgrade checks;
9. publish the verified files through the existing distribution process;
10. record the full source commit SHA with the distributed version.

An internal Git tag such as `v26.26.1` may be used as a convenient name for that
commit. A tag does not create a GitHub Release, publish files, or expose a
private repository; the recorded commit SHA remains the required traceability
reference.

## GitHub Actions

The platform workflows build and verify the application. Their artifacts are
temporary, and a successful run does not distribute a new version.

Windows and macOS upgrade smoke tests require the URL of the previously
distributed build. Do not keep an old URL as a workflow default: provide the
correct predecessor or disable the upgrade smoke test for a diagnostic build.
Unsigned Windows workflow output is diagnostic-only, uses an explicit
`unsigned-diagnostic` artifact/file suffix, and must not enter the distribution
process.
