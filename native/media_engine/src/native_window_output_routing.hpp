#pragma once

#include "solin/media_engine/scene_snapshot.hpp"

#include <algorithm>
#include <ranges>
#include <vector>

namespace solin::media_engine::detail {

// A stable target id owns one long-lived GStreamer/D3D11 presenter. Moving that
// presenter between Qt hosts is a cheap HWND reparent; visibility changes still
// alter the set of live pipelines and therefore require a rebuild.
[[nodiscard]] inline bool same_presenter_set(
    const std::vector<OutputWindowConfiguration>& current,
    const std::vector<OutputWindowConfiguration>& next) {
    if (current.size() != next.size()) {
        return false;
    }
    return std::ranges::all_of(current, [&next](const auto& target) {
        const auto match = std::ranges::find_if(next, [&target](const auto& candidate) {
            return candidate.target_id == target.target_id;
        });
        return match != next.end() && match->visible == target.visible;
    });
}

} // namespace solin::media_engine::detail
