#include "frame_adapter.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <span>
#include <string_view>
#include <vector>

namespace {

using namespace solin::media_engine;
using namespace solin::media_engine::windows_virtual_camera;

struct Nv12Fixture final {
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::vector<std::uint8_t> y{};
    std::vector<std::uint8_t> uv{};

    [[nodiscard]] VideoFrameView view() const noexcept {
        return {
            .width = width,
            .height = height,
            .pixel_format = VideoFramePixelFormat::nv12,
            .planes = {std::span<const std::uint8_t>{y},
                       std::span<const std::uint8_t>{uv}},
            .plane_strides = {static_cast<std::int32_t>(width),
                              static_cast<std::int32_t>(width)},
        };
    }
};

[[nodiscard]] bool benchmark(const std::string_view name,
                             const DirectShowMediaProfile profile,
                             const Nv12Fixture& fixture) {
    constexpr std::size_t warmup_frames = 30U;
    constexpr std::size_t measured_frames = 300U;
    constexpr std::int64_t maximum_p95_microseconds = 8'000;
    DirectShowFrameAdapter adapter{profile};
    std::vector<std::uint8_t> output(profile.sample_size());
    for (std::size_t index = 0U; index < warmup_frames; ++index) {
        if (!adapter.adapt(fixture.view(), output)) {
            return false;
        }
    }
    std::vector<std::int64_t> durations;
    durations.reserve(measured_frames);
    for (std::size_t index = 0U; index < measured_frames; ++index) {
        const auto started = std::chrono::steady_clock::now();
        if (!adapter.adapt(fixture.view(), output)) {
            return false;
        }
        durations.push_back(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - started)
                .count());
    }
    std::sort(durations.begin(), durations.end());
    const auto percentile = [&durations](const std::size_t numerator) {
        const auto index =
            (durations.size() - 1U) * numerator / static_cast<std::size_t>(100U);
        return durations[index];
    };
    const auto p95 = percentile(95U);
    std::cout << name << " p50_us=" << percentile(50U)
              << " p95_us=" << p95
              << " p99_us=" << percentile(99U) << '\n';
    if (p95 > maximum_p95_microseconds) {
        std::cerr << name << " exceeded p95 budget: " << p95 << " us > "
                  << maximum_p95_microseconds << " us\n";
        return false;
    }
    return true;
}

} // namespace

int main() {
    const Nv12Fixture source_1080{
        .width = 1920U,
        .height = 1080U,
        .y = std::vector<std::uint8_t>(1920U * 1080U, 96U),
        .uv = std::vector<std::uint8_t>(1920U * 1080U / 2U, 128U),
    };
    const Nv12Fixture source_720{
        .width = 1280U,
        .height = 720U,
        .y = std::vector<std::uint8_t>(1280U * 720U, 96U),
        .uv = std::vector<std::uint8_t>(1280U * 720U / 2U, 128U),
    };
    return benchmark("nv12-copy-1080p", kMediaProfiles[0U], source_1080) &&
                   benchmark("nv12-upscale-720p-to-1080p", kMediaProfiles[0U],
                             source_720) &&
                   benchmark("yuy2-convert-1080p", kMediaProfiles[4U],
                             source_1080)
               ? 0
               : 1;
}
