#pragma once

#include "solin/media_engine/shared_video_frame_channel.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace solin::media_engine::windows_virtual_camera {

class BrokerFrameProvider final {
  public:
    BrokerFrameProvider() noexcept;
    ~BrokerFrameProvider();

    BrokerFrameProvider(const BrokerFrameProvider&) = delete;
    BrokerFrameProvider& operator=(const BrokerFrameProvider&) = delete;

    void start() noexcept;
    void stop() noexcept;
    [[nodiscard]] bool visit_live_frame(
        const SharedVideoFrameReader::FrameVisitor& visitor) noexcept;
    [[nodiscard]] std::uint64_t generation() const noexcept;

  private:
    void reconnect_loop() noexcept;

    std::wstring pipe_name_{};
    std::unique_ptr<SharedVideoFrameReader> reader_{};
    std::uintptr_t broker_connection_{0U};
    mutable std::mutex lifecycle_mutex_{};
    mutable std::mutex mutex_{};
    std::chrono::steady_clock::time_point last_progress_{};
    std::uint64_t last_heartbeat_{0U};
    std::condition_variable reconnect_wakeup_{};
    std::atomic_bool stop_requested_{false};
    bool reconnect_requested_{false};
    std::thread reconnect_worker_{};
    std::atomic_uint64_t generation_{0U};
};

} // namespace solin::media_engine::windows_virtual_camera
