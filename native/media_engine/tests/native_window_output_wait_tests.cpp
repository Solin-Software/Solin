#include "native_window_output_routing.hpp"
#include "native_window_output_wait.hpp"

#include <chrono>
#include <iostream>

namespace {

using namespace std::chrono_literals;
using solin::media_engine::detail::native_window_routing_plan;
using solin::media_engine::detail::NativeWindowRoutingPlan;
using solin::media_engine::detail::NativeWindowWaitPlan;
using solin::media_engine::detail::native_window_wait_plan;
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

void test_stable_presenter_id_is_retained_across_native_host_changes() {
    expect(native_window_routing_plan({target("operator", 1U)},
                                      {target("operator", 2U)}) ==
               NativeWindowRoutingPlan{
                   .retained_target_ids = {"operator"},
               },
           "a stable presenter id is retained when its native host changes");
}

void test_operator_membership_changes_preserve_physical_presenters() {
    expect(native_window_routing_plan(
               {target("physical", 1U)},
               {target("physical", 1U), target("operator", 2U)}) ==
               NativeWindowRoutingPlan{
                   .retained_target_ids = {"physical"},
                   .added_target_ids = {"operator"},
               },
           "adding an operator presenter preserves the physical presenter");
    expect(native_window_routing_plan(
               {target("physical", 1U), target("operator", 2U)},
               {target("physical", 1U)}) ==
               NativeWindowRoutingPlan{
                   .retained_target_ids = {"physical"},
                   .removed_target_ids = {"operator"},
               },
           "removing an operator presenter preserves the physical presenter");
}

void test_identity_and_visibility_changes_are_scoped_per_presenter() {
    expect(native_window_routing_plan({target("operator", 1U)},
                                      {target("fullscreen", 2U)}) ==
               NativeWindowRoutingPlan{
                   .added_target_ids = {"fullscreen"},
                   .removed_target_ids = {"operator"},
               },
           "changing presenter identity replaces only that presenter");
    expect(native_window_routing_plan({target("operator", 1U)},
                                      {target("operator", 1U, false)}) ==
               NativeWindowRoutingPlan{
                   .removed_target_ids = {"operator"},
               },
           "hiding a presenter removes only that presenter");
}

} // namespace

int main() {
    test_stable_mixed_targets_wait_for_activity_at_watchdog_cadence();
    test_active_transition_selects_animation_cadence();
    test_single_target_still_waits_for_frame_activity();
    test_empty_route_uses_only_the_health_watchdog();
    test_stable_presenter_id_is_retained_across_native_host_changes();
    test_operator_membership_changes_preserve_physical_presenters();
    test_identity_and_visibility_changes_are_scoped_per_presenter();

    if (failures != 0) {
        std::cerr << failures << " native window output wait test(s) failed\n";
        return 1;
    }
    std::cout << "native window output wait tests passed\n";
    return 0;
}
