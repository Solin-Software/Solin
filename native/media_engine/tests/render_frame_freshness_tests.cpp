#include "render_frame_freshness.hpp"

#include <cstdlib>
#include <iostream>

namespace {

void expect(const bool condition, const char* message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(EXIT_FAILURE);
    }
}

} // namespace

int main() {
    solin::media_engine::RenderFrameFreshness output;
    const auto initial_generation = output.generation();
    expect(output.acknowledge(100U) && output.ready(initial_generation),
           "a new graph accepts its ordinary first composition");

    output.invalidate();
    const auto resumed_generation = output.generation();
    expect(!output.ready(initial_generation) && !output.acknowledge(200U),
           "resume rejects both retained frames and callbacks before replay submission");

    output.submitted(300U);
    expect(!output.acknowledge(299U),
           "a late callback from an old queued buffer cannot satisfy the replay");
    expect(output.acknowledge(300U) && output.ready(resumed_generation),
           "the replay's composed frame becomes publicable");
    output.submitted(400U);
    expect(output.acknowledge(300U),
           "ordinary following submissions do not starve the acknowledged frame");

    output.invalidate();
    output.invalidate();
    const auto current_generation = output.generation();
    output.submitted(500U);
    expect(!output.ready(resumed_generation),
           "an old activation cannot become current merely by having a larger timestamp");
    expect(output.acknowledge(500U) && output.ready(current_generation),
           "coalesced demand changes converge on the current replay");
    return EXIT_SUCCESS;
}
