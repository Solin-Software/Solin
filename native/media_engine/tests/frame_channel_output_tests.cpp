#include "solin/media_engine/frame_channel_output.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

class AdvancingRenderer final : public solin::media_engine::SceneRenderer {
  public:
    explicit AdvancingRenderer(const solin::media_engine::OutputBus bus) : bus_(bus) {}

    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    prepare(const solin::media_engine::SceneRenderPreparation&) override {
        return {};
    }

    [[nodiscard]] solin::media_engine::SceneRenderTransitionPreparation
    prepare_transition(
        solin::media_engine::OutputBus,
        const std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>&,
        const solin::media_engine::SceneTransitionSpec& transition) override {
        return {.effective_transition = transition};
    }

    void commit_hydration(
        const std::array<std::shared_ptr<
            solin::media_engine::PreparedSceneRenderGraph>,
                         2U>&,
        const std::array<bool, 2U>&, std::uint64_t) noexcept override {}

    void commit_take(
        solin::media_engine::OutputBus,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>,
        std::uint64_t) noexcept override {}

    void set_output_enabled(solin::media_engine::OutputBus, bool,
                            std::uint64_t) noexcept override {}

    void set_system_memory_output_enabled(
        const solin::media_engine::OutputBus bus,
        const solin::media_engine::SystemMemoryOutputConsumer consumer,
        const bool enabled) noexcept override {
        if (bus == bus_ &&
            consumer == solin::media_engine::SystemMemoryOutputConsumer::
                            frame_channel) {
            system_memory_output_enabled_.store(enabled);
        }
    }

    [[nodiscard]] bool system_memory_output_enabled() const noexcept {
        return system_memory_output_enabled_.load();
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(
        const solin::media_engine::OutputBus bus, std::uint64_t,
        const solin::media_engine::VideoFrameVisitor& visitor) const noexcept override {
        if (bus != bus_) {
            return std::nullopt;
        }
        try {
            const auto sequence = next_sequence_.fetch_add(1U) + 1U;
            const auto layout = solin::media_engine::packed_video_frame_layout(
                2U, 2U, solin::media_engine::VideoFramePixelFormat::bgra);
            const solin::media_engine::PackedVideoFrame frame{
                .sequence = sequence,
                .duration_ns = 16'666'667U,
                .width = layout.width,
                .height = layout.height,
                .pixel_format = layout.pixel_format,
                .plane_strides = layout.plane_strides,
                .plane_offsets = layout.plane_offsets,
                .bytes = std::vector<std::uint8_t>(layout.payload_size, 0x5AU),
            };
            visitor(solin::media_engine::video_frame_view(frame));
            return sequence;
        } catch (...) {
            return std::nullopt;
        }
    }

    [[nodiscard]] bool wait_for_frame(
        const solin::media_engine::OutputBus bus, std::uint64_t,
        const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        if (bus != bus_) {
            return false;
        }
        const auto cadence_deadline =
            std::chrono::steady_clock::now() + std::chrono::milliseconds{8};
        const auto effective_deadline =
            deadline < cadence_deadline ? deadline : cadence_deadline;
        std::mutex mutex;
        std::condition_variable_any wakeup;
        std::unique_lock lock{mutex};
        static_cast<void>(wakeup.wait_until(
            lock, stop_token, effective_deadline, [] { return false; }));
        return !stop_token.stop_requested();
    }

    void shutdown() noexcept override {}

  private:
    solin::media_engine::OutputBus bus_{solin::media_engine::OutputBus::media_windows};
    mutable std::atomic_uint64_t next_sequence_{0U};
    std::atomic_bool system_memory_output_enabled_{false};
};

void test_zero_cadence_is_rejected() {
    try {
        solin::media_engine::FrameChannelOutputController controller{
            std::make_shared<AdvancingRenderer>(
                solin::media_engine::OutputBus::media_windows),
            solin::media_engine::OutputBus::media_windows, 0U};
        expect(false, "a zero frame-channel cadence is rejected");
    } catch (const std::invalid_argument& error) {
        expect(std::string{error.what()} == "frame_channel_output_cadence_invalid",
               "an invalid frame-channel cadence has a stable error code");
    }
}

#ifdef _WIN32

template <typename Value>
void write_value(std::uint8_t* bytes, const std::size_t offset, const Value value) {
    std::memcpy(bytes + offset, &value, sizeof(Value));
}

template <typename Value>
[[nodiscard]] Value read_value(const std::uint8_t* bytes, const std::size_t offset) {
    Value value{};
    std::memcpy(&value, bytes + offset, sizeof(Value));
    return value;
}

class TestFrameChannel final {
  public:
    TestFrameChannel() {
        constexpr std::size_t header_size = 128U;
        constexpr std::size_t slot_header_size = 128U;
        constexpr std::size_t frame_bytes = 16U;
        constexpr std::size_t slot_size = slot_header_size + frame_bytes;
        constexpr std::size_t mapping_size = header_size + 3U * slot_size;
        static std::atomic_uint32_t suffix{0U};
        const auto process_id = GetCurrentProcessId();
        const auto tick_count = GetTickCount64();
        const auto channel_suffix = suffix.fetch_add(1U);
        const auto identity = std::to_wstring(process_id) + L"." +
                              std::to_wstring(tick_count) + L"." +
                              std::to_wstring(channel_suffix);
        mapping_name_ = L"SolinFrameOutputTest." + identity;
        mapping_token_ = "SolinFrameOutputTest." + std::to_string(process_id) +
                         "." + std::to_string(tick_count) + "." +
                         std::to_string(channel_suffix);
        mapping_ = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0U,
                                      static_cast<DWORD>(mapping_size),
                                      mapping_name_.c_str());
        const auto mutex_name = L"Local\\SolinFrameMutex." + mapping_name_;
        mutex_ = CreateMutexW(nullptr, FALSE, mutex_name.c_str());
        const auto event_name = L"Local\\SolinFrameEvent." + mapping_name_;
        event_ = CreateEventW(nullptr, FALSE, FALSE, event_name.c_str());
        view_ = static_cast<std::uint8_t*>(MapViewOfFile(
            mapping_, FILE_MAP_ALL_ACCESS, 0U, 0U, mapping_size));
        if (mapping_ == nullptr || mutex_ == nullptr || event_ == nullptr ||
            view_ == nullptr) {
            close();
            throw std::runtime_error("test_frame_channel_unavailable");
        }
        std::memset(view_, 0, mapping_size);
        constexpr std::array<std::uint8_t, 8U> magic{'S', 'L', 'N', 'F',
                                                     'R', 'M', '0', '1'};
        std::memcpy(view_, magic.data(), magic.size());
        write_value<std::uint16_t>(view_, 8U, 4U);
        write_value<std::uint16_t>(view_, 10U,
                                   static_cast<std::uint16_t>(header_size));
        write_value<std::uint32_t>(view_, 12U, 3U);
        write_value<std::uint32_t>(view_, 16U, 2U);
        write_value<std::uint32_t>(view_, 20U, 2U);
        write_value<std::uint32_t>(view_, 24U, 8U);
        write_value<std::uint32_t>(view_, 28U, 1U);
        write_value<std::uint64_t>(view_, 32U, 1U);
        write_value<std::uint64_t>(view_, 40U, slot_size);
        write_value<std::uint64_t>(view_, 48U, mapping_size);
    }

    ~TestFrameChannel() { close(); }

    TestFrameChannel(const TestFrameChannel&) = delete;
    TestFrameChannel& operator=(const TestFrameChannel&) = delete;

    [[nodiscard]] solin::media_engine::FrameChannelConfiguration configuration()
        const {
        return {
            .channel_id = "editor-preview",
            .generation = 1U,
            .producer_kind = "native_compositor",
            .transport = "shared_memory_bgra",
            .handle_token = mapping_token_,
            .width = 2U,
            .height = 2U,
            .pixel_format = "bgra",
            .color_space = "srgb",
            .color_range = "full",
        };
    }

    [[nodiscard]] std::uint64_t published_sequence() const {
        const auto wait_result = WaitForSingleObject(mutex_, INFINITE);
        if (wait_result != WAIT_OBJECT_0 && wait_result != WAIT_ABANDONED) {
            return 0U;
        }
        const auto sequence = read_value<std::uint64_t>(view_, 56U);
        static_cast<void>(ReleaseMutex(mutex_));
        return sequence;
    }

  private:
    void close() noexcept {
        if (view_ != nullptr) {
            static_cast<void>(UnmapViewOfFile(view_));
            view_ = nullptr;
        }
        if (mutex_ != nullptr) {
            static_cast<void>(CloseHandle(mutex_));
            mutex_ = nullptr;
        }
        if (event_ != nullptr) {
            static_cast<void>(CloseHandle(event_));
            event_ = nullptr;
        }
        if (mapping_ != nullptr) {
            static_cast<void>(CloseHandle(mapping_));
            mapping_ = nullptr;
        }
    }
    std::wstring mapping_name_{};
    std::string mapping_token_{};
    HANDLE mapping_{nullptr};
    HANDLE mutex_{nullptr};
    HANDLE event_{nullptr};
    std::uint8_t* view_{nullptr};
};

[[nodiscard]] solin::media_engine::SceneOutputDefinition preview_output() {
    return {
        .bus = solin::media_engine::OutputBus::media_windows,
        .default_scene_id = "scene-main",
        .video_format = {
            .width = 2U,
            .height = 2U,
            .fps_numerator = 60U,
            .fps_denominator = 1U,
            .pixel_format = "bgra",
            .color_space = "srgb",
            .color_range = "full",
        },
    };
}

[[nodiscard]] bool wait_for_first_frame(const TestFrameChannel& channel) {
    const auto deadline = std::chrono::steady_clock::now() + 500ms;
    while (std::chrono::steady_clock::now() < deadline) {
        if (channel.published_sequence() != 0U) {
            return true;
        }
        std::this_thread::sleep_for(1ms);
    }
    return false;
}

void test_preview_publication_samples_latest_frames_at_thirty_fps() {
    TestFrameChannel channel;
    auto renderer = std::make_shared<AdvancingRenderer>(
        solin::media_engine::OutputBus::media_windows);
    solin::media_engine::FrameChannelOutputController controller{
        renderer, solin::media_engine::OutputBus::media_windows, 30U};
    expect(controller.configure(channel.configuration(), preview_output()),
           "the capped editor channel configures");
    expect(controller.set_enabled(true), "the capped editor channel starts");
    expect(renderer->system_memory_output_enabled(),
           "a running frame channel requests system-memory output");
    expect(wait_for_first_frame(channel),
           "the cadence limiter publishes the first available frame immediately");
    std::this_thread::sleep_for(450ms);
    controller.shutdown();
    expect(!renderer->system_memory_output_enabled(),
           "stopping a frame channel releases system-memory output demand");
    const auto published = channel.published_sequence();
    expect(published >= 8U,
           "the editor channel continues publishing near its selected cadence");
    expect(published <= 16U,
           "a 60 fps compositor cannot drive the editor channel above 30 fps");
}

void test_uncapped_frame_channel_preserves_existing_behavior() {
    TestFrameChannel channel;
    auto renderer = std::make_shared<AdvancingRenderer>(
        solin::media_engine::OutputBus::media_windows);
    solin::media_engine::FrameChannelOutputController controller{
        renderer, solin::media_engine::OutputBus::media_windows};
    expect(controller.configure(channel.configuration(), preview_output()),
           "the uncapped frame channel configures");
    expect(controller.set_enabled(true), "the uncapped frame channel starts");
    expect(renderer->system_memory_output_enabled(),
           "an uncapped frame channel requests system-memory output");
    expect(wait_for_first_frame(channel),
           "the uncapped frame channel publishes its first frame");
    std::this_thread::sleep_for(450ms);
    controller.shutdown();
    expect(!renderer->system_memory_output_enabled(),
           "an uncapped frame channel releases system-memory output demand");
    const auto published = channel.published_sequence();
    if (published <= 16U) {
        std::cerr << "Observed uncapped publications: " << published << '\n';
    }
    expect(published > 16U,
           "omitting a cadence limit preserves the full latest-frame pump");
}

#endif

} // namespace

int main() {
    test_zero_cadence_is_rejected();
#ifdef _WIN32
    test_preview_publication_samples_latest_frames_at_thirty_fps();
    test_uncapped_frame_channel_preserves_existing_behavior();
#endif
    return failures == 0 ? 0 : 1;
}
