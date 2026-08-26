#pragma once

#include "solin/media_engine/scene_snapshot.hpp"

#include <algorithm>
#include <ranges>
#include <string>
#include <vector>

namespace solin::media_engine::detail {

struct NativeWindowRoutingPlan final {
    std::vector<std::string> retained_target_ids{};
    std::vector<std::string> added_target_ids{};
    std::vector<std::string> removed_target_ids{};

    bool operator==(const NativeWindowRoutingPlan&) const = default;
};

[[nodiscard]] inline bool live_target_exists(
    const std::vector<OutputWindowConfiguration>& targets,
    const std::string& target_id) {
    return std::ranges::any_of(targets, [&target_id](const auto& target) {
        return target.visible && target.target_id == target_id;
    });
}

// A target id owns one long-lived GStreamer/D3D11 presenter. Route membership
// is reconciled per id so adding an operator preview cannot disturb unchanged
// physical outputs.
[[nodiscard]] inline NativeWindowRoutingPlan native_window_routing_plan(
    const std::vector<OutputWindowConfiguration>& current,
    const std::vector<OutputWindowConfiguration>& next) {
    NativeWindowRoutingPlan plan;
    for (const auto& target : current) {
        if (!target.visible) {
            continue;
        }
        if (live_target_exists(next, target.target_id)) {
            plan.retained_target_ids.push_back(target.target_id);
        } else {
            plan.removed_target_ids.push_back(target.target_id);
        }
    }
    for (const auto& target : next) {
        if (target.visible && !live_target_exists(current, target.target_id)) {
            plan.added_target_ids.push_back(target.target_id);
        }
    }
    return plan;
}

} // namespace solin::media_engine::detail
