#include "native_window_output_routing.hpp"
#include "native_window_output_wait.hpp"

#include <chrono>
#include <iostream>

namespace {

using namespace std::chrono_literals;
using solin::media_engine::detail::NativeWindowWaitPlan;
using solin::media_engine::detail::native_window_wait_plan;
using solin::media_engine::detail::same_presenter_set;
using solin::media_engine::OutputBus;
using solin::media_engine::OutputWindowConfiguration;

int failures = 0;

void expect(const bool condition, const char* message) {
    if (!condition) {
        std::cerr << "FAILED: " << message << '\n';
        ++failures;
    }
}

void test_stable_mixed_targets_wait_for_activity_at_watchdog_cadence() {
    expect(native_window_wait_plan(true, true, false) ==
               NativeWindowWaitPlan{
                   .wait_for_activity = true,
                   .maximum_interval = 100ms,
               },
           "stable Raw and Program targets do not select transition cadence");
}

void test_active_transition_selects_animation_cadence() {
    expect(native_window_wait_plan(true, true, true) ==
               NativeWindowWaitPlan{
                   .wait_for_activity = true,
                   .maximum_interval = 16ms,
               },
           "an active presentation transition selects animation cadence");
}

void test_single_target_still_waits_for_frame_activity() {
    expect(native_window_wait_plan(true, false, false).wait_for_activity &&
               native_window_wait_plan(false, true, false).wait_for_activity,
           "either native output route waits for frame activity");
}

void test_empty_route_uses_only_the_health_watchdog() {
    expect(native_window_wait_plan(false, false, false) ==
               NativeWindowWaitPlan{
                   .wait_for_activity = false,
                   .maximum_interval = 100ms,
               },
           "an empty route has no frame activity source");
}

OutputWindowConfiguration target(
    const std::string& id,
    const std::uint64_t native_handle,
    const bool visible = true) {
    return OutputWindowConfiguration{
        .bus = OutputBus::media_windows,
        .target_id = id,
        .screen_id = "screen",
        .native_handle = native_handle,
        .x = 0,
        .y = 0,
        .width = 1280U,
        .height = 720U,
        .device_pixel_ratio = 1.0,
        .visible = visible,
    };
}

void test_stable_presenter_id_allows_hot_native_host_retargeting() {
    expect(same_presenter_set({target("operator", 1U)}, {target("operator", 2U)}),
           "a stable presenter id can move to a new native host without rebuilding");
}

void test_presenter_membership_and_visibility_changes_require_a_rebuild() {
    expect(!same_presenter_set({target("operator", 1U)}, {target("fullscreen", 2U)}),
           "changing presenter identity requires a rebuild");
    expect(!same_presenter_set({target("operator", 1U)},
                               {target("operator", 1U, false)}),
           "changing presenter visibility requires a rebuild");
    expect(!same_presenter_set({target("operator", 1U)},
                               {target("operator", 1U), target("second", 2U)}),
           "changing presenter membership requires a rebuild");
}

} // namespace

int main() {
    test_stable_mixed_targets_wait_for_activity_at_watchdog_cadence();
    test_active_transition_selects_animation_cadence();
    test_single_target_still_waits_for_frame_activity();
    test_empty_route_uses_only_the_health_watchdog();
    test_stable_presenter_id_allows_hot_native_host_retargeting();
    test_presenter_membership_and_visibility_changes_require_a_rebuild();

    if (failures != 0) {
        std::cerr << failures << " native window output wait test(s) failed\n";
        return 1;
    }
    std::cout << "native window output wait tests passed\n";
    return 0;
}
