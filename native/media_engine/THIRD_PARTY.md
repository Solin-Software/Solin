# Third-party components

## nlohmann/json 3.12.0

- Purpose: strict parsing and serialization of low-frequency control messages.
- Source: <https://github.com/nlohmann/json/releases/tag/v3.12.0>
- License: MIT; see `third_party/nlohmann-json.LICENSE.MIT`.
- Build integrity: the release archive SHA-256 is pinned in `CMakeLists.txt`.

Video frames do not pass through this dependency.

## Microsoft DirectShow BaseClasses

- Purpose: source-filter, pin, allocator, graph, and COM infrastructure for the Windows
  virtual camera.
- Source: Microsoft Windows classic samples, revision
  `d59e5f1dc9c768615e4e1ab1f0f009e6a3ed747c`.
- License: MIT; see `third_party/directshow-baseclasses.LICENSE`.
- Build integrity: the source archive and SHA-256 are pinned in
  `cmake/SolinVirtualCameraFilter.cmake`. BaseClasses use the same static MSVC runtime as the
  filter; no SDK `strmbase.lib` ABI is mixed into it.

## libyuv

- Purpose: bounded NV12 scaling and negotiated NV12-to-YUY2 conversion inside the
  DirectShow filter.
- Upstream source: <https://chromium.googlesource.com/libyuv/libyuv/+/eb6e7bb63738e29efd82ea3cf2a115238a89fa51>.
- Archive mirror used by the build: <https://github.com/lemenkov/libyuv>; revision
  `eb6e7bb63738e29efd82ea3cf2a115238a89fa51` is byte-pinned by SHA-256 because
  upstream Gitiles archives are gzip-time-dependent.
- License: BSD-3-Clause; see `third_party/libyuv.LICENSE`.
- Build integrity: the mirror archive and SHA-256 are pinned in
  `cmake/SolinVirtualCameraFilter.cmake`; it is linked privately and statically only into the
  virtual-camera filter.

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
