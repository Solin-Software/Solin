# Third-party components

## nlohmann/json 3.12.0

- Purpose: strict parsing and serialization of low-frequency control messages.
- Source: <https://github.com/nlohmann/json/releases/tag/v3.12.0>
- License: MIT; see `third_party/nlohmann-json.LICENSE.MIT`.
- Build integrity: the release archive SHA-256 is pinned in `CMakeLists.txt`.

Video frames do not pass through this dependency.

## GStreamer 1.28.5

- Purpose: native camera and RTSP acquisition, media negotiation, bounded queues, and the
  initial Direct3D 11 composition substrate.
- Source: <https://gstreamer.freedesktop.org/src/>
- Windows binaries: the official MSVC x86_64 installer is version- and SHA-256-pinned by
  `scripts/install_gstreamer_windows.ps1`.
- License: GStreamer core is LGPL-2.1-or-later. Plugins keep their own license metadata.
- Distribution status: the development bundle is not an application payload. Release
  packaging copies an explicit plugin allowlist, resolves only its transitive runtime DLLs,
  audits every selected plugin as LGPL, rejects known GPL components, and emits a machine-
  readable runtime manifest. Redistribution notices are shipped with the curated payload.
