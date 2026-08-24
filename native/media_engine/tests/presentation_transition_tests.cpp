#include "solin/media_engine/presentation_transition.hpp"

#include <chrono>
#include <iostream>

namespace {

using namespace std::chrono_literals;
using solin::media_engine::PresentationIdentity;
using solin::media_engine::PresentationReadinessWatchdog;
using solin::media_engine::PresentationTransition;
using solin::media_engine::PresentationTransitionPhase;
using solin::media_engine::OutputBus;
using solin::media_engine::SceneTransitionKind;
using solin::media_engine::SceneTransitionSpec;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_fade_waits_at_black_for_the_requested_epoch() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    const PresentationIdentity first{OutputBus::media_windows, 1U};
    const PresentationIdentity second{OutputBus::media_windows, 2U};
    state.request(first, start);
    static_cast<void>(state.sample(start, true));
    state.request(second, start);

    const auto middle = state.sample(start + 100ms, false);
    expect(middle.phase == PresentationTransitionPhase::waiting_at_black &&
               middle.weights.outgoing == 0.0 && middle.weights.incoming == 0.0,
           "fade-to-black releases the stale epoch and waits at black");
    const auto incoming = state.sample(start + 500ms, true);
    expect(incoming.phase == PresentationTransitionPhase::fading_in &&
               incoming.incoming == second && incoming.weights.incoming == 0.0,
           "the first valid requested epoch starts the fade-in from black");
    const auto complete = state.sample(start + 600ms, true);
    expect(complete.phase == PresentationTransitionPhase::stable &&
               state.current() == second && complete.weights.incoming == 1.0,
           "the requested raw epoch becomes the stable presentation");
}

void test_program_frames_do_not_restart_a_stable_owner() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    const PresentationIdentity program{OutputBus::virtual_camera, 0U};
    state.request(program, start);
    static_cast<void>(state.sample(start, true));
    state.request(program, start + 10ms);
    const auto sample = state.sample(start + 10ms, true);
    expect(sample.phase == PresentationTransitionPhase::stable &&
               !sample.animation_active(),
           "ordinary Program frames do not restart the surface transition");
}

void test_latest_request_supersedes_a_pending_raw_epoch() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    state.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(state.sample(start, true));
    state.request({OutputBus::media_windows, 2U}, start);
    static_cast<void>(state.sample(start + 80ms, false));
    state.request({OutputBus::media_windows, 3U}, start + 80ms);
    static_cast<void>(state.sample(start + 180ms, true));
    static_cast<void>(state.sample(start + 280ms, true));
    expect(state.current() == PresentationIdentity{OutputBus::media_windows, 3U},
           "latest-request-wins promotes only the newest raw epoch");
}

void test_superseding_fade_in_reverses_without_an_opacity_flash() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    state.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(state.sample(start, true));
    state.request({OutputBus::media_windows, 2U}, start);
    static_cast<void>(state.sample(start + 100ms, true));
    const auto half_visible = state.sample(start + 150ms, true);
    state.request({OutputBus::media_windows, 3U}, start + 150ms);
    const auto reversed = state.sample(start + 150ms, true);

    expect(half_visible.weights.incoming == 0.5 &&
               reversed.outgoing == PresentationIdentity{OutputBus::media_windows, 2U} &&
               reversed.weights.outgoing == 0.5,
           "a superseded fade-in reverses from its current opacity without flashing");
}

void test_readiness_delay_stops_the_visual_clock() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    state.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(state.sample(start, true));
    state.request({OutputBus::media_windows, 2U}, start);
    state.delay(80ms);
    const auto held = state.sample(start + 80ms, false);
    expect(held.phase == PresentationTransitionPhase::fading_out &&
               held.weights.outgoing == 1.0,
           "an unready renderer cannot consume the fade duration as wall time");
    const auto progressed = state.sample(start + 130ms, false);
    expect(progressed.phase == PresentationTransitionPhase::fading_out &&
               held.weights.outgoing > progressed.weights.outgoing,
           "the fade clock advances after the renderer acknowledges output");
}

void test_output_watchdog_bounds_a_missing_gpu_acknowledgement() {
    PresentationReadinessWatchdog watchdog{500ms};
    const auto start = std::chrono::steady_clock::time_point{};

    watchdog.arm(start);
    watchdog.arm(start + 400ms);
    expect(watchdog.pending() && !watchdog.timed_out(start + 499ms),
           "retries do not slide the original GPU output deadline");
    expect(watchdog.timed_out(start + 500ms),
           "an accepted GPU input without output reaches a bounded deadline");

    watchdog.acknowledge();
    expect(!watchdog.pending() && !watchdog.timed_out(start + 5s),
           "an observed output disarms the GPU deadline");
}

void test_recovery_restores_the_visible_owner() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    const PresentationIdentity outgoing{OutputBus::media_windows, 41U};
    const PresentationIdentity failed{OutputBus::media_windows, 42U};
    const PresentationIdentity next{OutputBus::media_windows, 43U};

    state.request(outgoing, start);
    static_cast<void>(state.sample(start, true));
    state.request(failed, start);
    const auto at_black = state.sample(start + 100ms, false);
    expect(at_black.phase == PresentationTransitionPhase::waiting_at_black,
           "the failed destination reaches black while waiting for its first frame");

    state.restore_stable(outgoing);
    const auto recovered = state.sample(start + 5s, true);
    expect(recovered.phase == PresentationTransitionPhase::stable &&
               state.current() == std::optional{outgoing} &&
               state.desired() == outgoing,
           "recovery aligns the logical owner with the restored visible frame");

    state.request(next, start + 5s);
    const auto restarted = state.sample(start + 5s, false);
    expect(restarted.phase == PresentationTransitionPhase::fading_out &&
               restarted.outgoing == std::optional{outgoing},
           "the request after recovery starts from the restored owner");
}

void test_superseding_while_black_never_reveals_the_old_frame() {
    PresentationTransition state{{SceneTransitionKind::fade_to_black, 200U}};
    const auto start = std::chrono::steady_clock::time_point{};
    state.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(state.sample(start, true));
    state.request({OutputBus::media_windows, 2U}, start);
    static_cast<void>(state.sample(start + 100ms, false));
    state.request({OutputBus::media_windows, 3U}, start + 150ms);
    const auto held = state.sample(start + 150ms, false);
    expect(held.phase == PresentationTransitionPhase::waiting_at_black &&
               held.weights.outgoing == 0.0 && held.weights.incoming == 0.0,
           "a newer idle/raw request stays black instead of flashing the stale presentation");
    const auto incoming = state.sample(start + 200ms, true);
    expect(incoming.phase == PresentationTransitionPhase::fading_in &&
               incoming.incoming ==
                   PresentationIdentity{OutputBus::media_windows, 3U},
           "the newest request alone fades in after a black hold");
}

void test_dissolve_and_cut_share_the_surface_contract() {
    const auto start = std::chrono::steady_clock::time_point{};
    PresentationTransition dissolve{{SceneTransitionKind::dissolve, 200U}};
    dissolve.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(dissolve.sample(start, true));
    dissolve.request({OutputBus::virtual_camera, 0U}, start);
    const auto waiting = dissolve.sample(start + 50ms, false);
    expect(waiting.weights.outgoing == 1.0 && waiting.weights.incoming == 0.0,
           "dissolve retains the stable owner until Program has a frame");
    static_cast<void>(dissolve.sample(start + 50ms, true));
    const auto blend = dissolve.sample(start + 150ms, true);
    expect(blend.phase == PresentationTransitionPhase::blending &&
               blend.weights.outgoing == 0.5 && blend.weights.incoming == 0.5,
           "dissolve blends both GPU inputs through the common contract");

    PresentationTransition cut{{SceneTransitionKind::cut, 0U}};
    cut.request({OutputBus::media_windows, 1U}, start);
    static_cast<void>(cut.sample(start, true));
    cut.request({OutputBus::virtual_camera, 0U}, start);
    const auto switched = cut.sample(start, true);
    expect(switched.phase == PresentationTransitionPhase::stable &&
               cut.current() == PresentationIdentity{OutputBus::virtual_camera, 0U},
           "Cut uses the same owner switch state without animation");
}

} // namespace

int main() {
    test_fade_waits_at_black_for_the_requested_epoch();
    test_program_frames_do_not_restart_a_stable_owner();
    test_latest_request_supersedes_a_pending_raw_epoch();
    test_superseding_fade_in_reverses_without_an_opacity_flash();
    test_readiness_delay_stops_the_visual_clock();
    test_output_watchdog_bounds_a_missing_gpu_acknowledgement();
    test_recovery_restores_the_visible_owner();
    test_superseding_while_black_never_reveals_the_old_frame();
    test_dissolve_and_cut_share_the_surface_contract();
    if (failures == 0) {
        std::cout << "presentation transition tests passed\n";
    }
    return failures == 0 ? 0 : 1;
}
