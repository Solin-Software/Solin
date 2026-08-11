#pragma once

#ifdef _WIN32

#include "solin/media_engine/virtual_camera.hpp"

#include <cstdint>
#include <memory>

struct IMFAttributes;

namespace solin::media_engine {

[[nodiscard]] VirtualCameraBackendProbe
probe_windows_media_foundation_virtual_camera() noexcept;
[[nodiscard]] std::unique_ptr<VirtualCameraBackend>
make_windows_media_foundation_virtual_camera_backend();
[[nodiscard]] bool windows_virtual_camera_is_registered(
    IMFAttributes* attributes) noexcept;
[[nodiscard]] int run_windows_virtual_camera_registration_host(
    std::uintptr_t command_read_handle,
    std::uintptr_t status_write_handle) noexcept;

} // namespace solin::media_engine

#endif
