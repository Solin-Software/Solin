#pragma once

#include "solin/media_engine/scene_renderer.hpp"

#include <chrono>
#include <cstdint>
#include <optional>

namespace solin::media_engine {

// Code-owned policy until a persisted media-output setting is introduced.
// Raw content changes and physical Raw/Program ownership consume this single
// source of truth; authored Program scene transitions remain independent.
inline constexpr SceneTransitionSpec kMediaPresentationTransition{
    .kind = SceneTransitionKind::fade_to_black,
    .duration_ms = 200U,
};

// Ephemeral GPU graphs are asynchronous: accepting an input frame does not
// guarantee that their appsink will ever acknowledge an output. Keep this
// deadline longer than an ordinary transition, but bounded so a stalled device
// cannot become the permanent owner of the canonical Raw presentation.
inline constexpr auto kPresentationOutputTimeout = std::chrono::milliseconds{500};
inline constexpr auto kPresentationFirstFrameTimeout = std::chrono::seconds{5};

class PresentationReadinessWatchdog final {
  public:
    explicit PresentationReadinessWatchdog(
        std::chrono::steady_clock::duration timeout =
            kPresentationOutputTimeout) noexcept;

    void arm(std::chrono::steady_clock::time_point now) noexcept;
    void acknowledge() noexcept;
    [[nodiscard]] bool pending() const noexcept;
    [[nodiscard]] bool timed_out(
        std::chrono::steady_clock::time_point now) const noexcept;

  private:
    std::chrono::steady_clock::duration timeout_{};
    std::optional<std::chrono::steady_clock::time_point> armed_at_{};
};

struct PresentationIdentity final {
    OutputBus bus{OutputBus::media_windows};
    // Meaningful only for Raw. Program is already a composed continuous stream,
    // so ordinary Program frames must not create new surface transitions.
    std::uint64_t media_epoch{0U};

    bool operator==(const PresentationIdentity&) const = default;
};

enum class PresentationTransitionPhase : std::uint8_t {
    waiting_for_first_frame,
    stable,
    waiting_for_incoming,
    fading_out,
    waiting_at_black,
    blending,
    fading_in,
};

struct PresentationTransitionSample final {
    std::optional<PresentationIdentity> outgoing{};
    std::optional<PresentationIdentity> incoming{};
    SceneTransitionWeights weights{};
    PresentationTransitionPhase phase{
        PresentationTransitionPhase::waiting_for_first_frame};

    [[nodiscard]] bool animation_active() const noexcept {
        return phase == PresentationTransitionPhase::fading_out ||
               phase == PresentationTransitionPhase::blending ||
               phase == PresentationTransitionPhase::fading_in;
    }
};

// Pure, clock-driven ownership state machine. It deliberately knows nothing
// about GStreamer or windows so rapid supersession and delayed first frames can
// be tested without a GPU or a desktop session.
class PresentationTransition final {
  public:
    explicit PresentationTransition(SceneTransitionSpec transition);

    void request(PresentationIdentity presentation,
                 std::chrono::steady_clock::time_point now) noexcept;
    [[nodiscard]] PresentationTransitionSample
    sample(std::chrono::steady_clock::time_point now,
           bool incoming_frame_available) noexcept;
    // Readiness is owned by the renderer. Moving the phase origin keeps the
    // visual clock stopped while a GPU graph has not acknowledged its previous
    // frame, instead of allowing wall time to skip the effect.
    void delay(std::chrono::steady_clock::duration duration) noexcept;
    // Re-establishes the presentation that recovery actually made visible.
    // This keeps the logical owner aligned with the screen after a readiness
    // timeout, so a later request starts a normal transition from that owner.
    void restore_stable(PresentationIdentity presentation) noexcept;

    [[nodiscard]] PresentationIdentity desired() const noexcept;
    [[nodiscard]] std::optional<PresentationIdentity> current() const noexcept;
    [[nodiscard]] bool animation_active() const noexcept;

  private:
    void commit_desired() noexcept;
    [[nodiscard]] double phase_progress(
        std::chrono::steady_clock::time_point now,
        std::chrono::milliseconds duration) const noexcept;

    SceneTransitionSpec transition_{};
    PresentationIdentity desired_{};
    std::optional<PresentationIdentity> current_{};
    PresentationTransitionPhase phase_{
        PresentationTransitionPhase::waiting_for_first_frame};
    std::chrono::steady_clock::time_point phase_started_at_{};
    bool requested_{false};
    bool incoming_observed_{false};
};

} // namespace solin::media_engine
