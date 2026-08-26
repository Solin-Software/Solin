#include "solin/media_engine/virtual_camera_output.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

namespace {

using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

struct SinkState final {
    std::mutex mutex{};
    std::condition_variable wakeup{};
    std::optional<solin::media_engine::VirtualCameraConfiguration> configuration{};
    std::uint64_t published_frames{0U};
    std::uint64_t rejected_publications_remaining{0U};
    std::uint64_t consumer_probes{0U};
    bool consumer_present{false};
    bool started{false};
};

class FakeVirtualCameraSink final
    : public solin::media_engine::VirtualCameraSink {
  public:
    explicit FakeVirtualCameraSink(std::shared_ptr<SinkState> state)
        : state_(std::move(state)) {}

    void start() override {
        std::scoped_lock lock{state_->mutex};
        state_->started = true;
    }

    [[nodiscard]] bool has_consumer() const noexcept override {
        std::scoped_lock lock{state_->mutex};
        ++state_->consumer_probes;
        return state_->consumer_present;
    }

    [[nodiscard]] bool publish(
        const solin::media_engine::VideoFrameView& frame) noexcept override {
        std::scoped_lock lock{state_->mutex};
        if (!state_->started ||
            frame.pixel_format !=
                solin::media_engine::VideoFramePixelFormat::nv12) {
            return false;
        }
        if (state_->rejected_publications_remaining != 0U) {
            --state_->rejected_publications_remaining;
            state_->wakeup.notify_all();
            return false;
        }
        ++state_->published_frames;
        state_->wakeup.notify_all();
        return true;
    }

    void stop() noexcept override {
        std::scoped_lock lock{state_->mutex};
        state_->started = false;
    }

    [[nodiscard]] solin::media_engine::VirtualCameraSinkHealth health()
        const override {
        std::scoped_lock lock{state_->mutex};
        return {
            .state = state_->started
                         ? solin::media_engine::VirtualCameraSinkState::ready
                         : solin::media_engine::VirtualCameraSinkState::stopped,
            .published_frames = state_->published_frames,
        };
    }

  private:
    std::shared_ptr<SinkState> state_{};
};

class FakeVirtualCameraBackend final
    : public solin::media_engine::VirtualCameraBackend {
  public:
    explicit FakeVirtualCameraBackend(std::shared_ptr<SinkState> state)
        : state_(std::move(state)) {}

    [[nodiscard]] solin::media_engine::VirtualCameraBackendProbe probe()
        const override {
        return {
            .backend = solin::media_engine::VirtualCameraBackendKind::
                windows_directshow,
            .platform_supported = true,
            .filter_registered_x86 = true,
            .filter_registered_x64 = true,
            .cross_process_transport_available = true,
            .operational = true,
            .error_code = {},
        };
    }

    [[nodiscard]] std::unique_ptr<solin::media_engine::VirtualCameraSink>
    create_sink(const solin::media_engine::VirtualCameraConfiguration&
                    configuration) override {
        {
            std::scoped_lock lock{state_->mutex};
            state_->configuration = configuration;
        }
        return std::make_unique<FakeVirtualCameraSink>(state_);
    }

  private:
    std::shared_ptr<SinkState> state_{};
};

class FakeRenderer final : public solin::media_engine::SceneRenderer {
  public:
    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    prepare(const solin::media_engine::SceneRenderPreparation& preparation) override {
        static_cast<void>(preparation);
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
                         2U>& graphs,
        const std::array<bool, 2U>& enabled,
        const std::uint64_t sequence) noexcept override {
        static_cast<void>(graphs);
        static_cast<void>(enabled);
        static_cast<void>(sequence);
    }

    void commit_take(
        const solin::media_engine::OutputBus bus,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> graph,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> transition,
        const std::uint64_t sequence) noexcept override {
        static_cast<void>(bus);
        static_cast<void>(graph);
        static_cast<void>(transition);
        static_cast<void>(sequence);
    }

    void set_output_enabled(const solin::media_engine::OutputBus bus,
                            const bool enabled,
                            const std::uint64_t sequence) noexcept override {
        static_cast<void>(bus);
        static_cast<void>(enabled);
        static_cast<void>(sequence);
    }

    void set_system_memory_output_enabled(
        const solin::media_engine::OutputBus bus,
        const solin::media_engine::SystemMemoryOutputConsumer consumer,
        const bool enabled) noexcept override {
        if (bus == solin::media_engine::OutputBus::virtual_camera &&
            consumer == solin::media_engine::SystemMemoryOutputConsumer::
                            virtual_camera) {
            system_memory_output_enabled_.store(enabled);
        }
    }

    [[nodiscard]] bool system_memory_output_enabled() const noexcept {
        return system_memory_output_enabled_.load();
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(const solin::media_engine::OutputBus bus) const noexcept override {
        static_cast<void>(bus);
        return {};
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame(const solin::media_engine::OutputBus bus) const noexcept override {
        static_cast<void>(bus);
        return {};
    }

    [[nodiscard]] std::optional<solin::media_engine::SceneOutputFrameCursor>
    visit_latest_frame(
        const solin::media_engine::OutputBus bus,
        const solin::media_engine::SceneOutputFrameCursor after,
        const solin::media_engine::VideoFrameVisitor& visitor) const noexcept override {
        if (bus != solin::media_engine::OutputBus::virtual_camera ||
            after.frame_sequence >= 1U) {
            return std::nullopt;
        }
        const auto layout = solin::media_engine::packed_video_frame_layout(
            4U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
        const solin::media_engine::PackedVideoFrame frame{
            .sequence = 1U,
            .duration_ns = 16'666'667U,
            .width = layout.width,
            .height = layout.height,
            .pixel_format = layout.pixel_format,
            .plane_strides = layout.plane_strides,
            .plane_offsets = layout.plane_offsets,
            .bytes = std::vector<std::uint8_t>(layout.payload_size, 0x80U),
        };
        visitor(solin::media_engine::video_frame_view(frame));
        return solin::media_engine::SceneOutputFrameCursor{
            .route_generation = 1U,
            .frame_sequence = frame.sequence,
        };
    }

    [[nodiscard]] bool wait_for_frame(
        solin::media_engine::OutputBus,
        solin::media_engine::SceneOutputFrameCursor,
        const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        std::mutex mutex;
        std::condition_variable_any wakeup;
        std::unique_lock lock{mutex};
        static_cast<void>(wakeup.wait_until(
            lock, stop_token, deadline, [] { return false; }));
        return false;
    }

    void shutdown() noexcept override {}

  private:
    std::atomic_bool system_memory_output_enabled_{false};
};

[[nodiscard]] solin::media_engine::SceneOutputDefinition test_output() {
    return {
        .bus = solin::media_engine::OutputBus::virtual_camera,
        .default_scene_id = "scene-main",
        .video_format = {
            .width = 4U,
            .height = 2U,
            .fps_numerator = 60U,
            .fps_denominator = 1U,
            .pixel_format = "bgra",
            .color_space = "srgb",
            .color_range = "full",
        },
    };
}

void test_controller_normalizes_and_pumps_the_virtual_camera_bus() {
    auto state = std::make_shared<SinkState>();
    state->rejected_publications_remaining = 1U;
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::VirtualCameraOutputController controller{
        renderer, std::make_unique<FakeVirtualCameraBackend>(state)};
    const auto output = test_output();
    expect(controller.configure(output),
           "a virtual-camera output route configures without side effects");
    expect(controller.set_enabled(true),
           "enabling the output starts the selected platform backend");
    expect(!renderer->system_memory_output_enabled(),
           "an enabled camera without a consumer keeps GPU readback closed");
    std::this_thread::sleep_for(160ms);
    {
        std::scoped_lock lock{state->mutex};
        expect(state->consumer_probes <= 6U,
               "an idle camera probes demand at a bounded cadence instead of "
               "spinning on newer compositor frames");
    }
    {
        std::scoped_lock lock{state->mutex};
        state->consumer_present = true;
    }
    {
        std::unique_lock lock{state->mutex};
        static_cast<void>(state->wakeup.wait_for(lock, 1s, [state] {
            return state->published_frames != 0U;
        }));
        expect(state->configuration.has_value() &&
                   state->configuration->video_format.pixel_format == "nv12" &&
                   state->configuration->video_format.color_space == "bt709" &&
                   state->configuration->video_format.color_range == "limited",
               "the OS boundary receives the canonical NV12 BT.709 format");
        expect(state->published_frames == 1U,
               "a transient sink rejection retries the retained compositor frame once");
    }
    expect(renderer->system_memory_output_enabled(),
           "a connected consumer opens system-memory compositor output");
    {
        std::scoped_lock lock{state->mutex};
        state->consumer_present = false;
    }
    const auto demand_close_deadline = std::chrono::steady_clock::now() + 1s;
    while (renderer->system_memory_output_enabled() &&
           std::chrono::steady_clock::now() < demand_close_deadline) {
        std::this_thread::sleep_for(10ms);
    }
    expect(!renderer->system_memory_output_enabled(),
           "disconnecting the final consumer closes GPU readback again");
    expect(controller.set_enabled(false),
           "disabling the output stops the platform backend");
    expect(!renderer->system_memory_output_enabled(),
           "the virtual camera releases system-memory compositor output");
    expect(controller.health().state ==
               solin::media_engine::VirtualCameraSinkState::stopped,
           "the controller reports a stopped sink after disable");
}

} // namespace

int main() {
    test_controller_normalizes_and_pumps_the_virtual_camera_bus();
    return failures == 0 ? 0 : 1;
}
