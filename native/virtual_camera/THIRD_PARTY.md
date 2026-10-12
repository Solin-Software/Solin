# Third-party components

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
