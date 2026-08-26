#pragma once

#include <chrono>

namespace solin::media_engine::detail {

using namespace std::chrono_literals;

struct NativeWindowWaitPlan final {
    bool wait_for_activity{false};
    std::chrono::milliseconds maximum_interval{100ms};

    bool operator==(const NativeWindowWaitPlan&) const = default;
};

[[nodiscard]] constexpr NativeWindowWaitPlan native_window_wait_plan(
    const bool has_content_target, const bool has_program_target,
    const bool has_active_transition) noexcept {
    return {
        .wait_for_activity = has_content_target || has_program_target,
        .maximum_interval = has_active_transition ? 16ms : 100ms,
    };
}

} // namespace solin::media_engine::detail
