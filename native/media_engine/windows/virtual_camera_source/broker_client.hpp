#pragma once

#include "solin/media_engine/shared_video_frame_channel.hpp"
#include "solin/media_engine/virtual_camera_broker_protocol.hpp"

#include <mfobjects.h>
#include <wrl/client.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace solin::media_engine::windows_virtual_camera {

class BrokerFrameProvider final {
  public:
    BrokerFrameProvider(std::wstring pipe_name, VirtualCameraBrokerToken token,
                        PackedVideoFrameLayout layout,
                        std::uint32_t fps_numerator,
                        std::uint32_t fps_denominator);
    ~BrokerFrameProvider();

    BrokerFrameProvider(const BrokerFrameProvider&) = delete;
    BrokerFrameProvider& operator=(const BrokerFrameProvider&) = delete;

    [[nodiscard]] bool visit_frame(
        const SharedVideoFrameReader::FrameVisitor& visitor);
    [[nodiscard]] const PackedVideoFrameLayout& layout() const noexcept;
    [[nodiscard]] std::uint32_t fps_numerator() const noexcept;
    [[nodiscard]] std::uint32_t fps_denominator() const noexcept;
    [[nodiscard]] std::uint64_t generation() const noexcept;

  private:
    void reconnect_loop() noexcept;

    std::wstring pipe_name_{};
    VirtualCameraBrokerToken token_{};
    PackedVideoFrameLayout layout_{};
    std::uint32_t fps_numerator_{0U};
    std::uint32_t fps_denominator_{0U};
    std::unique_ptr<SharedVideoFrameReader> reader_{};
    std::mutex mutex_{};
    PackedVideoFrame standby_frame_{};
    std::chrono::steady_clock::time_point last_progress_{};
    std::uint64_t last_heartbeat_{0U};
    std::condition_variable reconnect_wakeup_{};
    std::atomic_bool stop_requested_{false};
    bool reconnect_requested_{true};
    std::thread reconnect_worker_{};
    std::atomic_uint64_t generation_{0U};
};

[[nodiscard]] HRESULT connect_to_frame_broker(
    IMFAttributes* activation_attributes,
    std::shared_ptr<BrokerFrameProvider>& provider) noexcept;
[[nodiscard]] HRESULT validate_frame_broker_activation(
    IMFAttributes* activation_attributes) noexcept;

} // namespace solin::media_engine::windows_virtual_camera
