#pragma once

#include <chrono>
#include <cstdint>

namespace solin::media_engine::windows_virtual_camera {

// Live capture drops elapsed wall-clock slots after backpressure. Media sample
// timestamps retain their separate, contiguous rational timeline.
class DirectShowFrameCadence final {
  public:
    using Clock = std::chrono::steady_clock;

    struct Deadline final {
        Clock::time_point due;
        bool discontinuity;
    };

    void reset(const Clock::time_point now) noexcept {
        epoch_ = now;
        index_ = 0U;
    }

    [[nodiscard]] Deadline next(const Clock::time_point now) noexcept {
        auto due = epoch_ + std::chrono::seconds{index_ / 30U} +
                   std::chrono::nanoseconds{static_cast<std::int64_t>(
                       (index_ % 30U) * 1'000'000'000ULL / 30U)};
        constexpr auto maximum_lateness =
            std::chrono::nanoseconds{1'000'000'000LL / 30LL};
        const auto discontinuity = now > due + maximum_lateness;
        if (discontinuity) {
            reset(now);
            due = now;
        }
        ++index_;
        return {due, discontinuity};
    }

  private:
    Clock::time_point epoch_{};
    std::uint64_t index_{0U};
};

} // namespace solin::media_engine::windows_virtual_camera
