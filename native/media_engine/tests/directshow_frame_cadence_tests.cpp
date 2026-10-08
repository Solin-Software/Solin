#include "frame_cadence.hpp"

#include <chrono>
#include <iostream>

namespace {

using solin::media_engine::windows_virtual_camera::DirectShowFrameCadence;
using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_normal_cadence_and_early_delivery() {
    DirectShowFrameCadence cadence;
    const auto epoch = DirectShowFrameCadence::Clock::time_point{123s};
    cadence.reset(epoch);
    auto now = epoch;
    for (int index = 0; index <= 300; ++index) {
        const auto deadline = cadence.next(now);
        const auto expected = epoch + std::chrono::nanoseconds{
            static_cast<std::int64_t>(index) * 1'000'000'000LL / 30LL};
        expect(deadline.due == expected && !deadline.discontinuity,
               "early and on-time requests retain exact 30 fps deadlines");
        now = deadline.due;
    }
}

void test_lateness_boundary_and_reset() {
    DirectShowFrameCadence cadence;
    const auto epoch = DirectShowFrameCadence::Clock::time_point{123s};
    constexpr auto period = 33'333'333ns;
    cadence.reset(epoch);
    const auto at_boundary = cadence.next(epoch + period);
    expect(at_boundary.due == epoch && !at_boundary.discontinuity,
           "one late slot does not rebase the capture schedule");
    cadence.reset(epoch);
    const auto after_boundary = cadence.next(epoch + period + 1ns);
    expect(after_boundary.due == epoch + period + 1ns &&
               after_boundary.discontinuity,
           "lateness beyond a slot rebases and marks discontinuity");
    cadence.reset(epoch + 10s);
    const auto restarted = cadence.next(epoch + 10s);
    expect(restarted.due == epoch + 10s && !restarted.discontinuity,
           "graph restart discards the previous schedule and pending slots");
}

void test_backpressure_drops_elapsed_slots_without_bursts() {
    DirectShowFrameCadence cadence;
    const auto epoch = DirectShowFrameCadence::Clock::time_point{123s};
    cadence.reset(epoch);
    static_cast<void>(cadence.next(epoch));
    const auto resumed = cadence.next(epoch + 120ms);
    expect(resumed.due == epoch + 120ms && resumed.discontinuity,
           "the delayed consumer gets one current sample with discontinuity");
    const auto next = cadence.next(resumed.due);
    expect(next.due == resumed.due + 33'333'333ns && !next.discontinuity,
           "the next sample waits a full slot instead of catching up in a burst");
    auto now = next.due;
    for (int index = 0; index < 40; ++index) {
        now += 80ms;
        const auto delayed = cadence.next(now);
        expect(delayed.due == now && delayed.discontinuity,
               "sustained slow consumption keeps dropping stale deadlines");
    }
}

} // namespace

int main() {
    test_normal_cadence_and_early_delivery();
    test_lateness_boundary_and_reset();
    test_backpressure_drops_elapsed_slots_without_bursts();
    return failures == 0 ? 0 : 1;
}
