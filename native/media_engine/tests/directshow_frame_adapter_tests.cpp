#include "frame_adapter.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdlib>
#include <cstdint>
#include <iostream>
#include <limits>
#include <new>
#include <span>
#include <vector>

namespace {
std::atomic_bool track_allocations{false};
std::atomic_size_t tracked_allocations{0U};
}

void* operator new(const std::size_t size) {
    if (track_allocations.load(std::memory_order_relaxed)) {
        tracked_allocations.fetch_add(1U, std::memory_order_relaxed);
    }
    if (auto* memory = std::malloc(size); memory != nullptr) {
        return memory;
    }
    throw std::bad_alloc{};
}

void* operator new[](const std::size_t size) { return ::operator new(size); }
void operator delete(void* const memory) noexcept { std::free(memory); }
void operator delete[](void* const memory) noexcept { std::free(memory); }
void operator delete(void* const memory, std::size_t) noexcept {
    std::free(memory);
}
void operator delete[](void* const memory, std::size_t) noexcept {
    std::free(memory);
}

namespace {

using namespace solin::media_engine;
using namespace solin::media_engine::windows_virtual_camera;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

struct Nv12Fixture final {
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::int32_t stride{0};
    std::vector<std::uint8_t> y{};
    std::vector<std::uint8_t> uv{};

    [[nodiscard]] VideoFrameView view() const noexcept {
        return {
            .sequence = 1U,
            .width = width,
            .height = height,
            .pixel_format = VideoFramePixelFormat::nv12,
            .planes = {std::span<const std::uint8_t>{y},
                       std::span<const std::uint8_t>{uv}},
            .plane_strides = {stride, stride},
        };
    }
};

void test_profile_table_is_exact_and_extensible() {
    constexpr std::array<std::array<std::uint32_t, 2U>, 4U> sizes{{
        {1920U, 1080U}, {1280U, 720U}, {640U, 360U}, {640U, 480U}}};
    expect(kMediaProfiles.size() == 8U,
           "the filter advertises exactly eight profiles");
    for (std::size_t format = 0U; format < 2U; ++format) {
        const auto pixel_format = format == 0U ? DirectShowPixelFormat::nv12
                                               : DirectShowPixelFormat::yuy2;
        for (std::size_t size = 0U; size < sizes.size(); ++size) {
            const auto& profile = kMediaProfiles[format * sizes.size() + size];
            expect(profile.width == sizes[size][0] &&
                       profile.height == sizes[size][1] &&
                       profile.pixel_format == pixel_format &&
                       profile.fps_numerator == 30U &&
                       profile.fps_denominator == 1U,
                   "profile order and cadence are stable");
            const auto bytes_per_pixel_numerator =
                pixel_format == DirectShowPixelFormat::nv12 ? 3U : 2U;
            const auto bytes_per_pixel_denominator =
                pixel_format == DirectShowPixelFormat::nv12 ? 2U : 1U;
            expect(profile.sample_size() ==
                       static_cast<std::size_t>(profile.width) * profile.height *
                           bytes_per_pixel_numerator /
                           bytes_per_pixel_denominator,
                   "profile sample size matches its packed layout");
            expect(find_media_profile(profile.width, profile.height, pixel_format,
                                      30U, 1U) == &profile,
                   "profile lookup returns the single table entry");
        }
    }
    expect(find_media_profile(3840U, 2160U, DirectShowPixelFormat::nv12, 30U,
                              1U) == nullptr &&
               find_media_profile(1920U, 1080U,
                                  DirectShowPixelFormat::nv12, 60U,
                                  1U) == nullptr,
           "unadvertised resolutions and frame rates are rejected");
}

void test_rational_timestamps_have_no_accumulated_drift() {
    expect(directshow_frame_time_100ns(0U) == 0 &&
               directshow_frame_time_100ns(1U) == 333'333 &&
               directshow_frame_time_100ns(2U) == 666'666 &&
               directshow_frame_time_100ns(3U) == 1'000'000 &&
               directshow_frame_time_100ns(30U) == 10'000'000,
           "30 fps timestamps use the exact rational timeline");
    std::int64_t accumulated = 0;
    for (std::uint64_t index = 0U; index < 300U; ++index) {
        const auto duration = directshow_frame_time_100ns(index + 1U) -
                              directshow_frame_time_100ns(index);
        expect(duration == 333'333 || duration == 333'334,
               "frame durations alternate without jitter outside one tick");
        accumulated += duration;
    }
    expect(accumulated == 100'000'000,
           "ten seconds of timestamps have zero accumulated drift");
    expect(directshow_frame_time_100ns(
               (std::numeric_limits<std::uint64_t>::max)()) ==
               (std::numeric_limits<std::int64_t>::max)(),
           "extreme frame indices saturate instead of overflowing");
}

void test_direct_nv12_copy_honors_source_strides() {
    constexpr auto profile = kMediaProfiles[2U];
    constexpr std::int32_t stride = 672;
    Nv12Fixture fixture{
        .width = profile.width,
        .height = profile.height,
        .stride = stride,
        .y = std::vector<std::uint8_t>(
            static_cast<std::size_t>(stride) * profile.height, 0xEEU),
        .uv = std::vector<std::uint8_t>(
            static_cast<std::size_t>(stride) * profile.height / 2U, 0xDDU),
    };
    for (std::uint32_t row = 0U; row < profile.height; ++row) {
        std::fill_n(fixture.y.data() + static_cast<std::size_t>(row) * stride,
                    profile.width, static_cast<std::uint8_t>(row % 251U));
    }
    for (std::uint32_t row = 0U; row < profile.height / 2U; ++row) {
        std::fill_n(fixture.uv.data() + static_cast<std::size_t>(row) * stride,
                    profile.width,
                    static_cast<std::uint8_t>(64U + row % 127U));
    }
    DirectShowFrameAdapter adapter{profile};
    std::vector<std::uint8_t> output(profile.sample_size());
    expect(adapter.write_direct_nv12(fixture.view(), output),
            "an exact padded NV12 producer frame writes directly");
    bool rows_match = true;
    for (std::uint32_t row = 0U; row < profile.height; ++row) {
        rows_match = rows_match &&
                     std::equal(output.begin() +
                                    static_cast<std::ptrdiff_t>(row) *
                                        profile.width,
                                output.begin() +
                                    static_cast<std::ptrdiff_t>(row + 1U) *
                                        profile.width,
                                fixture.y.begin() +
                                    static_cast<std::ptrdiff_t>(row) * stride);
    }
    expect(rows_match, "direct NV12 copy strips source row padding");

    fixture.width -= 2U;
    expect(!adapter.write_direct_nv12(fixture.view(), output),
           "the direct path rejects frames that require scaling");
    DirectShowFrameAdapter yuy2_adapter{kMediaProfiles[6U]};
    fixture.width = profile.width;
    expect(!yuy2_adapter.write_direct_nv12(fixture.view(), output),
           "the direct path rejects profiles that require conversion");
}

void test_scaling_preserves_aspect_and_uses_limited_black() {
    constexpr auto profile = kMediaProfiles[2U];
    Nv12Fixture fixture{
        .width = 8U,
        .height = 4U,
        .stride = 8,
        .y = std::vector<std::uint8_t>(32U, 120U),
        .uv = std::vector<std::uint8_t>(16U, 128U),
    };
    DirectShowFrameAdapter adapter{profile};
    std::vector<std::uint8_t> output(profile.sample_size());
    expect(adapter.adapt(fixture.view(), output),
           "a valid differently sized producer frame is scaled");
    const auto first_row = std::span<const std::uint8_t>{output}.first(profile.width);
    const auto center_row = std::span<const std::uint8_t>{output}.subspan(
        static_cast<std::size_t>(profile.height / 2U) * profile.width,
        profile.width);
    expect(std::all_of(first_row.begin(), first_row.end(),
                       [](const std::uint8_t value) { return value == 16U; }),
           "letterbox bars use limited-range luma black");
    expect(std::any_of(center_row.begin(), center_row.end(),
                       [](const std::uint8_t value) { return value > 16U; }),
           "scaled content remains centered inside the letterbox");
}

void test_yuy2_conversion_and_malformed_plane_rejection() {
    constexpr auto profile = kMediaProfiles[7U];
    Nv12Fixture fixture{
        .width = profile.width,
        .height = profile.height,
        .stride = static_cast<std::int32_t>(profile.width),
        .y = std::vector<std::uint8_t>(
            static_cast<std::size_t>(profile.width) * profile.height, 100U),
        .uv = std::vector<std::uint8_t>(
            static_cast<std::size_t>(profile.width) * profile.height / 2U,
            128U),
    };
    DirectShowFrameAdapter adapter{profile};
    std::vector<std::uint8_t> output(profile.sample_size());
    expect(adapter.adapt(fixture.view(), output) && output[0] == 100U &&
               output[1] == 128U && output[2] == 100U && output[3] == 128U,
           "NV12 converts to byte-exact neutral YUY2 pairs");
    fixture.uv.resize(fixture.uv.size() - 1U);
    expect(!adapter.adapt(fixture.view(), output),
           "a truncated chroma plane is rejected before scaling");
}

void test_steady_state_adaptation_does_not_allocate() {
    constexpr auto profile = kMediaProfiles[0U];
    Nv12Fixture fixture{
        .width = 1280U,
        .height = 720U,
        .stride = 1280,
        .y = std::vector<std::uint8_t>(1280U * 720U, 90U),
        .uv = std::vector<std::uint8_t>(1280U * 720U / 2U, 128U),
    };
    DirectShowFrameAdapter adapter{profile};
    std::vector<std::uint8_t> output(profile.sample_size());
    tracked_allocations.store(0U, std::memory_order_relaxed);
    track_allocations.store(true, std::memory_order_release);
    bool adapted = true;
    for (int frame = 0; frame < 120; ++frame) {
        adapted = adapter.adapt(fixture.view(), output) && adapted;
    }
    track_allocations.store(false, std::memory_order_release);
    expect(adapted && tracked_allocations.load(std::memory_order_relaxed) == 0U,
           "steady-state scaling performs no owning allocation per frame");
}

void test_standby_is_available_for_every_profile() {
    for (const auto& profile : kMediaProfiles) {
        DirectShowFrameAdapter adapter{profile};
        std::vector<std::uint8_t> output(profile.sample_size());
        expect(adapter.write_standby(output),
               "every negotiated profile has an offline standby frame");
    }
}

} // namespace

int main() {
    test_profile_table_is_exact_and_extensible();
    test_rational_timestamps_have_no_accumulated_drift();
    test_direct_nv12_copy_honors_source_strides();
    test_scaling_preserves_aspect_and_uses_limited_black();
    test_yuy2_conversion_and_malformed_plane_rejection();
    test_steady_state_adaptation_does_not_allocate();
    test_standby_is_available_for_every_profile();
    return failures == 0 ? 0 : 1;
}
