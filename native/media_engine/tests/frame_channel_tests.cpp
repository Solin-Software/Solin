#include "solin/media_engine/frame_channel.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_invalid_configuration_is_rejected_before_platform_dispatch() {
    solin::media_engine::FrameChannelConfiguration configuration{
        .channel_id = "content",
        .generation = 1U,
        .producer_kind = "solin_offscreen",
        .transport = "shared_memory_bgra",
        .handle_token = "mapping",
        .width = 1920U,
        .height = 1080U,
        .pixel_format = "nv12",
        .color_space = "bt709",
        .color_range = "limited",
    };
    try {
        static_cast<void>(solin::media_engine::make_frame_channel_reader(configuration));
        expect(false, "shared-memory transport rejects non-BGRA pixels");
    } catch (const std::runtime_error& error) {
        expect(std::string_view{error.what()} == "frame_channel_configuration_invalid",
               "invalid transport configuration has a stable error code");
    }
}

#ifdef _WIN32

class WindowsHandle final {
  public:
    explicit WindowsHandle(HANDLE value) : value_(value) {}
    ~WindowsHandle() {
        if (value_ != nullptr) {
            static_cast<void>(CloseHandle(value_));
        }
    }
    WindowsHandle(const WindowsHandle&) = delete;
    WindowsHandle& operator=(const WindowsHandle&) = delete;
    [[nodiscard]] HANDLE get() const noexcept { return value_; }

  private:
    HANDLE value_{nullptr};
};

template <typename Value>
void write_value(std::uint8_t* bytes, const std::size_t offset, const Value value) {
    std::memcpy(bytes + offset, &value, sizeof(Value));
}

void test_windows_reader_consumes_only_the_latest_complete_frame() {
    constexpr std::size_t header_size = 128U;
    constexpr std::size_t slot_header_size = 64U;
    constexpr std::size_t frame_bytes = 16U;
    constexpr std::size_t slot_size = slot_header_size + frame_bytes;
    constexpr std::size_t mapping_size = header_size + 3U * slot_size;
    const auto process_id = GetCurrentProcessId();
    const auto tick_count = GetTickCount64();
    const auto mapping_name =
        L"SolinFrameTest." + std::to_wstring(process_id) + L"." + std::to_wstring(tick_count);
    const auto mapping_token =
        "SolinFrameTest." + std::to_string(process_id) + "." + std::to_string(tick_count);
    const auto mutex_name = L"Local\\SolinFrameMutex." + mapping_name;
    const WindowsHandle mapping{CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                                   0U, static_cast<DWORD>(mapping_size),
                                                   mapping_name.c_str())};
    const WindowsHandle mutex{CreateMutexW(nullptr, FALSE, mutex_name.c_str())};
    auto* view = static_cast<std::uint8_t*>(
        MapViewOfFile(mapping.get(), FILE_MAP_ALL_ACCESS, 0U, 0U, mapping_size));
    expect(mapping.get() != nullptr && mutex.get() != nullptr && view != nullptr,
           "the test frame channel creates its named Windows resources");
    if (mapping.get() == nullptr || mutex.get() == nullptr || view == nullptr) {
        return;
    }
    std::memset(view, 0, mapping_size);
    constexpr std::array<std::uint8_t, 8U> magic{'S', 'L', 'N', 'F', 'R', 'M', '0', '1'};
    std::memcpy(view, magic.data(), magic.size());
    write_value<std::uint16_t>(view, 8U, 3U);
    write_value<std::uint16_t>(view, 10U, static_cast<std::uint16_t>(header_size));
    write_value<std::uint32_t>(view, 12U, 3U);
    write_value<std::uint32_t>(view, 16U, 2U);
    write_value<std::uint32_t>(view, 20U, 2U);
    write_value<std::uint32_t>(view, 24U, 8U);
    write_value<std::uint32_t>(view, 28U, 1U);
    write_value<std::uint64_t>(view, 32U, 7U);
    write_value<std::uint64_t>(view, 40U, slot_size);
    write_value<std::uint64_t>(view, 48U, mapping_size);

    auto reader = solin::media_engine::make_frame_channel_reader({
        .channel_id = "content",
        .generation = 7U,
        .producer_kind = "solin_offscreen",
        .transport = "shared_memory_bgra",
        .handle_token = mapping_token,
        .width = 2U,
        .height = 2U,
        .pixel_format = "bgra",
        .color_space = "srgb",
        .color_range = "full",
    });
    expect(!reader->read_latest().has_value(), "an initialized channel has no synthetic frame");

    const auto wait_result = WaitForSingleObject(mutex.get(), INFINITE);
    expect(wait_result == WAIT_OBJECT_0, "the test writer acquires the channel mutex");
    const auto slot = header_size;
    write_value<std::uint64_t>(view, slot, 1U);
    write_value<std::uint64_t>(view, slot + 8U, 1U);
    write_value<std::uint64_t>(view, slot + 16U, 123U);
    write_value<std::uint64_t>(view, slot + 24U, 456U);
    write_value<std::uint64_t>(view, slot + 32U, 789U);
    write_value<std::uint64_t>(view, slot + 40U, frame_bytes);
    write_value<std::uint32_t>(view, slot + 48U, 2U);
    write_value<std::uint32_t>(view, slot + 52U, 2U);
    write_value<std::uint32_t>(view, slot + 56U, 8U);
    write_value<std::uint32_t>(view, slot + 60U, 1U);
    for (std::size_t index = 0U; index < frame_bytes; ++index) {
        view[slot + slot_header_size + index] = static_cast<std::uint8_t>(index);
    }
    write_value<std::uint64_t>(view, slot, 2U);
    write_value<std::uint64_t>(view, 56U, 1U);
    static_cast<void>(ReleaseMutex(mutex.get()));

    const auto frame = reader->read_latest();
    expect(frame.has_value() && frame->sequence == 1U && frame->presentation_timestamp_ns == 123U &&
               frame->duration_ns == 456U && frame->width == 2U && frame->height == 2U &&
               frame->pixel_format == solin::media_engine::VideoFramePixelFormat::bgra &&
               frame->bytes.size() == frame_bytes && frame->bytes.front() == 0U &&
               frame->bytes.back() == 15U,
           "the reader returns the complete published frame and metadata");
    expect(!reader->read_latest().has_value(),
           "the latest-frame reader does not redeliver a consumed sequence");

    const auto second_wait_result = WaitForSingleObject(mutex.get(), INFINITE);
    expect(second_wait_result == WAIT_OBJECT_0, "the test writer reacquires the channel mutex");
    const auto second_slot = header_size + slot_size;
    write_value<std::uint64_t>(view, second_slot, 3U);
    write_value<std::uint64_t>(view, second_slot + 8U, 2U);
    write_value<std::uint64_t>(view, second_slot + 16U, 321U);
    write_value<std::uint64_t>(view, second_slot + 24U, 654U);
    write_value<std::uint64_t>(view, second_slot + 32U, 987U);
    write_value<std::uint64_t>(view, second_slot + 40U, 4U);
    write_value<std::uint32_t>(view, second_slot + 48U, 1U);
    write_value<std::uint32_t>(view, second_slot + 52U, 1U);
    write_value<std::uint32_t>(view, second_slot + 56U, 4U);
    write_value<std::uint32_t>(view, second_slot + 60U, 1U);
    std::memset(view + second_slot + slot_header_size, 0xCD, 4U);
    write_value<std::uint64_t>(view, second_slot, 4U);
    write_value<std::uint64_t>(view, 56U, 2U);
    static_cast<void>(ReleaseMutex(mutex.get()));

    const auto smaller_frame = reader->read_latest();
    expect(smaller_frame.has_value() && smaller_frame->sequence == 2U &&
               smaller_frame->width == 1U && smaller_frame->height == 1U &&
               smaller_frame->bytes.size() == 4U && smaller_frame->bytes.front() == 0xCDU,
           "the reader accepts a frame smaller than the channel capacity");

    auto writer = solin::media_engine::make_frame_channel_writer({
        .channel_id = "media-output",
        .generation = 7U,
        .producer_kind = "native_compositor",
        .transport = "shared_memory_bgra",
        .handle_token = mapping_token,
        .width = 2U,
        .height = 2U,
        .pixel_format = "bgra",
        .color_space = "srgb",
        .color_range = "full",
    });
    solin::media_engine::PackedVideoFrame output_frame{
        .sequence = 9U,
        .presentation_timestamp_ns = 1'000U,
        .duration_ns = 2'000U,
        .produced_monotonic_ns = 3'000U,
        .width = 2U,
        .height = 2U,
        .pixel_format = solin::media_engine::VideoFramePixelFormat::bgra,
        .plane_strides = {8U, 0U},
        .plane_offsets = {0U, 0U},
        .bytes = std::vector<std::uint8_t>(frame_bytes, 0xABU),
    };
    expect(writer->publish(solin::media_engine::video_frame_view(output_frame)),
           "the native writer publishes into a consumer-owned channel");
    const auto native_frame = reader->read_latest();
    expect(native_frame.has_value() && native_frame->sequence == 3U &&
               native_frame->presentation_timestamp_ns == 1'000U && native_frame->width == 2U &&
               native_frame->height == 2U && native_frame->bytes.front() == 0xABU,
           "the consumer reads the native writer frame without control-pipe payloads");
    static_cast<void>(UnmapViewOfFile(view));
}

#endif

} // namespace

int main() {
    test_invalid_configuration_is_rejected_before_platform_dispatch();
#ifdef _WIN32
    test_windows_reader_consumes_only_the_latest_complete_frame();
#endif
    return failures == 0 ? 0 : 1;
}
