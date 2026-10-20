#include "broker_client.hpp"

#include <array>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>

// Registration-free qualification bridge for a real out-of-process producer.
// The explicitly supplied pipe is local to the caller's integration fixture.
int wmain(const int argument_count, wchar_t** arguments) {
    using namespace solin::virtual_camera;
    using namespace solin::virtual_camera::windows_virtual_camera;
    if (argument_count < 2 || argument_count > 3) {
        std::cerr << "pipe-name [duration-ms] required\n";
        return 2;
    }
    try {
        const std::wstring pipe_name{arguments[1]};
        if (!pipe_name.starts_with(L"\\\\.\\pipe\\") || pipe_name.size() <= 9U) {
            return 2;
        }
        const auto duration_ms = argument_count == 3 ? std::stoul(arguments[2]) : 10000UL;
        if (duration_ms == 0UL || duration_ms > 60000UL) {
            return 2;
        }
        BrokerFrameProvider provider{pipe_name};
        provider.start();
        const auto deadline =
            std::chrono::steady_clock::now() + std::chrono::milliseconds{duration_ms};
        std::array<std::uint64_t, 3U> previous{};
        bool first = true;
        while (std::chrono::steady_clock::now() < deadline) {
            std::uint64_t sequence = 0U;
            std::array<unsigned int, 2U> sample{};
            const auto live = provider.visit_live_frame([&](const VideoFrameView& frame) {
                sequence = frame.sequence;
                for (std::size_t plane = 0U; plane < sample.size(); ++plane) {
                    if (!frame.planes[plane].empty()) {
                        sample[plane] = frame.planes[plane].front();
                    }
                }
            });
            const auto generation = live ? provider.generation() : 0U;
            if (!live) {
                sequence = 0U;
                sample = {};
            }
            const std::array<std::uint64_t, 3U> state{static_cast<std::uint64_t>(live), generation,
                                                      sequence};
            if (first || state != previous) {
                std::cout << "{\"live\":" << (live ? "true" : "false")
                          << ",\"generation\":" << generation << ",\"sequence\":" << sequence
                          << ",\"bytes\":[" << sample[0] << ',' << sample[1] << "]}\n"
                          << std::flush;
                previous = state;
                first = false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds{10});
        }
        provider.stop();
        return 0;
    } catch (...) {
        return 1;
    }
}
