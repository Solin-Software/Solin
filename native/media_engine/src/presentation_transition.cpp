#include "solin/media_engine/presentation_transition.hpp"

#include <algorithm>

namespace solin::media_engine {

PresentationReadinessWatchdog::PresentationReadinessWatchdog(
    const std::chrono::steady_clock::duration timeout) noexcept
    : timeout_{timeout} {}

void PresentationReadinessWatchdog::arm(
    const std::chrono::steady_clock::time_point now) noexcept {
    if (!armed_at_.has_value()) {
        armed_at_ = now;
    }
}

void PresentationReadinessWatchdog::acknowledge() noexcept { armed_at_.reset(); }

bool PresentationReadinessWatchdog::pending() const noexcept {
    return armed_at_.has_value();
}

bool PresentationReadinessWatchdog::timed_out(
    const std::chrono::steady_clock::time_point now) const noexcept {
    return armed_at_.has_value() && timeout_ > timeout_.zero() &&
           now >= *armed_at_ && now - *armed_at_ >= timeout_;
}

PresentationTransition::PresentationTransition(const SceneTransitionSpec transition)
    : transition_(transition) {
    validate_scene_transition(transition_);
}

void PresentationTransition::request(const PresentationIdentity presentation,
                                     const std::chrono::steady_clock::time_point now) noexcept {
    if (requested_ && presentation == desired_) {
        return;
    }
    if (!requested_) {
        requested_ = true;
        desired_ = presentation;
        phase_ = PresentationTransitionPhase::waiting_for_first_frame;
        return;
    }

    if (phase_ == PresentationTransitionPhase::waiting_at_black) {
        // The previous presentation is already fully hidden. Superseding an
        // idle/waiting request must remain black until the newest first frame,
        // never reveal the old frame and fade it out a second time.
        desired_ = presentation;
        incoming_observed_ = false;
        return;
    }

    std::optional<double> superseded_fade_in_alpha;
    // During the incoming half, the destination is the only visible texture
    // and becomes the origin for a superseding request. Preserve its current
    // alpha when reversing toward black so latest-request-wins cannot flash it
    // to full opacity for one frame.
    if (incoming_observed_ &&
        (phase_ == PresentationTransitionPhase::blending ||
         phase_ == PresentationTransitionPhase::fading_in)) {
        if (phase_ == PresentationTransitionPhase::fading_in &&
            transition_.kind == SceneTransitionKind::fade_to_black) {
            const auto half_duration = std::chrono::milliseconds{
                (std::max)(1U, transition_.duration_ms / 2U)};
            superseded_fade_in_alpha = phase_progress(now, half_duration);
        }
        current_ = desired_;
    }
    desired_ = presentation;
    incoming_observed_ = false;
    if (current_.has_value() && current_.value() == desired_) {
        phase_ = PresentationTransitionPhase::stable;
        return;
    }
    if (!current_.has_value()) {
        phase_ = PresentationTransitionPhase::waiting_for_first_frame;
        return;
    }
    if (transition_.kind == SceneTransitionKind::cut ||
        transition_.kind == SceneTransitionKind::dissolve) {
        phase_ = PresentationTransitionPhase::waiting_for_incoming;
    } else {
        phase_ = PresentationTransitionPhase::fading_out;
        if (superseded_fade_in_alpha.has_value()) {
            const auto half_duration = std::chrono::milliseconds{
                (std::max)(1U, transition_.duration_ms / 2U)};
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                half_duration * (1.0 - superseded_fade_in_alpha.value()));
            phase_started_at_ = now - elapsed;
        } else {
            phase_started_at_ = now;
        }
    }
}

PresentationTransitionSample PresentationTransition::sample(
    const std::chrono::steady_clock::time_point now,
    const bool incoming_frame_available) noexcept {
    incoming_observed_ = incoming_observed_ || incoming_frame_available;
    if (!requested_) {
        return {};
    }
    if (phase_ == PresentationTransitionPhase::waiting_for_first_frame) {
        if (incoming_frame_available) {
            commit_desired();
        }
        return {
            .outgoing = {},
            .incoming = incoming_frame_available ? std::optional{desired_} : std::nullopt,
            .weights = {.outgoing = 0.0, .incoming = incoming_frame_available ? 1.0 : 0.0},
            .phase = phase_,
        };
    }
    if (phase_ == PresentationTransitionPhase::stable) {
        return {
            .outgoing = {},
            .incoming = current_,
            .weights = {.outgoing = 0.0, .incoming = 1.0},
            .phase = phase_,
        };
    }
    if (phase_ == PresentationTransitionPhase::waiting_for_incoming) {
        if (!incoming_frame_available) {
            return {
                .outgoing = current_,
                .incoming = {},
                .weights = {.outgoing = 1.0, .incoming = 0.0},
                .phase = phase_,
            };
        }
        if (transition_.kind == SceneTransitionKind::cut) {
            commit_desired();
            return {
                .outgoing = {},
                .incoming = current_,
                .weights = {.outgoing = 0.0, .incoming = 1.0},
                .phase = phase_,
            };
        }
        phase_ = PresentationTransitionPhase::blending;
        phase_started_at_ = now;
    }

    const auto total_duration = std::chrono::milliseconds{transition_.duration_ms};
    if (phase_ == PresentationTransitionPhase::blending) {
        const auto progress = phase_progress(now, total_duration);
        if (progress >= 1.0) {
            commit_desired();
            return {
                .outgoing = {},
                .incoming = current_,
                .weights = {.outgoing = 0.0, .incoming = 1.0},
                .phase = phase_,
            };
        }
        return {
            .outgoing = current_,
            .incoming = desired_,
            .weights = scene_transition_weights(transition_, progress),
            .phase = phase_,
        };
    }

    const auto half_duration = std::chrono::milliseconds{
        (std::max)(1U, transition_.duration_ms / 2U)};
    if (phase_ == PresentationTransitionPhase::fading_out) {
        const auto progress = phase_progress(now, half_duration);
        if (progress < 1.0) {
            return {
                .outgoing = current_,
                .incoming = {},
                .weights = {.outgoing = 1.0 - progress, .incoming = 0.0},
                .phase = phase_,
            };
        }
        if (!incoming_frame_available) {
            phase_ = PresentationTransitionPhase::waiting_at_black;
            return {
                .outgoing = current_,
                .incoming = {},
                .weights = {.outgoing = 0.0, .incoming = 0.0},
                .phase = phase_,
            };
        }
        phase_ = PresentationTransitionPhase::fading_in;
        phase_started_at_ = now;
    }
    if (phase_ == PresentationTransitionPhase::waiting_at_black) {
        if (!incoming_frame_available) {
            return {
                .outgoing = current_,
                .incoming = {},
                .weights = {.outgoing = 0.0, .incoming = 0.0},
                .phase = phase_,
            };
        }
        phase_ = PresentationTransitionPhase::fading_in;
        phase_started_at_ = now;
    }
    const auto progress = phase_progress(now, half_duration);
    if (progress >= 1.0) {
        commit_desired();
        return {
            .outgoing = {},
            .incoming = current_,
            .weights = {.outgoing = 0.0, .incoming = 1.0},
            .phase = phase_,
        };
    }
    return {
        .outgoing = current_,
        .incoming = desired_,
        .weights = {.outgoing = 0.0, .incoming = progress},
        .phase = phase_,
    };
}

PresentationIdentity PresentationTransition::desired() const noexcept {
    return desired_;
}

std::optional<PresentationIdentity> PresentationTransition::current() const noexcept {
    return current_;
}

bool PresentationTransition::animation_active() const noexcept {
    return phase_ == PresentationTransitionPhase::fading_out ||
           phase_ == PresentationTransitionPhase::blending ||
           phase_ == PresentationTransitionPhase::fading_in;
}

void PresentationTransition::delay(
    const std::chrono::steady_clock::duration duration) noexcept {
    if (duration <= std::chrono::steady_clock::duration::zero()) {
        return;
    }
    if (phase_ == PresentationTransitionPhase::fading_out ||
        phase_ == PresentationTransitionPhase::blending ||
        phase_ == PresentationTransitionPhase::fading_in) {
        phase_started_at_ += duration;
    }
}

void PresentationTransition::restore_stable(
    const PresentationIdentity presentation) noexcept {
    requested_ = true;
    desired_ = presentation;
    current_ = presentation;
    phase_ = PresentationTransitionPhase::stable;
    phase_started_at_ = {};
    incoming_observed_ = false;
}

void PresentationTransition::commit_desired() noexcept {
    current_ = desired_;
    phase_ = PresentationTransitionPhase::stable;
    incoming_observed_ = false;
}

double PresentationTransition::phase_progress(
    const std::chrono::steady_clock::time_point now,
    const std::chrono::milliseconds duration) const noexcept {
    if (now <= phase_started_at_) {
        return 0.0;
    }
    return std::clamp(
        std::chrono::duration<double>(now - phase_started_at_).count() /
            std::chrono::duration<double>(duration).count(),
        0.0, 1.0);
}

} // namespace solin::media_engine
