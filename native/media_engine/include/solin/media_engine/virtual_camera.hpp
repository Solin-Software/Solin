#pragma once

#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/video_frame.hpp"
#include "solin/media_engine/virtual_camera_identity.hpp"

#include <cstdint>
#include <memory>
#include <string>

namespace solin::media_engine {

enum class VirtualCameraBackendKind : std::uint8_t {
    unavailable,
    windows_media_foundation,
    windows_directshow,
    linux_v4l2_loopback,
    macos_core_media_io,
};

struct VirtualCameraBackendProbe final {
    VirtualCameraBackendKind backend{VirtualCameraBackendKind::unavailable};
    bool platform_supported{false};
    bool registration_api_available{false};
    bool source_component_installed{false};
    bool cross_session_transport_available{false};
    bool operational{false};
    std::uint32_t operating_system_build{0U};
    std::string error_code{"virtual_camera_platform_unsupported"};

    bool operator==(const VirtualCameraBackendProbe&) const = default;
};

struct VirtualCameraConfiguration final {
    std::string camera_id{};
    std::string friendly_name{};
    OutputVideoFormat video_format{};

    bool operator==(const VirtualCameraConfiguration&) const = default;
};

enum class VirtualCameraSinkState : std::uint8_t {
    stopped,
    starting,
    ready,
    degraded,
    failed,
};

struct VirtualCameraSinkHealth final {
    VirtualCameraSinkState state{VirtualCameraSinkState::stopped};
    std::uint64_t published_frames{0U};
    std::uint64_t dropped_frames{0U};
    std::uint64_t last_frame_sequence{0U};
    std::string error_code{};

    bool operator==(const VirtualCameraSinkHealth&) const = default;
};

struct VirtualCameraFrameEndpoint final {
    std::uint64_t generation{0U};
    PackedVideoFrameLayout layout{};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{0U};
    std::uintptr_t process_local_mapping_handle{0U};
    std::uint64_t mapping_size{0U};
    std::string cross_session_file_path_utf8{};

    [[nodiscard]] explicit operator bool() const noexcept {
        return generation != 0U && !cross_session_file_path_utf8.empty() &&
               mapping_size != 0U && fps_numerator != 0U &&
               fps_denominator != 0U;
    }
};

class VirtualCameraSink {
  public:
    virtual ~VirtualCameraSink() = default;

    VirtualCameraSink(const VirtualCameraSink&) = delete;
    VirtualCameraSink& operator=(const VirtualCameraSink&) = delete;
    VirtualCameraSink(VirtualCameraSink&&) = delete;
    VirtualCameraSink& operator=(VirtualCameraSink&&) = delete;

    virtual void start() = 0;
    [[nodiscard]] virtual bool publish(const VideoFrameView& frame) noexcept = 0;
    virtual void heartbeat() noexcept {}
    virtual void stop() noexcept = 0;
    [[nodiscard]] virtual VirtualCameraSinkHealth health() const = 0;

  protected:
    VirtualCameraSink() = default;
};

class VirtualCameraBackend {
  public:
    virtual ~VirtualCameraBackend() = default;

    VirtualCameraBackend(const VirtualCameraBackend&) = delete;
    VirtualCameraBackend& operator=(const VirtualCameraBackend&) = delete;
    VirtualCameraBackend(VirtualCameraBackend&&) = delete;
    VirtualCameraBackend& operator=(VirtualCameraBackend&&) = delete;

    [[nodiscard]] virtual VirtualCameraBackendProbe probe() const = 0;
    [[nodiscard]] virtual std::unique_ptr<VirtualCameraSink>
    create_sink(const VirtualCameraConfiguration& configuration) = 0;

  protected:
    VirtualCameraBackend() = default;
};

class SharedFrameVirtualCameraSink : public VirtualCameraSink {
  public:
    ~SharedFrameVirtualCameraSink() override = default;

    [[nodiscard]] virtual VirtualCameraFrameEndpoint endpoint() const noexcept = 0;

  protected:
    SharedFrameVirtualCameraSink() = default;
};

[[nodiscard]] VirtualCameraBackendProbe probe_platform_virtual_camera() noexcept;
[[nodiscard]] std::unique_ptr<VirtualCameraBackend>
make_platform_virtual_camera_backend();
[[nodiscard]] std::unique_ptr<SharedFrameVirtualCameraSink>
make_shared_frame_virtual_camera_sink(const VirtualCameraConfiguration& configuration,
                                      std::uint64_t generation);

} // namespace solin::media_engine
