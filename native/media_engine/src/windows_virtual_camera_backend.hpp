#pragma once

#ifdef _WIN32

#include "solin/media_engine/virtual_camera.hpp"

#include <filesystem>
#include <memory>

namespace solin::media_engine {

[[nodiscard]] VirtualCameraBackendProbe
probe_windows_directshow_virtual_camera() noexcept;
[[nodiscard]] bool windows_pe_dll_matches_architecture(
    const std::filesystem::path& path, bool x64) noexcept;
[[nodiscard]] std::unique_ptr<VirtualCameraBackend>
make_windows_directshow_virtual_camera_backend();

} // namespace solin::media_engine

#endif
