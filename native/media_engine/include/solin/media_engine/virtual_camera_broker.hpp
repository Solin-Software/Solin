#pragma once

#include "solin/media_engine/virtual_camera.hpp"
#include "solin/media_engine/virtual_camera_broker_protocol.hpp"

#include <cstdint>
#include <memory>
#include <string>

namespace solin::media_engine {

struct VirtualCameraBrokerActivation final {
    std::string pipe_name{};
    VirtualCameraBrokerToken token{};
    std::uint16_t protocol_version{kVirtualCameraBrokerProtocolVersion};

    bool operator==(const VirtualCameraBrokerActivation&) const = default;
};

struct VirtualCameraBrokerHealth final {
    bool running{false};
    std::uint64_t accepted_connections{0U};
    std::uint64_t denied_connections{0U};
    std::uint64_t failed_connections{0U};
    std::string error_code{};

    bool operator==(const VirtualCameraBrokerHealth&) const = default;
};

class VirtualCameraFrameBroker {
  public:
    virtual ~VirtualCameraFrameBroker() = default;

    VirtualCameraFrameBroker(const VirtualCameraFrameBroker&) = delete;
    VirtualCameraFrameBroker& operator=(const VirtualCameraFrameBroker&) = delete;
    VirtualCameraFrameBroker(VirtualCameraFrameBroker&&) = delete;
    VirtualCameraFrameBroker& operator=(VirtualCameraFrameBroker&&) = delete;

    virtual void start() = 0;
    virtual void stop() noexcept = 0;
    [[nodiscard]] virtual VirtualCameraBrokerActivation activation() const = 0;
    [[nodiscard]] virtual VirtualCameraBrokerHealth health() const = 0;

  protected:
    VirtualCameraFrameBroker() = default;
};

[[nodiscard]] std::unique_ptr<VirtualCameraFrameBroker>
make_virtual_camera_frame_broker(
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink);
[[nodiscard]] std::unique_ptr<VirtualCameraFrameBroker>
make_virtual_camera_frame_broker(
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink,
    VirtualCameraBrokerActivation activation);

} // namespace solin::media_engine
