#include "solin/media_engine/scene_renderer.hpp"

#include <algorithm>
#include <cstddef>
#include <stdexcept>
#include <utility>

namespace solin::media_engine {

SceneTransitionKind scene_transition_kind_from_text(const std::string_view value) {
    if (value == "cut") {
        return SceneTransitionKind::cut;
    }
    if (value == "dissolve") {
        return SceneTransitionKind::dissolve;
    }
    if (value == "fade_to_black") {
        return SceneTransitionKind::fade_to_black;
    }
    throw std::invalid_argument("invalid scene transition kind");
}

std::string_view scene_transition_kind_text(const SceneTransitionKind kind) noexcept {
    switch (kind) {
    case SceneTransitionKind::cut:
        return "cut";
    case SceneTransitionKind::dissolve:
        return "dissolve";
    case SceneTransitionKind::fade_to_black:
        return "fade_to_black";
    }
    return "cut";
}

void validate_scene_transition(const SceneTransitionSpec& transition) {
    if (transition.kind == SceneTransitionKind::cut) {
        if (transition.duration_ms != 0U) {
            throw std::invalid_argument("CUT transition duration must be zero");
        }
        return;
    }
    if (transition.duration_ms < kMinimumAnimatedTransitionDurationMs ||
        transition.duration_ms > kMaximumAnimatedTransitionDurationMs) {
        throw std::invalid_argument("animated transition duration is out of range");
    }
}

SceneTransitionWeights scene_transition_weights(const SceneTransitionSpec& transition,
                                                 const double progress) noexcept {
    const auto normalized = std::clamp(progress, 0.0, 1.0);
    switch (transition.kind) {
    case SceneTransitionKind::cut:
        return {.outgoing = 0.0, .incoming = 1.0};
    case SceneTransitionKind::dissolve:
        return {.outgoing = 1.0 - normalized, .incoming = normalized};
    case SceneTransitionKind::fade_to_black:
        if (normalized < 0.5) {
            return {.outgoing = 1.0 - (normalized * 2.0), .incoming = 0.0};
        }
        return {.outgoing = 0.0, .incoming = (normalized - 0.5) * 2.0};
    }
    return {.outgoing = 0.0, .incoming = 1.0};
}

SceneTransitionPadAlphas scene_transition_source_over_alphas(
    const SceneTransitionWeights weights) noexcept {
    const auto incoming = std::clamp(weights.incoming, 0.0, 1.0);
    if (incoming >= 1.0) {
        return {.outgoing = 0.0, .incoming = 1.0};
    }
    return {
        .outgoing =
            std::clamp(weights.outgoing / (1.0 - incoming), 0.0, 1.0),
        .incoming = incoming,
    };
}

BgraPixel scene_transition_blend_pixel(const SceneTransitionSpec& transition,
                                       const double progress,
                                       const BgraPixel& outgoing,
                                       const BgraPixel& incoming) noexcept {
    const auto weights = scene_transition_weights(transition, progress);
    BgraPixel result{};
    for (std::size_t channel = 0U; channel < 3U; ++channel) {
        const auto value = static_cast<double>(outgoing[channel]) * weights.outgoing +
                           static_cast<double>(incoming[channel]) * weights.incoming;
        result[channel] = static_cast<std::uint8_t>(
            std::clamp(value + 0.5, 0.0, 255.0));
    }
    // The compositor background is opaque black, so fade-to-black remains an
    // opaque video frame even while both scene layers have zero alpha.
    result[3U] = 255U;
    return result;
}

std::chrono::nanoseconds
scene_transition_frame_interval(const OutputVideoFormat& format) {
    if (format.fps_numerator == 0U || format.fps_denominator == 0U) {
        throw std::invalid_argument("transition output frame rate is invalid");
    }
    return std::chrono::nanoseconds{
        (1'000'000'000ULL * format.fps_denominator) / format.fps_numerator};
}

SceneRendererError::SceneRendererError(std::string error_code, std::string message)
    : std::runtime_error(std::move(message)), error_code_(std::move(error_code)) {
    if (error_code_.empty()) {
        throw std::invalid_argument("scene renderer error code is required");
    }
}

const std::string& SceneRendererError::error_code() const noexcept { return error_code_; }

SceneRenderTransitionPreparation SceneRenderer::prepare_transition(
    const OutputBus bus,
    const std::shared_ptr<PreparedSceneRenderGraph>& incoming,
    const SceneTransitionSpec& transition) {
    static_cast<void>(bus);
    validate_scene_transition(transition);
    if (incoming == nullptr) {
        throw SceneRendererError{"renderer_preparation_failed",
                                 "The transition destination is unavailable"};
    }
    if (transition.kind == SceneTransitionKind::cut) {
        return {.effective_transition = transition};
    }
    return {
        .effective_transition = {},
        .fallback_applied = true,
        .fallback_reason = "transition_renderer_unavailable",
    };
}

} // namespace solin::media_engine
