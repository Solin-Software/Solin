#include "solin/media_engine/virtual_camera_output.hpp"

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

    [[nodiscard]] bool publish(
        const solin::media_engine::VideoFrameView& frame) noexcept override {
        std::scoped_lock lock{state_->mutex};
        if (!state_->started ||
            frame.pixel_format !=
                solin::media_engine::VideoFramePixelFormat::nv12) {
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
                windows_media_foundation,
            .platform_supported = true,
            .registration_api_available = true,
            .source_component_installed = true,
            .cross_session_transport_available = true,
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
        const std::uint64_t sequence) noexcept override {
        static_cast<void>(bus);
        static_cast<void>(graph);
        static_cast<void>(sequence);
    }

    void set_output_enabled(const solin::media_engine::OutputBus bus,
                            const bool enabled,
                            const std::uint64_t sequence) noexcept override {
        static_cast<void>(bus);
        static_cast<void>(enabled);
        static_cast<void>(sequence);
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(const solin::media_engine::OutputBus bus) const noexcept override {
        static_cast<void>(bus);
        return {};
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(
        const solin::media_engine::OutputBus bus,
        const std::uint64_t after_sequence,
        const solin::media_engine::VideoFrameVisitor& visitor) const noexcept override {
        if (bus != solin::media_engine::OutputBus::virtual_camera ||
            after_sequence >= 1U) {
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
        return frame.sequence;
    }

    void shutdown() noexcept override {}
};

void test_controller_normalizes_and_pumps_the_virtual_camera_bus() {
    auto state = std::make_shared<SinkState>();
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::VirtualCameraOutputController controller{
        renderer, std::make_unique<FakeVirtualCameraBackend>(state)};
    const solin::media_engine::SceneOutputDefinition output{
        .bus = solin::media_engine::OutputBus::virtual_camera,
        .default_scene_id = "scene-main",
        .transition = "cut",
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
    expect(controller.configure(output),
           "a virtual-camera output route configures without side effects");
    expect(controller.set_enabled(true),
           "enabling the output starts the selected platform backend");
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
               "the pump publishes each new compositor frame once");
    }
    expect(controller.set_enabled(false),
           "disabling the output stops the platform backend");
    expect(controller.health().state ==
               solin::media_engine::VirtualCameraSinkState::stopped,
           "the controller reports a stopped sink after disable");
}

} // namespace

int main() {
    test_controller_normalizes_and_pumps_the_virtual_camera_bus();
    return failures == 0 ? 0 : 1;
}
